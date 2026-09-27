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
#: ``evaluate``/``evaluate_query`` and ``children`` = the facts matched
#: to the rule's positive body literals, in body order.  Negated literals
#: contribute no children (they are absence checks, not derivations).
Derivation = namedtuple("Derivation", ["rule_index", "children"])


#: Work counters for one evaluation run.
#:
#: * ``firings`` — rule applications that produced a candidate head fact
#:   (before set-semantics dedup); this is the "number of derivations
#:   performed" metric used to quantify irrelevant work.
#: * ``derived`` — distinct new facts added to the store.
#: * ``rounds`` — semi-naive rounds executed across all strata.
EvalStats = namedtuple("EvalStats", ["firings", "derived", "rounds"])


#: Relevance report attached to ``Result`` of :func:`evaluate_query``.
#:
#: * ``query_pred`` / ``pattern`` — the requested relation and pattern.
#: * ``relevant_predicates`` — predicates the query transitively depends on
#:   (through positive *and* negative edges; negative dependencies are kept
#:   so the facts deciding negation are never pruned).
#: * ``pruned_rules`` — original indices of rules never expanded.
#: * ``pruned_facts`` — number of input facts dropped as irrelevant.
#: * ``derivations_avoided`` — full-evaluation firings minus focused
#:   firings, or ``None`` unless ``measure_baseline=True`` was passed.
FocusStats = namedtuple(
    "FocusStats",
    [
        "query_pred",
        "pattern",
        "relevant_predicates",
        "pruned_rules",
        "pruned_facts",
        "derivations_avoided",
    ],
)


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

    def __init__(self, *, complete, reason, facts, input_facts, derivations,
                 rules, stats=None, focus=None):
        self.complete = complete
        self.reason = reason
        self.facts = frozenset(facts)
        self.derived = frozenset(f for f in facts if f not in input_facts)
        self._input_facts = frozenset(input_facts)
        self._derivations = dict(derivations)
        # Map of *original* rule index -> Rule (a dict so that focused
        # evaluation can keep the caller's rule numbering after pruning).
        self._rules = dict(rules)
        self.stats = stats
        self.focus = focus

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
                  max_facts, stats):
    """Semi-naive least-model computation for one stratum.

    Returns ``(complete, total, reason)``; on budget overflow the offending
    round is discarded wholesale (no partial round is applied).
    """
    delta = {p: set(store.get(p, ())) for p in stratum_preds}
    round_no = 0
    while True:
        if round_no > 0 and not any(delta[p] for p in stratum_preds):
            return True, total, None
        stats["rounds"] += 1
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
                    stats["firings"] += 1
                    hpred = head[0]
                    if head in store.get(hpred, ()):
                        continue  # derived in an earlier round => shorter proof kept
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
        stats["derived"] += len(candidates)
        delta = new_delta
        round_no += 1


# --- Core evaluation ------------------------------------------------------

def _evaluate_core(norm_facts, indexed_rules, predicates, max_facts):
    """Run stratified evaluation over already-validated input.

    ``indexed_rules`` is a list of ``(original_rule_index, Rule)`` pairs so
    that provenance keeps the caller's rule numbering even when irrelevant
    rules were pruned.  Returns a :class:`Result` with ``.stats`` filled.
    """
    rules_only = [rule for _ri, rule in indexed_rules]
    stratum = _stratify(predicates, rules_only)

    store = {}
    for fact in norm_facts:
        store.setdefault(fact[0], set()).add(fact)
    depth = {f: 0 for fs in store.values() for f in fs}
    prov = {}
    total = len(norm_facts)
    stats = {"firings": 0, "derived": 0, "rounds": 0}
    rule_map = dict(indexed_rules)

    def make_result(complete, reason, facts):
        return Result(
            complete=complete,
            reason=reason,
            facts=facts,
            input_facts=norm_facts,
            derivations=prov,
            rules=rule_map,
            stats=EvalStats(stats["firings"], stats["derived"], stats["rounds"]),
        )

    if total > max_facts:
        return make_result(
            False,
            f"fact budget exceeded by input alone: {total} input facts "
            f"> budget {max_facts}",
            set(norm_facts),
        )

    # Group rules by head-predicate stratum; evaluate strata in order.
    strata_rules = {}
    for ri, rule in indexed_rules:
        strata_rules.setdefault(stratum[rule.head[0]], []).append((ri, rule))

    complete, reason = True, None
    for s in sorted(strata_rules):
        preds = {rule.head[0] for _ri, rule in strata_rules[s]}
        complete, total, reason = _eval_stratum(
            preds, strata_rules[s], store, depth, prov, total, max_facts, stats
        )
        if not complete:
            break

    all_facts = {f for fs in store.values() for f in fs}
    return make_result(complete, reason, all_facts)


def _check_budget(max_facts):
    if isinstance(max_facts, bool) or not isinstance(max_facts, int):
        raise ValidationError(
            f"max_facts must be an int (bool rejected), got {max_facts!r}"
        )
    if max_facts < 0:
        raise ValidationError(f"max_facts must be >= 0, got {max_facts}")


