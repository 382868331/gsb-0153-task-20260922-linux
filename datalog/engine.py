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


def _render_explain(fact, derivations, rules, leaf_label=None):
    """Render the derivation DAG of ``fact`` as an indented source chain.

    ``leaf_label(fact)`` customises the line suffix for facts without a
    derivation; the default is the historical ``[input fact]``.
    """
    if leaf_label is None:
        leaf_label = lambda f: "[input fact]"
    lines = []

    def walk(f, depth):
        d = derivations.get(f)
        pad = "  " * depth
        if d is None:
            lines.append(f"{pad}{_fmt_fact(f)}  {leaf_label(f)}")
            return
        lines.append(f"{pad}{_fmt_fact(f)}  <= rule {d.rule_index}: {rules[d.rule_index]!r}")
        for child in d.children:
            walk(child, depth + 1)

    walk(fact, 0)
    return "\n".join(lines)


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
        return _render_explain(fact, self._derivations, self._rules)


# --- Validation -----------------------------------------------------------

def _validate_facts(facts, limit=MAX_INPUT_FACTS):
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
    if len(out) > limit:
        raise LimitError(
            f"too many distinct facts: {len(out)} > limit {limit}"
        )
    return out


def _validate_rules(rules, limit=MAX_RULES):
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
    if len(out) > limit:
        raise LimitError(f"too many rules: {len(out)} > limit {limit}")
    return out


def _check_predicates_and_arity(facts, rules, limit=MAX_PREDICATES):
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
    if len(arity) > limit:
        raise LimitError(
            f"too many distinct predicates: {len(arity)} > limit {limit}"
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


def _eval_stratum(stratum_preds, stratum_rules, store, depth, prov, total, max_facts):
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
