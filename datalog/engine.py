"""Stratified Datalog evaluation engine.

Pipeline of ``evaluate``:

1. Validate inputs (types, limits, fixed arity, rule safety).
2. Build the predicate dependency graph and compute strata; a cycle through
   a negative edge raises ``StratificationError``.
3. Evaluate stratum by stratum (lower strata first); inside one stratum use
   semi-naive delta iteration for positive recursion, producing the least
   (minimal) model of that stratum.
4. For every derived fact record the *shortest* derivation (minimal rule-
   application depth), breaking ties by the lexicographically smallest key
   ``(rule_index, child facts in body order)``.  The per-fact derivations
   together form a DAG (shared sub-derivations).
5. A global fact budget bounds ``input facts + derived facts``.  If the next
   semi-naive round would exceed it, that round is *not* applied at all and
   an explicitly incomplete ``Result`` (``complete=False``) is returned —
   never a partial answer masquerading as a complete one.
"""

from __future__ import annotations

from collections import namedtuple

from .errors import (
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
)
from .terms import (
    Literal,
    Rule,
    Var,
    atom_vars,
    check_constant,
    check_predicate,
    check_term,
)

# --- Declared interface limits -------------------------------------------

MAX_PREDICATES = 20
MAX_RULES = 100
MAX_INPUT_FACTS = 2000
DEFAULT_FACT_BUDGET = 100_000


#: One derivation step: ``rule_index`` into the rule list passed to
#: ``evaluate`` and ``children`` = the facts matched to the rule's positive
#: body literals, in body order.  Negated literals contribute no children
#: (they are absence checks, not derivations).
Derivation = namedtuple("Derivation", ["rule_index", "children"])


# --- Ordering keys (mixed int/float/str constants must be comparable) -----

def _term_key(value):
    if isinstance(value, (int, float)):  # bool already rejected at validation
        return (0, value)
    return (1, value)


def _fact_key(fact):
    return (fact[0], tuple(_term_key(a) for a in fact[1:]))


def _fmt_fact(fact):
    return f"{fact[0]}({', '.join(repr(a) for a in fact[1:])})"


# --- Result ---------------------------------------------------------------

class Result:
    """Outcome of :func:`evaluate`.

    Attributes:
        complete: ``True`` iff the least model was fully computed within the
            fact budget.  When ``False``, ``facts``/``derived`` contain only
            the facts derived by fully-finished semi-naive rounds and
            ``reason`` explains the incompleteness — an incomplete result is
            never silently presented as complete.
        reason: ``None`` when complete, else a human-readable cause.
        facts: frozenset of all facts (input + derived) as tuples.
        derived: frozenset of the derived (non-input) facts.
    """

    def __init__(self, *, complete, reason, facts, input_facts, derivations, rules):
        self.complete = complete
        self.reason = reason
        self.facts = frozenset(facts)
        self.derived = frozenset(f for f in facts if f not in input_facts)
        self._input_facts = frozenset(input_facts)
        self._derivations = dict(derivations)
        self._rules = rules

    def query(self, pred, *pattern):
        """Return the sorted list of stored facts for ``pred`` matching
        ``pattern`` (each entry a constant or a ``Var`` wildcard)."""
        check_predicate(pred, "query")
        for i, p in enumerate(pattern):
            if not isinstance(p, Var):
                check_constant(p, f"query pattern argument {i}")
        hits = [
            f
            for f in self.facts
            if f[0] == pred
            and len(f) - 1 == len(pattern)
            and all(isinstance(p, Var) or p == a for p, a in zip(pattern, f[1:]))
        ]
        hits.sort(key=_fact_key)
        return hits

    def derivation(self, fact):
        """The recorded :class:`Derivation` for ``fact``, or ``None`` for an
        input fact.  Raises ``KeyError`` for a fact not in the result."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")
        return self._derivations.get(fact)

    def explain(self, fact):
        """Render the derivation DAG of ``fact`` as an indented source chain."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")
        lines = []

        def walk(f, depth):
            d = self._derivations.get(f)
            pad = "  " * depth
            if d is None:
                lines.append(f"{pad}{_fmt_fact(f)}  [input fact]")
                return
            lines.append(f"{pad}{_fmt_fact(f)}  <= rule {d.rule_index}: {self._rules[d.rule_index]!r}")
            for child in d.children:
                walk(child, depth + 1)

        walk(fact, 0)
        return "\n".join(lines)