# --- Public entry points --------------------------------------------------

def evaluate(facts, rules, *, max_facts=DEFAULT_FACT_BUDGET):
    """Evaluate stratified Datalog rules over ground facts (full evaluation).

    Args:
        facts: iterable of ground atom tuples ``(pred, const, ...)``.
        rules: iterable of :class:`Rule` objects.
        max_facts: total-fact budget (input + derived), an ``int`` >= 0
            (``bool`` rejected).  Exceeding it yields an incomplete
            :class:`Result` rather than an exception.

    Returns:
        A :class:`Result`.  ``result.complete`` is ``False`` exactly when the
        budget stopped the computation; ``result.reason`` then says why.
        ``result.stats`` is an :class:`EvalStats` with work counters.

    Raises:
        ValidationError: malformed input (located message).
        UnsafeRuleError: head/negated variable not bound positively.
        LimitError: more than 20 predicates / 100 rules / 2000 facts.
        StratificationError: dependency cycle through a negative edge.
    """
    _check_budget(max_facts)
    norm_facts = _validate_facts(facts)
    norm_rules = _validate_rules(rules)
    arity = _check_predicates_and_arity(norm_facts, norm_rules)
    _check_safety(norm_rules)
    return _evaluate_core(
        norm_facts, list(enumerate(norm_rules)), arity.keys(), max_facts
    )


def _relevant_predicates(query_pred, rules):
    """Predicates that can influence ``query_pred``: the transitive closure
    of the dependency graph starting at ``query_pred``, following *both*
    positive and negative body edges.  Negative edges must be followed so
    that the facts deciding whether a negation holds are never pruned."""
    deps = {}
    for rule in rules:
        deps.setdefault(rule.head[0], set()).update(
            lit.atom[0] for lit in rule.body
        )
    relevant = {query_pred}
    frontier = [query_pred]
    while frontier:
        pred = frontier.pop()
        for dep in deps.get(pred, ()):
            if dep not in relevant:
                relevant.add(dep)
                frontier.append(dep)
    return relevant


def evaluate_query(facts, rules, query_pred, *pattern,
                   max_facts=DEFAULT_FACT_BUDGET, measure_baseline=False):
    """Demand-driven evaluation: only expand what can affect one query.

    Same semantics as :func:`evaluate`, but rules and input facts whose
    predicate cannot transitively influence ``query_pred`` are pruned
    before evaluation (dependency closure through positive *and* negative
    edges, so stratified negation and recursion keep their exact meaning).
    The answers and provenance for the queried relation are identical to
    full evaluation; ``result.focus`` records what was pruned and how much
    irrelevant derivation was avoided.

    Args:
        facts, rules, max_facts: as in :func:`evaluate`.  The budget applies
            to the *focused* run (relevant input facts + derived facts).
        query_pred: the relation (predicate name) to answer for.
        *pattern: optional known conditions — constants or ``Var``
            wildcards, exactly like :meth:`Result.query`.  The pattern is
            validated and applied to the returned answers; pruning itself is
            predicate-level and therefore always sound.
        measure_baseline: when ``True``, additionally run full evaluation
            internally to fill ``focus.derivations_avoided`` with the
            measured reduction in rule firings.  Off by default so the
            focused run stays cheap.

    Returns:
        A :class:`Result` restricted to the relevant part of the model.
        ``result.query(query_pred, *pattern)`` (already applied for
        convenience — see ``result.answers``) matches full evaluation.
    """
    _check_budget(max_facts)
    if not isinstance(measure_baseline, bool):
        raise ValidationError(
            f"measure_baseline must be a bool, got {measure_baseline!r}"
        )
    check_predicate(query_pred, "evaluate_query")
    for i, p in enumerate(pattern):
        if not isinstance(p, Var):
            check_constant(p, f"evaluate_query pattern argument {i}")

    norm_facts = _validate_facts(facts)
    norm_rules = _validate_rules(rules)
    arity = _check_predicates_and_arity(norm_facts, norm_rules)
    _check_safety(norm_rules)

    relevant = _relevant_predicates(query_pred, norm_rules)
    kept_rules = [
        (ri, rule) for ri, rule in enumerate(norm_rules)
        if rule.head[0] in relevant
    ]
    pruned_rules = [
        ri for ri, rule in enumerate(norm_rules)
        if rule.head[0] not in relevant
    ]
    kept_facts = [f for f in norm_facts if f[0] in relevant]
    pruned_facts = len(norm_facts) - len(kept_facts)

    res = _evaluate_core(kept_facts, kept_rules, relevant, max_facts)

    avoided = None
    if measure_baseline:
        baseline = _evaluate_core(
            norm_facts, list(enumerate(norm_rules)), arity.keys(), max_facts
        )
        avoided = baseline.stats.firings - res.stats.firings

    res.focus = FocusStats(
        query_pred=query_pred,
        pattern=tuple(pattern),
        relevant_predicates=frozenset(relevant),
        pruned_rules=tuple(pruned_rules),
        pruned_facts=pruned_facts,
        derivations_avoided=avoided,
    )
    res.answers = res.query(query_pred, *pattern)
    return res
