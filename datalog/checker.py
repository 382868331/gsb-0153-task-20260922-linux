"""Independent evidence checker for Datalog results.

The checker trusts *nothing* the solver claims: it does not read
``complete`` flags or any other solver-computed booleans.  Given a
snapshot's public data (facts, base facts with their source ids, recorded
derivations, rules) it re-verifies, bottom-up from the base facts that
are still present:

* every base fact has at least one source id;
* every derived fact has a recorded derivation whose rule, head and
  positive-body children replay exactly (re-unified by the checker), and
  whose negated body literals are absent from the fact set;
* every derivation is well-founded: following children eventually
  reaches base facts — cyclic "evidence" with no base support is
  rejected;
* the fact budget, if given, is respected.

``recompute_least_model`` is a deliberately naive, self-contained
from-scratch evaluator (no code shared with ``datalog.engine`` or
``datalog.incremental``); ``verify_retractions`` uses it to confirm that
a retracted fact is genuinely no longer derivable.
"""

from __future__ import annotations

from collections import defaultdict

from .terms import Var


class EvidenceReport:
    """Result of a checker run: ``ok`` plus a tuple of human-readable
    ``violations`` (empty iff ok).  ``checked`` counts the derivations
    that were replayed."""

    __slots__ = ("ok", "violations", "checked")

    def __init__(self, violations, checked):
        self.ok = not violations
        self.violations = tuple(violations)
        self.checked = checked

    def __repr__(self):
        return (
            f"EvidenceReport(ok={self.ok}, checked={self.checked}, "
            f"violations={len(self.violations)})"
        )


def verify_evidence(*, facts, derived, base_sources, derivations, rules,
                    max_facts=None):
    """Verify recorded evidence bottom-up.  All inputs are plain data:

    ``facts``: iterable of all facts (base + derived).
    ``derived``: iterable of the derived facts.
    ``base_sources``: mapping base fact -> non-empty iterable of source ids.
    ``derivations``: mapping derived fact -> ``Derivation``-like
        ``(rule_index, children)``.
    ``rules``: the rule list the derivation indices refer to.
    ``max_facts``: optional budget the total fact count must respect.

    Returns an :class:`EvidenceReport`.
    """
    violations = []
    facts = set(facts)
    derived = set(derived)
    base = set(base_sources)

    if max_facts is not None and len(facts) > max_facts:
        violations.append(
            f"budget violated: {len(facts)} facts > max_facts {max_facts}"
        )
    if not derived <= facts:
        violations.append("derived facts are not a subset of all facts")
    overlap = base & derived
    if overlap:
        violations.append(
            f"facts both base and derived: {sorted(map(repr, overlap))[:3]}"
        )
    if base | derived != facts:
        missing = facts - (base | derived)
        violations.append(
            f"facts neither base nor derived: {sorted(map(repr, missing))[:3]}"
        )
    for f in sorted(base, key=repr):
        srcs = base_sources[f]
        if not srcs:
            violations.append(f"base fact {f!r} has no source id")
        for s in srcs:
            if isinstance(s, bool) or not isinstance(s, (int, str)):
                violations.append(f"base fact {f!r} has invalid source id {s!r}")

    checked = 0
    for f in sorted(derived, key=repr):
        d = derivations.get(f)
        if d is None:
            violations.append(f"derived fact {f!r} has no recorded derivation")
            continue
        checked += 1
        ri, children = d
        if not (0 <= ri < len(rules)):
            violations.append(
                f"derived fact {f!r}: rule index {ri} out of range "
                f"(rules version has {len(rules)} rules)"
            )
            continue
        rule = rules[ri]
        if rule.head[0] != f[0]:
            violations.append(
                f"derived fact {f!r}: rule {ri} head predicate "
                f"{rule.head[0]!r} does not match"
            )
            continue
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        if len(pos_lits) != len(children):
            violations.append(
                f"derived fact {f!r}: rule {ri} has {len(pos_lits)} positive "
                f"body literals but {len(children)} children recorded"
            )
            continue
        env = {}
        ok = True
        for lit, child in zip(pos_lits, children):
            if lit.atom[0] != child[0]:
                violations.append(
                    f"derived fact {f!r}: child {child!r} does not match "
                    f"literal {lit.atom!r}"
                )
                ok = False
                break
            for a, v in zip(lit.atom[1:], child[1:]):
                if isinstance(a, Var):
                    if a in env:
                        if env[a] != v:
                            ok = False
                            break
                    else:
                        env[a] = v
                elif a != v:
                    ok = False
                    break
            if not ok:
                violations.append(
                    f"derived fact {f!r}: children do not unify with rule "
                    f"{ri} positive body"
                )
                break
        if not ok:
            continue
        head = (rule.head[0],) + tuple(
            env[a] if isinstance(a, Var) else a for a in rule.head[1:]
        )
        if head != f:
            violations.append(
                f"derived fact {f!r}: rule {ri} body match derives {head!r} "
                f"instead"
            )
            continue
        for lit in neg_lits:
            g = (lit.atom[0],) + tuple(
                env[a] if isinstance(a, Var) else a for a in lit.atom[1:]
            )
            if g in facts:
                violations.append(
                    f"derived fact {f!r}: negated literal {lit.atom!r} of "
                    f"rule {ri} is satisfied by present fact {g!r}"
                )
        for c in children:
            if c not in facts:
                violations.append(
                    f"derived fact {f!r}: child {c!r} is not a current fact"
                )

    # Well-foundedness: close upward from the base facts; every derived
    # fact must become reachable.  Facts supported only by cycles of
    # other derived facts never do.
    reachable = set(base)
    parents = defaultdict(list)
    for f in derived:
        d = derivations.get(f)
        if d is None:
            continue
        for c in d[1]:
            parents[c].append(f)
    queue = list(base)
    while queue:
        c = queue.pop()
        for f in parents.get(c, ()):
            if f in reachable:
                continue
            d = derivations.get(f)
            if d is not None and all(ch in reachable for ch in d[1]):
                reachable.add(f)
                queue.append(f)
    unfounded = derived - reachable
    for f in sorted(unfounded, key=repr):
        violations.append(
            f"derived fact {f!r} has no well-founded support from the "
            f"current base facts (cyclic or dangling evidence)"
        )

    return EvidenceReport(violations, checked)