# --- Query-directed (demand-driven) evaluation ----------------------------
#
# A *demand* on a predicate is a partial binding: a frozenset of
# (arg_position, constant) pairs.  A fact satisfies a demand when every
# bound position holds the demanded constant.  Demands are seeded by the
# caller's query patterns (the "known conditions") and propagated backwards
# through rules: for each demand on a rule head, every body literal
# (positive AND negated) inherits a demand from its own constants and from
# head variables bound by the head demand.  Negated literals must propagate
# demand too — the facts that decide whether a negation holds are exactly
# the ones matching the negated literal's demand, so they are never pruned.
#
# Soundness sketch: if a fact F satisfies a demand, then every fact in
# every proof of F satisfies the propagated demand of its literal, so
# restricting the store to demanded facts loses no proof of any demanded
# fact.  Hence demanded facts are derived with the same semi-naive rounds,
# the same depths, and the same lexicographically-minimal shortest
# derivations as full evaluation.


class QueryStats(namedtuple("QueryStats", [
    "input_facts_total",
    "input_facts_used",
    "rules_total",
    "rules_used",
    "derived_facts",
    "pruned_derivations",
])):
    """Work actually done (and avoided) by evaluate_query.

    pruned_derivations counts candidate head facts that joins produced
    but that matched no demand, so they were discarded instead of entering
    the model (each would have triggered further downstream derivations
    under full evaluation).  Facts of predicates with no demand at all are
    never even generated; the skipped input facts and rules are reported
    via the total vs used counters.
    """

    __slots__ = ()

    @property
    def input_facts_skipped(self):
        return self.input_facts_total - self.input_facts_used

    @property
    def rules_skipped(self):
        return self.rules_total - self.rules_used

    @property
    def irrelevant_derivations_reduced(self):
        """Candidate derivations discarded as irrelevant to the query."""
        return self.pruned_derivations

    def summary(self):
        return (
            f"input facts used {self.input_facts_used}/{self.input_facts_total} "
            f"(skipped {self.input_facts_skipped}), "
            f"rules used {self.rules_used}/{self.rules_total} "
            f"(skipped {self.rules_skipped}), "
            f"derived {self.derived_facts} facts, "
            f"pruned {self.pruned_derivations} irrelevant derivations"
        )


class QueryResult(Result):
    """Result of evaluate_query: a Result over the demand-restricted
    model, plus the original query patterns and QueryStats."""

    def __init__(self, *, query_patterns, stats, **kwargs):
        super().__init__(**kwargs)
        self.query_patterns = query_patterns
        self.stats = stats

    def answer(self):
        """Sorted, de-duplicated facts matching the query patterns.

        Constants must match exactly; a Var is a wildcard, and a
        repeated Var within one pattern requires equal arguments.
        """
        out = set()
        for pattern in self.query_patterns:
            pred, pargs = pattern[0], pattern[1:]
            for fact in self.facts:
                if fact[0] != pred or len(fact) - 1 != len(pargs):
                    continue
                env = {}
                ok = True
                for p, a in zip(pargs, fact[1:]):
                    if isinstance(p, Var):
                        if p in env:
                            if env[p] != a:
                                ok = False
                                break
                        else:
                            env[p] = a
                    elif p != a:
                        ok = False
                        break
                if ok:
                    out.add(fact)
        return sorted(out, key=_fact_key)


