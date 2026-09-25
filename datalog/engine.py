"""Stratified Datalog evaluation engine with group aggregation.

Pipeline of ``evaluate``:

1. Validate inputs (types, limits, fixed arity, rule safety, aggregate
   shape, explicit group domain).
2. Build the predicate dependency graph and compute strata.  Positive edges
   keep strata equal; negative *and aggregate* edges force a strictly
   higher stratum.  A cycle through a negative edge raises
   ``StratificationError``; a cycle through an aggregate edge raises
   ``AggregateStratificationError``.
3. Evaluate stratum by stratum (lower strata first).  At each stratum:
   a. every aggregate rule of that stratum fires **once** over the already
      complete lower strata (set-of-rows semantics), atomically committing
      all group results — budget overflow publishes none of them;
   b. ordinary rules then run semi-naive delta iteration for positive
      recursion, so higher-stratum normal rules (and same-stratum normal
      rules) can consume the freshly aggregated facts.
4. For every derived fact record the *shortest* derivation (minimal rule-
   application depth), breaking ties by the lexicographically smallest key
   ``(rule_index, child facts in body order)``.  Aggregate derivations
   additionally keep every contributing join row (contributing facts in
   body order) and the aggregate value; facts carry deterministic stable
   integer IDs.  The per-fact derivations together form a DAG.
5. A global fact budget bounds ``input facts + derived facts``.  If the next
   semi-naive round, or a stratum's whole aggregate commit, would exceed
   it, that batch is *not* applied at all and an explicitly incomplete
   ``Result`` (``complete=False``) is returned — never a partial answer (and
   never a half-finished aggregate total) masquerading as a complete one.
"""

from __future__ import annotations

from collections import namedtuple