def verify_snapshot(snapshot):
    """Convenience wrapper: verify a :class:`SessionResult` (or any
    object with ``facts``/``derived``/``base_sources``/``derivation``/
    ``rules``/``max_facts``) using only its public data."""
    facts = set(snapshot.facts)
    derived = set(snapshot.derived)
    derivations = {f: snapshot.derivation(f) for f in derived}
    return verify_evidence(
        facts=facts,
        derived=derived,
        base_sources=snapshot.base_sources,
        derivations=derivations,
        rules=snapshot.rules,
        max_facts=snapshot.max_facts,
    )


# --- Independent from-scratch reference ------------------------------------

def _ref_stratify(preds, rules):
    edges = []
    for r in rules:
        for lit in r.body:
            edges.append((r.head[0], lit.atom[0], not lit.positive))
    stratum = {p: 0 for p in preds}
    for _ in range(len(preds) + 1):
        changed = False
        for h, b, is_neg in edges:
            need = stratum[b] + (1 if is_neg else 0)
            if need > stratum[h]:
                stratum[h] = need
                changed = True
        if not changed:
            return stratum
    raise ValueError("reference: unstratifiable program")


def _ref_fire(rule, store):
    pos_lits = [l for l in rule.body if l.positive]
    neg_lits = [l for l in rule.body if not l.positive]
    out = set()

    def subst(atom, env):
        return (atom[0],) + tuple(
            env[a] if isinstance(a, Var) else a for a in atom[1:]
        )

    def bt(i, env):
        if i == len(pos_lits):
            for lit in neg_lits:
                g = subst(lit.atom, env)
                if g in store.get(g[0], ()):
                    return
            out.add(subst(rule.head, env))
            return
        atom = pos_lits[i].atom
        for fact in store.get(atom[0], ()):
            env2 = dict(env)
            ok = True
            for a, v in zip(atom[1:], fact[1:]):
                if isinstance(a, Var):
                    if a in env2:
                        if env2[a] != v:
                            ok = False
                            break
                    else:
                        env2[a] = v
                elif a != v:
                    ok = False
                    break
            if ok:
                bt(i + 1, env2)

    bt(0, {})
    return out


def recompute_least_model(facts, rules):
    """Naive from-scratch least model of stratified Datalog: per stratum,
    re-fire every rule over the full accumulated store until fixpoint.
    Self-contained; shares no code with the engine under test."""
    preds = {f[0] for f in facts}
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = _ref_stratify(preds, rules)
    store = {}
    for f in facts:
        store.setdefault(f[0], set()).add(f)
    by_stratum = {}
    for r in rules:
        by_stratum.setdefault(stratum[r.head[0]], []).append(r)
    for s in sorted(by_stratum):
        while True:
            new = set()
            for r in by_stratum[s]:
                for f in _ref_fire(r, store):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            for f in new:
                store.setdefault(f[0], set()).add(f)
    return {f for fs in store.values() for f in fs}


def verify_retractions(rules, base_facts, retracted_facts):
    """Verify that every fact in ``retracted_facts`` is genuinely absent
    from the least model of ``base_facts`` + ``rules`` — recomputed from
    scratch by the independent reference, not taken on the solver's word.
    Returns an :class:`EvidenceReport`."""
    model = recompute_least_model(base_facts, rules)
    violations = [
        f"retracted fact {f!r} is still derivable from the current base "
        f"facts and rules"
        for f in sorted(set(retracted_facts), key=repr)
        if f in model
    ]
    return EvidenceReport(violations, checked=len(set(retracted_facts)))