# --- Validation -----------------------------------------------------------

def _validate_facts(facts):
    if isinstance(facts, (str, bytes)) or not hasattr(facts, "__iter__"):
        raise ValidationError(
            f"facts must be an iterable of atom tuples, got {type(facts).__name__}"
        )
    out = []
    seen = set()
    for i, fact in enumerate(facts):
        where = f"fact {i}"
        if not isinstance(fact, (tuple, list)) or not fact:
            raise ValidationError(f"{where}: fact must be a non-empty tuple, got {fact!r}")
        fact = tuple(fact)
        check_predicate(fact[0], where)
        for j, arg in enumerate(fact[1:]):
            if isinstance(arg, Var):
                raise ValidationError(f"{where}: facts must be ground, found variable {arg!r}")
            check_constant(arg, f"{where}, argument {j}")
        if fact not in seen:  # set semantics: duplicates collapse (documented)
            seen.add(fact)
            out.append(fact)
    if len(out) > MAX_INPUT_FACTS:
        raise LimitError(
            f"too many distinct facts: {len(out)} > limit {MAX_INPUT_FACTS}"
        )
    return out


def _validate_rules(rules):
    if isinstance(rules, (str, bytes)) or not hasattr(rules, "__iter__"):
        raise ValidationError(
            f"rules must be an iterable of Rule objects, got {type(rules).__name__}"
        )
    out = []
    for ri, rule in enumerate(rules):
        where = f"rule {ri}"
        if not isinstance(rule, Rule):
            raise ValidationError(f"{where}: expected a Rule, got {rule!r}")
        head = rule.head
        if not head:
            raise ValidationError(f"{where}: head atom must be a non-empty tuple")
        check_predicate(head[0], f"{where}, head")
        for j, arg in enumerate(head[1:]):
            check_term(arg, f"{where}, head argument {j}")
        body = []
        for bi, lit in enumerate(rule.body):
            lwhere = f"{where}, body literal {bi}"
            if not isinstance(lit, Literal):
                raise ValidationError(f"{lwhere}: expected a Literal (use pos()/neg()), got {lit!r}")
            if not isinstance(lit.positive, bool):
                raise ValidationError(
                    f"{lwhere}: polarity must be a bool, got {lit.positive!r}"
                )
            if not lit.atom:
                raise ValidationError(f"{lwhere}: atom must be a non-empty tuple")
            check_predicate(lit.atom[0], lwhere)
            for j, arg in enumerate(lit.atom[1:]):
                check_term(arg, f"{lwhere}, argument {j}")
            body.append(lit)
        out.append(Rule(head, body))
    if len(out) > MAX_RULES:
        raise LimitError(f"too many rules: {len(out)} > limit {MAX_RULES}")
    return out


def _check_predicates_and_arity(facts, rules):
    arity = {}

    def note(pred, n, where):
        if pred in arity:
            if arity[pred] != n:
                raise ValidationError(
                    f"{where}: predicate {pred!r} used with arity {n}, "
                    f"but already declared with arity {arity[pred]} (fixed arity)"
                )
        else:
            arity[pred] = n

    for i, fact in enumerate(facts):
        note(fact[0], len(fact) - 1, f"fact {i}")
    for ri, rule in enumerate(rules):
        note(rule.head[0], len(rule.head) - 1, f"rule {ri}, head")
        for bi, lit in enumerate(rule.body):
            note(lit.atom[0], len(lit.atom) - 1, f"rule {ri}, body literal {bi}")
    if len(arity) > MAX_PREDICATES:
        raise LimitError(
            f"too many distinct predicates: {len(arity)} > limit {MAX_PREDICATES}"
        )
    return arity