from .errors import (
    AggregateError,
    AggregateStratificationError,
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
)
from .terms import (
    Aggregate,
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


#: One derivation step for an ordinary rule: ``rule_index`` into the rule
#: list passed to ``evaluate`` and ``children`` = the facts matched to the
#: rule's positive body literals, in body order.  Negated literals
#: contribute no children (they are absence checks, not derivations).
Derivation = namedtuple("Derivation", ["rule_index", "children"])

#: A derivation of an aggregate fact.  ``rule_index`` identifies the
#: aggregate rule; ``contributors`` is the tuple of *distinct* join rows
#: that fed the group, each row a tuple of contributing facts in positive-
#: body order (rows dedup by their fact tuples, so one fact reachable by
#: several derivations is never counted twice); ``contributor_ids`` holds
#: the stable integer ids of those same facts (same shape); ``value`` is
#: the published aggregate value (count or sum); ``explicit`` marks groups
#: that came from the caller-supplied group domain and had no rows.
AggregateDerivation = namedtuple(
    "AggregateDerivation",
    ["rule_index", "contributors", "contributor_ids", "value", "explicit"],
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
            the facts derived by fully-finished batches (semi-naive rounds or
            atomic aggregate commits) and ``reason`` explains the
            incompleteness — an incomplete result is never silently
            presented as complete.
        reason: ``None`` when complete, else a human-readable cause.
        facts: frozenset of all facts (input + derived) as tuples.
        derived: frozenset of the derived (non-input) facts.
    """

    def __init__(self, *, complete, reason, facts, input_facts, derivations,
                 rules, fact_ids=None):
        self.complete = complete
        self.reason = reason
        self.facts = frozenset(facts)
        self.derived = frozenset(f for f in facts if f not in input_facts)
        self._input_facts = frozenset(input_facts)
        self._derivations = dict(derivations)
        self._rules = rules
        #: Deterministic stable integer id per fact (input first, then
        #: derived in commit order); survives re-runs with identical input.
        self._fact_ids = dict(fact_ids or ())

    def fact_id(self, fact):
        """Stable integer id of ``fact`` (stable across identical runs)."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")
        return self._fact_ids[fact]

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
        """The recorded derivation for ``fact``: a :class:`Derivation`
        (ordinary rule), an :class:`AggregateDerivation` (aggregate rule),
        or ``None`` for an input fact.  Raises ``KeyError`` for a fact not
        in the result."""
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

        def walk(f, depth, prefix=""):
            d = self._derivations.get(f)
            pad = "  " * depth
            fid = self._fact_ids.get(f)
            idtag = f"  [#{fid}]" if fid is not None else ""
            if d is None:
                lines.append(f"{pad}{prefix}{_fmt_fact(f)}  [input fact]{idtag}")
                return
            if isinstance(d, AggregateDerivation):
                kind = "explicit empty group, " if d.explicit else ""
                lines.append(
                    f"{pad}{prefix}{_fmt_fact(f)}  <= aggregate rule "
                    f"{d.rule_index} ({kind}{d.value}): "
                    f"{self._rules[d.rule_index]!r}{idtag}"
                )
                for row in d.contributors:
                    if len(row) == 1:
                        walk(row[0], depth + 1)
                    else:
                        lines.append(f"{'  ' * (depth + 1)}- contributing row:")
                        for cf in row:
                            walk(cf, depth + 2, prefix="* ")
                return
            lines.append(
                f"{pad}{prefix}{_fmt_fact(f)}  <= rule {d.rule_index}: "
                f"{self._rules[d.rule_index]!r}{idtag}"
            )
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
        agg = rule.aggregate
        if agg is not None and not isinstance(agg, Aggregate):
            raise ValidationError(
                f"{where}: rule aggregate must be an Aggregate "
                f"(use Aggregate('count') / Aggregate('sum', Var(...))), "
                f"got {agg!r}"
            )
        head = rule.head
        if not head:
            raise ValidationError(f"{where}: head atom must be a non-empty tuple")
        check_predicate(head[0], f"{where}, head")
        for j, arg in enumerate(head[1:]):
            check_term(arg, f"{where}, head argument {j}")
        if agg is not None and not head[1:]:
            raise ValidationError(
                f"{where}: aggregate rule head must have at least one argument "
                f"(group keys plus the aggregate value)"
            )
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
        if agg is not None and not any(l.positive for l in body):
            raise ValidationError(
                f"{where}: aggregate rule must have at least one positive body "
                f"literal contributing facts"
            )
        out.append(Rule(head, body, agg))
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
        agg = rule.aggregate
        pos_vars = set()
        for lit in rule.body:
            if lit.positive:
                pos_vars |= atom_vars(lit.atom)
        if agg is None:
            head_vars = atom_vars(rule.head)
        else:
            # Aggregate rule: the last head column is the computed value;
            # all preceding columns are group keys.
            result_arg = rule.head[-1]
            if not isinstance(result_arg, Var):
                raise UnsafeRuleError(
                    f"rule {ri}: the last head argument of an aggregate rule "
                    f"must be a variable placeholder for the aggregate value, "
                    f"got {result_arg!r}"
                )
            if result_arg in pos_vars:
                raise UnsafeRuleError(
                    f"rule {ri}: aggregate result variable {result_arg.name!r} "
                    f"must not also be bound in a positive body literal"
                )
            head_vars = set()
            for j, a in enumerate(rule.head[1:-1]):
                if not isinstance(a, Var):
                    raise UnsafeRuleError(
                        f"rule {ri}: group-key head argument {j} must be a "
                        f"variable bound in a positive body literal, got {a!r}"
                    )
                head_vars.add(a)
            if agg.func == "sum" and agg.value not in pos_vars:
                raise UnsafeRuleError(
                    f"rule {ri}: sum value variable {agg.value.name!r} does "
                    f"not appear in a positive body literal (unsafe rule)"
                )
        for v in sorted(head_vars - pos_vars, key=lambda v: v.name):
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


def _check_groups(groups, rules):
    """Validate the caller-supplied explicit group domain.

    ``groups`` maps ``rule_index`` -> iterable of ground group-key tuples.
    Only aggregate rules may have a domain; each key must be ground and have
    exactly the group-key arity of that rule's head.
    """
    if groups is None:
        return {}
    if not isinstance(groups, dict):
        raise ValidationError(
            f"groups must be a dict mapping rule index to an iterable of "
            f"group-key tuples, got {type(groups).__name__}"
        )
    out = {}
    for key, dom in groups.items():
        where = f"groups[{key!r}]"
        if isinstance(key, bool) or not isinstance(key, int) or key < 0 or key >= len(rules):
            raise ValidationError(
                f"{where}: rule index must be an int in [0, {len(rules)})"
            )
        rule = rules[key]
        if rule.aggregate is None:
            raise ValidationError(
                f"{where}: rule {key} is not an aggregate rule"
            )
        key_arity = len(rule.head) - 2  # minus predicate and aggregate value
        if isinstance(dom, (str, bytes)) or not hasattr(dom, "__iter__"):
            raise ValidationError(f"{where}: group domain must be an iterable")
        keyset = set()
        for gi, gkey in enumerate(dom):
            gwhere = f"{where}, group {gi}"
            if not isinstance(gkey, (tuple, list)):
                raise ValidationError(
                    f"{gwhere}: group key must be a tuple, got {gkey!r}"
                )
            gkey = tuple(gkey)
            if len(gkey) != key_arity:
                raise ValidationError(
                    f"{gwhere}: group key arity {len(gkey)} does not match "
                    f"rule {key} group-key arity {key_arity}"
                )
            for j, a in enumerate(gkey):
                if isinstance(a, Var):
                    raise ValidationError(
                        f"{gwhere}, argument {j}: group keys must be ground"
                    )
                check_constant(a, f"{gwhere}, argument {j}")
            keyset.add(gkey)
        out[key] = keyset
    return out


# --- Stratification -------------------------------------------------------

def _dependency_edges(rules):
    """``(head_pred, body_pred, kind, rule_index)`` with kind in
    ``"pos"`` / ``"neg"`` / ``"agg"``.  Every body edge of an aggregate
    rule is a strict (stratum-crossing) edge: an aggregate may consume only
    strictly lower, already-complete strata."""
    edges = []
    for ri, rule in enumerate(rules):
        for lit in rule.body:
            if rule.aggregate is not None and lit.positive:
                kind = "agg"
            elif not lit.positive:
                kind = "neg"
            else:
                kind = "pos"
            edges.append((rule.head[0], lit.atom[0], kind, ri))
    return edges


def _bad_cycle_predicates(predicates, edges, kind):
    """Predicates lying on a dependency cycle whose internal edges include
    one of ``kind`` (``"neg"`` / ``"agg"``).  Uses Tarjan SCCs; an edge can
    only close a cycle when both ends are in the same SCC."""
    adj = {p: [] for p in predicates}
    for h, b, _k, _ri in edges:
        adj[h].append(b)

    index = {}
    low = {}
    stack = []
    on_stack = set()
    counter = [0]
    sccs = []

    def strongconnect(v):
        index[v] = counter[0]
        low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj[v]:
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.add(w)
                if w == v:
                    break
            sccs.append(comp)

    for p in sorted(predicates, key=lambda x: x):
        if p not in index:
            strongconnect(p)

    bad = set()
    for comp in sccs:
        if not any(
            h in comp and b in comp and k == kind
            for h, b, k, _ri in edges
        ):
            continue
        # A self-loop through the strict edge, or a multi-node SCC.
        internal_edges = [
            (h, b) for h, b, k, _ri in edges
            if k == kind and h in comp and b in comp
        ]
        on_cycle = set()
        for h, b in internal_edges:
            if h == b:
                on_cycle.add(h)
            else:
                on_cycle |= comp  # every node in a nontrivial SCC lies on
                #                  some cycle; keep the message small/safe
        bad |= on_cycle
    return bad


def _stratify(predicates, rules):
    """Assign each predicate a stratum number.

    Positive dependency: stratum(head) >= stratum(body).
    Negative or aggregate dependency: stratum(head) >= stratum(body) + 1.
    Relaxation converges iff no dependency cycle passes through a strict
    edge.  A negative-edge cycle raises ``StratificationError``; an
    aggregate-edge cycle raises ``AggregateStratificationError``.
    """
    edges = _dependency_edges(rules)

    stratum = {p: 0 for p in predicates}
    n = len(predicates)
    for _ in range(n + 1):
        changed = False
        for h, b, kind, _ri in edges:
            need = stratum[b] + (1 if kind in ("neg", "agg") else 0)
            if need > stratum[h]:
                stratum[h] = need
                changed = True
        if not changed:
            break
    else:
        # Relaxation did not converge within n+1 passes: some dependency
        # cycle passes through a strict (neg/agg) edge.  Classify the
        # offending cycles via SCCs (negation reported first).
        neg_bad = _bad_cycle_predicates(predicates, edges, "neg")
        if neg_bad:
            raise StratificationError(
                "dependency cycle through a negative edge; predicates involved: "
                + ", ".join(repr(p) for p in sorted(neg_bad))
            )
        agg_bad = _bad_cycle_predicates(predicates, edges, "agg")
        raise AggregateStratificationError(
            "recursive dependency cycle through an aggregate edge is not "
            "allowed (aggregates may only consume lower strata); "
            "predicates involved: "
            + ", ".join(repr(p) for p in sorted(agg_bad))
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


def _eval_aggregate_rule(ri, rule, store, explicit_keys):
    """Fire one aggregate rule once over the completed lower strata.

    Returns ``(results, order)`` where ``results`` maps group key ->
    ``(value, contributors, explicit)`` and ``order`` is the sorted list of
    group keys (deterministic).  ``contributors`` is the sorted tuple of
    distinct join rows (each a tuple of contributing facts in body order);
    set semantics guarantee one fact never contributes twice no matter how
    many derivations produced it.

    Raises ``AggregateError`` when a ``sum`` value is not an integer.
    """
    pos_lits = [l for l in rule.body if l.positive]
    neg_lits = [l for l in rule.body if not l.positive]
    key_vars = rule.head[1:-1]

    distinct_rows = {}  # children row -> env (env is unique per row)
    for env, children in _join(pos_lits, store, {}, None):
        if neg_lits and not _negated_ok(neg_lits, env, store):
            continue
        distinct_rows.setdefault(children, env)

    grouped = {}
    for children, env in distinct_rows.items():
        gkey = tuple(env[v] for v in key_vars)
        grouped.setdefault(gkey, []).append((children, env))

    results = {}
    for gkey, rows in grouped.items():
        rows_sorted = tuple(
            sorted(rows, key=lambda ce: tuple(_fact_key(c) for c in ce[0]))
        )
        if rule.aggregate.func == "count":
            value = len(rows_sorted)
        else:
            total_sum = 0
            for _children, env in rows_sorted:
                v = env[rule.aggregate.value]
                if isinstance(v, bool) or not isinstance(v, int):
                    raise AggregateError(
                        f"rule {ri}: sum value must be an integer for group "
                        f"{gkey!r}, got {type(v).__name__} {v!r}"
                    )
                total_sum += v
            value = total_sum
        results[gkey] = (
            value,
            tuple(c for c, _e in rows_sorted),
            False,
        )

    # Empty groups exist *only* inside the caller-supplied explicit domain.
    for gkey in explicit_keys:
        if gkey not in results:
            results[gkey] = (0, (), True)

    order = sorted(results, key=lambda k: tuple(_term_key(a) for a in k))
    return results, order


def _commit_aggregates(agg_rules, store, depth, prov, total, max_facts,
                       groups, next_id, fact_ids):
    """Atomically commit every aggregate rule of one stratum.

    All aggregate facts of the stratum are computed first; the commit is
    applied only if the *whole* batch fits the budget — otherwise nothing is
    published, so a half-finished group total can never escape.
    Returns ``(complete, total, next_id, reason)``.
    """
    planned = []  # (fact, AggregateDerivation, new_depth) in commit order
    skipped = set()
    for ri, rule in sorted(agg_rules, key=lambda x: x[0]):
        explicit = groups.get(ri, set())
        results, order = _eval_aggregate_rule(ri, rule, store, explicit)
        hpred = rule.head[0]
        for gkey in order:
            value, contributors, is_explicit = results[gkey]
            fact = (hpred,) + gkey + (value,)
            if fact in store.get(hpred, ()) or fact in skipped:
                # Already an input/earlier-stratum/earlier-rule fact: the
                # existing fact (and its provenance) wins under set semantics.
                continue
            skipped.add(fact)
            ids = tuple(
                tuple(fact_ids[c] for c in row) for row in contributors
            )
            ad = AggregateDerivation(ri, contributors, ids, value, is_explicit)
            new_depth = 1 + max(
                (depth[c] for row in contributors for c in row), default=0
            )
            planned.append((fact, ad, new_depth))

    if total + len(planned) > max_facts:
        return (
            False,
            total,
            next_id,
            f"fact budget exceeded: {total} facts so far + "
            f"{len(planned)} aggregate facts in the next batch > budget {max_facts}",
        )

    for fact, ad, new_depth in sorted(planned, key=lambda x: _fact_key(x[0])):
        store.setdefault(fact[0], set()).add(fact)
        depth[fact] = new_depth
        prov[fact] = ad
        fact_ids[fact] = next_id
        next_id += 1
    return True, total + len(planned), next_id, None


def _eval_stratum(stratum_preds, stratum_rules, store, depth, prov, total,
                  max_facts, next_id, fact_ids):
    """Semi-naive least-model computation for one stratum's ordinary rules.

    Returns ``(complete, total, next_id, reason)``; on budget overflow the
    offending round is discarded wholesale (no partial round is applied).
    """
    delta = {p: set(store.get(p, ())) for p in stratum_preds}
    round_no = 0
    while True:
        if round_no > 0 and not any(delta[p] for p in stratum_preds):
            return True, total, next_id, None
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
            return True, total, next_id, None
        if total + len(candidates) > max_facts:
            return (
                False,
                total,
                next_id,
                f"fact budget exceeded: {total} facts so far + "
                f"{len(candidates)} facts in the next round > budget {max_facts}",
            )
        new_delta = {p: set() for p in stratum_preds}
        for fact in sorted(candidates, key=_fact_key):
            key, children = candidates[fact]
            new_depth, ri, _ = key
            store.setdefault(fact[0], set()).add(fact)
            depth[fact] = new_depth
            prov[fact] = Derivation(ri, children)
            fact_ids[fact] = next_id
            next_id += 1
            new_delta[fact[0]].add(fact)
        total += len(candidates)
        delta = new_delta
        round_no += 1


# --- Public entry point ---------------------------------------------------

def evaluate(facts, rules, *, max_facts=DEFAULT_FACT_BUDGET, groups=None):
    """Evaluate stratified Datalog rules (with optional group aggregation)
    over ground facts.

    Args:
        facts: iterable of ground atom tuples ``(pred, const, ...)``.
        rules: iterable of :class:`Rule` objects; a rule carrying an
            :class:`Aggregate` is a group aggregate evaluated once over the
            completed lower strata.
        max_facts: total-fact budget (input + derived), an ``int`` >= 0
            (``bool`` rejected).  Exceeding it yields an incomplete
            :class:`Result` rather than an exception.
        groups: optional ``{rule_index: [group_key_tuple, ...]}`` giving the
            explicit group domain for aggregate rules.  A listed key with no
            contributing rows produces a zero; unlisted empty groups produce
            nothing.

    Returns:
        A :class:`Result`.  ``result.complete`` is ``False`` exactly when the
        budget stopped the computation; ``result.reason`` then says why.

    Raises:
        ValidationError: malformed input (located message).
        UnsafeRuleError: head/negated/aggregate variable not bound as
            required.
        LimitError: more than 20 predicates / 100 rules / 2000 facts.
        StratificationError: dependency cycle through a negative edge.
        AggregateStratificationError: dependency cycle through an aggregate
            edge.
        AggregateError: a ``sum`` value is not an integer.
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
    group_domain = _check_groups(groups, norm_rules)
    stratum = _stratify(arity.keys(), norm_rules)

    store = {}
    for fact in norm_facts:
        store.setdefault(fact[0], set()).add(fact)
    depth = {}
    fact_ids = {}
    # Deterministic stable ids: input facts first, sorted by fact key.
    for fid, fact in enumerate(
        sorted((f for fs in store.values() for f in fs), key=_fact_key)
    ):
        depth[fact] = 0
        fact_ids[fact] = fid
    prov = {}
    total = len(norm_facts)
    next_id = total

    def make_result(complete, reason):
        all_facts = {f for fs in store.values() for f in fs}
        return Result(
            complete=complete,
            reason=reason,
            facts=all_facts,
            input_facts=norm_facts,
            derivations=prov,
            rules=norm_rules,
            fact_ids=fact_ids,
        )

    if total > max_facts:
        return make_result(
            False,
            f"fact budget exceeded by input alone: {total} input facts "
            f"> budget {max_facts}",
        )

    # Group rules by head-predicate stratum; evaluate strata in order.
    strata_rules = {}
    for ri, rule in enumerate(norm_rules):
        strata_rules.setdefault(stratum[rule.head[0]], []).append((ri, rule))

    complete, reason = True, None
    for s in sorted(strata_rules):
        srules = strata_rules[s]
        agg_rules = [(ri, r) for ri, r in srules if r.aggregate is not None]
        plain_rules = [(ri, r) for ri, r in srules if r.aggregate is None]

        # Aggregates are the stratum boundary: fire each once over strictly
        # lower strata, committing the whole stratum batch atomically.
        if agg_rules:
            complete, total, next_id, reason = _commit_aggregates(
                agg_rules, store, depth, prov, total, max_facts,
                group_domain, next_id, fact_ids,
            )
            if not complete:
                break

        # Then the ordinary (possibly positively recursive) rules of this
        # stratum run to their fixpoint; they may consume aggregated facts.
        # Only ordinary-rule head predicates are "in-play" for semi-naive
        # recursion: the just-committed aggregate predicates are frozen
        # lower inputs (like any earlier stratum), so a non-recursive rule
        # referencing one fires in the first round only.
        if plain_rules:
            preds = {r.head[0] for _ri, r in plain_rules}
            complete, total, next_id, reason = _eval_stratum(
                preds, plain_rules, store, depth, prov, total, max_facts,
                next_id, fact_ids,
            )
            if not complete:
                break

    return make_result(complete, reason)
