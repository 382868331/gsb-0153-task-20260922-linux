"""Independent naive from-scratch reference for the tests.

Computes the least model of a stratified Datalog program by naive
fixpoint per stratum.  Shares no code with ``datalog.engine`` /
``datalog.incremental`` / ``datalog.checker`` — only the public data
structures (``Var``, ``Rule``, literals) are read, never evaluated by
the package.  Expected values in the incremental tests come from this
reference plus hand-computed small cases, never from the solver.
"""

from datalog import Var


def naive_stratify(preds, rules):
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
    raise AssertionError("reference: unstratifiable program")


def naive_fire(rule, store):
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


def naive_least_model(facts, rules):
    """Least model of stratified ``rules`` over ground ``facts``."""
    preds = {f[0] for f in facts}
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = naive_stratify(preds, rules)
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
                for f in naive_fire(r, store):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            for f in new:
                store.setdefault(f[0], set()).add(f)
    return {f for fs in store.values() for f in fs}


class SourceTracker:
    """Independent bookkeeping of base facts and their source ids.

    The tests apply the same batches to a ``Session`` and to this
    tracker, then compare the session's base facts and per-fact source
    sets against it — the session never gets to grade its own source
    bookkeeping.
    """

    def __init__(self):
        self.sources = {}  # fact -> set(source_id)

    def apply(self, adds=(), removes=(), default_source="default"):
        for op in removes:
            fact, src = self._split(op, default_source)
            s = self.sources.get(fact)
            if s:
                s.discard(src)
                if not s:
                    del self.sources[fact]
        for op in adds:
            fact, src = self._split(op, default_source)
            self.sources.setdefault(fact, set()).add(src)

    @staticmethod
    def _split(op, default_source):
        if (
            isinstance(op, (tuple, list))
            and len(op) == 2
            and isinstance(op[0], (tuple, list))
        ):
            return tuple(op[0]), op[1]
        return tuple(op), default_source

    def facts(self):
        return set(self.sources)