def _check_safety(rules):
    for ri, rule in enumerate(rules):
        pos_vars = set()
        for lit in rule.body:
            if lit.positive:
                pos_vars |= atom_vars(lit.atom)
        for v in sorted(atom_vars(rule.head) - pos_vars, key=lambda v: v.name):
            raise UnsafeRuleError(
                f"rule {ri}: head variable {v.name!r} does not appear in a "
                f"positive body literal (unsafe rule)"
            )
        for bi, lit in enumerate(rule.body):
            if not lit.positive:
                for v in sorted(atom_vars(lit.atom) - pos_vars, key=lambda v: v.name):
                    raise UnsafeRuleError(
                        f"rule {ri}, body literal {bi}: variable {v.name!r} in a "
                        f"negated literal does not appear in a positive body "
                        f"literal (unsafe rule)"
                    )


# --- Stratification -------------------------------------------------------

def _stratify(predicates, rules):
    """Assign each predicate a stratum number.

    Positive dependency: stratum(head) >= stratum(body).
    Negative dependency: stratum(head) >= stratum(body) + 1.
    Relaxation converges iff no dependency cycle passes through a negative
    edge; otherwise raise StratificationError naming the predicates.
    """
    edges = []  # (head_pred, body_pred, is_negative, rule_index)
    for ri, rule in enumerate(rules):
        for lit in rule.body:
            edges.append((rule.head[0], lit.atom[0], not lit.positive, ri))

    stratum = {p: 0 for p in predicates}
    n = len(predicates)
    for _ in range(n + 1):
        changed = False
        for h, b, is_neg, _ri in edges:
            need = stratum[b] + (1 if is_neg else 0)
            if need > stratum[h]:
                stratum[h] = need
                changed = True
        if not changed:
            break
    else:
        # Relaxation did not converge within n+1 passes: some dependency
        # cycle passes through a negative edge.  Any legitimate stratum
        # number is < n, so predicates inflated to >= n lie on (or
        # downstream of) such a cycle.
        bad = sorted(p for p in predicates if stratum[p] >= n)
        raise StratificationError(
            "dependency cycle through a negative edge; predicates involved: "
            + ", ".join(repr(p) for p in bad)
        )
    return stratum


# --- Rule evaluation ------------------------------------------------------

def _join(pos_lits, store, delta, distinguished):
    """Yield ``(env, children)`` for every matching of the positive literals.

    ``children[i]`` is the fact matched to ``pos_lits[i]`` (body order).
    The literal at index ``distinguished`` (if not ``None``) is restricted to
    that predicate's delta set — the semi-naive optimisation.
    """
    children = [None] * len(pos_lits)
    env = {}

    def backtrack(i):
        if i == len(pos_lits):
            yield dict(env), tuple(children)
            return
        atom = pos_lits[i].atom
        pred = atom[0]
        args = atom[1:]
        candidates = delta[pred] if i == distinguished else store.get(pred, ())
        for fact in candidates:
            bound = []
            ok = True
            for a, v in zip(args, fact[1:]):
                if isinstance(a, Var):
                    prev = env.get(a, _UNBOUND)
                    if prev is _UNBOUND:
                        env[a] = v
                        bound.append(a)
                    elif prev != v:
                        ok = False
                        break
                elif a != v:
                    ok = False
                    break
            if ok:
                children[i] = fact
                yield from backtrack(i + 1)
            for a in bound:
                del env[a]

    yield from backtrack(0)


_UNBOUND = object()


def _negated_ok(neg_lits, env, store):
    for lit in neg_lits:
        atom = lit.atom
        ground = (atom[0],) + tuple(
            env[a] if isinstance(a, Var) else a for a in atom[1:]
        )
        if ground in store.get(atom[0], ()):
            return False
    return True


def _substitute(atom, env):
    return (atom[0],) + tuple(env[a] if isinstance(a, Var) else a for a in atom[1:])


def _eval_stratum(stratum_preds, stratum_rules, store, depth, prov, total,
                  max_facts, match=None, stats=None):
    """Semi-naive least-model computation for one stratum.

    Returns ``(complete, total, reason)``; on budget overflow the offending
    round is discarded wholesale (no partial round is applied).
    """
    delta = {p: set(store.get(p, ())) for p in stratum_preds}
    round_no = 0
    while True:
        if round_no > 0 and not any(delta[p] for p in stratum_preds):
            return True, total, None
        candidates = {}  # fact -> (key, children)
        for ri, rule in stratum_rules:
            pos_lits = [l for l in rule.body if l.positive]
            neg_lits = [l for l in rule.body if not l.positive]
            recursive = [i for i, l in enumerate(pos_lits) if l.atom[0] in stratum_preds]
            if not recursive:
                if round_no > 0:
                    continue  # non-recursive rule: only fires in the first round
                distinguished = (None,)
            else:
                distinguished = tuple(recursive)
            for d in distinguished:
                for env, children in _join(pos_lits, store, delta, d):
                    if neg_lits and not _negated_ok(neg_lits, env, store):
                        continue
                    head = _substitute(rule.head, env)
                    hpred = head[0]
                    if head in store.get(hpred, ()):
                        continue  # derived in an earlier round => shorter proof kept
                    if match is not None and not match(head):
                        stats["pruned_derivations"] += 1
                        continue
                    child_depths = [depth[c] for c in children]
                    new_depth = 1 + (max(child_depths) if child_depths else 0)
                    key = (new_depth, ri, tuple(_fact_key(c) for c in children))
                    prev = candidates.get(head)
                    if prev is None or key < prev[0]:
                        candidates[head] = (key, children)
        if not candidates:
            return True, total, None
        if total + len(candidates) > max_facts:
            return (
                False,
                total,
                f"fact budget exceeded: {total} facts so far + "
                f"{len(candidates)} facts in the next round > budget {max_facts}",
            )
        new_delta = {p: set() for p in stratum_preds}
        for fact, (key, children) in candidates.items():
            new_depth, ri, _ = key
            store.setdefault(fact[0], set()).add(fact)
            depth[fact] = new_depth
            prov[fact] = Derivation(ri, children)
            new_delta[fact[0]].add(fact)
        total += len(candidates)
        delta = new_delta
        round_no += 1


# --- Public entry point ---------------------------------------------------

def evaluate(facts, rules, *, max_facts=DEFAULT_FACT_BUDGET):
    """Evaluate stratified Datalog rules over ground facts.

    Args:
        facts: iterable of ground atom tuples ``(pred, const, ...)``.
        rules: iterable of :class:`Rule` objects.
        max_facts: total-fact budget (input + derived), an ``int`` >= 0
            (``bool`` rejected).  Exceeding it yields an incomplete
            :class:`Result` rather than an exception.

    Returns:
        A :class:`Result`.  ``result.complete`` is ``False`` exactly when the
        budget stopped the computation; ``result.reason`` then says why.

    Raises:
        ValidationError: malformed input (located message).
        UnsafeRuleError: head/negated variable not bound positively.
        LimitError: more than 20 predicates / 100 rules / 2000 facts.
        StratificationError: dependency cycle through a negative edge.
    """
    if isinstance(max_facts, bool) or not isinstance(max_facts, int):
        raise ValidationError(
            f"max_facts must be an int (bool rejected), got {max_facts!r}"
        )
    if max_facts < 0:
        raise ValidationError(f"max_facts must be >= 0, got {max_facts}")

    norm_facts = _validate_facts(facts)
    norm_rules = _validate_rules(rules)
    arity = _check_predicates_and_arity(norm_facts, norm_rules)
    _check_safety(norm_rules)
    stratum = _stratify(arity.keys(), norm_rules)

    store = {}
    for fact in norm_facts:
        store.setdefault(fact[0], set()).add(fact)
    depth = {f: 0 for fs in store.values() for f in fs}
    prov = {}
    total = len(norm_facts)

    if total > max_facts:
        return Result(
            complete=False,
            reason=f"fact budget exceeded by input alone: {total} input facts "
            f"> budget {max_facts}",
            facts=set(norm_facts),
            input_facts=norm_facts,
            derivations=prov,
            rules=norm_rules,
        )

    # Group rules by head-predicate stratum; evaluate strata in order.
    strata_rules = {}
    for ri, rule in enumerate(norm_rules):
        strata_rules.setdefault(stratum[rule.head[0]], []).append((ri, rule))

    complete, reason = True, None
    for s in sorted(strata_rules):
        preds = {rule.head[0] for _ri, rule in strata_rules[s]}
        complete, total, reason = _eval_stratum(
            preds, strata_rules[s], store, depth, prov, total, max_facts
        )
        if not complete:
            break

    all_facts = {f for fs in store.values() for f in fs}
    return Result(
        complete=complete,
        reason=reason,
        facts=all_facts,
        input_facts=norm_facts,
        derivations=prov,
        rules=norm_rules,
    )


# --- Demand computation -------------------------------------------------

def _normalize_query(query, arity):
    """Validate the query and return a list of pattern tuples.

    Accepts a single pattern tuple ('reach', 'a', Var('X')), a bare
    predicate name ('reach'), or an iterable of such patterns.  Pattern
    arguments must be constants or Var; a repeated Var in one pattern is
    an equality constraint honoured by QueryResult.answer().
    """
    if isinstance(query, str):
        query = [(query,)]
    elif isinstance(query, tuple) and query and isinstance(query[0], str):
        query = [query]
    elif isinstance(query, (str, bytes)) or not hasattr(query, '__iter__'):
        raise ValidationError(
            f'query must be a pattern tuple, a predicate name, or an '
            f'iterable of patterns, got {query!r}'
        )
    patterns = []
    for qi, pat in enumerate(query):
        where = f'query pattern {qi}'
        if isinstance(pat, str):
            pat = (pat,)
        if not isinstance(pat, (tuple, list)) or not pat:
            raise ValidationError(
                f'{where}: pattern must be a non-empty tuple, got {pat!r}'
            )
        pat = tuple(pat)
        check_predicate(pat[0], where)
        if len(pat) == 1 and arity.get(pat[0], 0) > 0:
            # Bare predicate name: all argument positions open.
            pat = (pat[0],) + tuple(Var(f'_q{i}') for i in range(arity[pat[0]]))
        for j, a in enumerate(pat[1:]):
            if not isinstance(a, Var):
                check_constant(a, f'{where}, argument {j}')
        if pat[0] in arity and len(pat) - 1 != arity[pat[0]]:
            raise ValidationError(
                f'{where}: predicate {pat[0]!r} has arity '
                f'{arity[pat[0]]}, but the pattern has {len(pat) - 1} '
                f'arguments'
            )
        if pat not in patterns:
            patterns.append(pat)
    if not patterns:
        raise ValidationError('query must contain at least one pattern')
    return patterns


def _seed_demands(patterns):
    demands = {}
    for pat in patterns:
        d = frozenset(
            (j, a) for j, a in enumerate(pat[1:]) if not isinstance(a, Var)
        )
        demands.setdefault(pat[0], set()).add(d)
    return demands


def _propagate_demands(rules, demands):
    """Fixpoint: backwards demand propagation through every rule.

    For each demand on a rule head, every body literal (positive and
    negated alike) inherits a demand built from its own constants and
    from head variables the head demand binds to constants.  Variables
    bound only by other body literals cannot restrict the demand (their
    values are not known before the join), so the result is a sound
    over-approximation of the truly needed facts.
    """
    changed = True
    while changed:
        changed = False
        for rule in rules:
            hpred = rule.head[0]
            if hpred not in demands:
                continue
            for d in list(demands[hpred]):
                head_bind = {
                    a: v
                    for (j, v) in d
                    for a in [rule.head[1:][j]]
                    if isinstance(a, Var)
                }
                for lit in rule.body:
                    ld = []
                    for j, a in enumerate(lit.atom[1:]):
                        if isinstance(a, Var):
                            if a in head_bind:
                                ld.append((j, head_bind[a]))
                        else:
                            ld.append((j, a))
                    ld = frozenset(ld)
                    bucket = demands.setdefault(lit.atom[0], set())
                    if ld not in bucket:
                        bucket.add(ld)
                        changed = True
    return demands


def evaluate_query(facts, rules, query, *, max_facts=DEFAULT_FACT_BUDGET):
    """Query-directed evaluation: derive only what the query can reach.

    Same stratified least-model semantics as evaluate(), but restricted
    to the facts that can influence the answer to 'query' (one or more
    patterns with constants as known conditions and Var for open
    positions).  Rules of predicates no demand reaches are never fired;
    input facts and candidate derivations matching no demand are
    skipped.  Negated literals propagate demand exactly like positive
    ones, so every fact that decides whether a negation holds is kept —
    answers and recorded derivations for demanded facts are identical
    to full evaluation.

    max_facts budgets the facts actually materialised (used input facts
    plus kept derivations); overflow yields complete=False exactly as in
    evaluate().  Returns a QueryResult whose .stats records how much
    irrelevant work was avoided.
    """
    if isinstance(max_facts, bool) or not isinstance(max_facts, int):
        raise ValidationError(
            f'max_facts must be an int (bool rejected), got {max_facts!r}'
        )
    if max_facts < 0:
        raise ValidationError(f'max_facts must be >= 0, got {max_facts}')

    norm_facts = _validate_facts(facts)
    norm_rules = _validate_rules(rules)
    arity = _check_predicates_and_arity(norm_facts, norm_rules)
    _check_safety(norm_rules)
    patterns = _normalize_query(query, arity)
    stratum = _stratify(arity.keys(), norm_rules)

    demands = _propagate_demands(norm_rules, _seed_demands(patterns))

    def match(fact):
        return any(
            all(fact[j + 1] == v for j, v in d)
            for d in demands.get(fact[0], ())
        )

    used_rules = []
    for ri, rule in enumerate(norm_rules):
        if rule.head[0] in demands:
            used_rules.append((ri, rule))

    store = {}
    input_used = 0
    for fact in norm_facts:
        if match(fact):
            store.setdefault(fact[0], set()).add(fact)
            input_used += 1
    depth = {f: 0 for fs in store.values() for f in fs}
    prov = {}
    total = input_used
    counters = {'pruned_derivations': 0}

    complete, reason = True, None
    if total > max_facts:
        complete = False
        reason = (
            f'fact budget exceeded by demanded input alone: {total} '
            f'demanded input facts > budget {max_facts}'
        )
    else:
        strata_rules = {}
        for ri, rule in used_rules:
            strata_rules.setdefault(stratum[rule.head[0]], []).append((ri, rule))
        for s in sorted(strata_rules):
            preds = {rule.head[0] for _ri, rule in strata_rules[s]}
            complete, total, reason = _eval_stratum(
                preds, strata_rules[s], store, depth, prov, total,
                max_facts, match=match, stats=counters)
            if not complete:
                break

    all_facts = {f for fs in store.values() for f in fs}
    stats = QueryStats(
        input_facts_total=len(norm_facts),
        input_facts_used=input_used,
        rules_total=len(norm_rules),
        rules_used=len(used_rules),
        derived_facts=len(all_facts) - input_used,
        pruned_derivations=counters['pruned_derivations'],
    )
    return QueryResult(
        query_patterns=patterns,
        stats=stats,
        complete=complete,
        reason=reason,
        facts=all_facts,
        input_facts=[f for f in norm_facts if match(f)],
        derivations=prov,
        rules=norm_rules,
    )
