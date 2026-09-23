"""Extended verification (separate from the fast regression suite):

  * main scale: 100 rules, 10 000 base facts, 100 atomic update batches,
    with recursion, two negation layers and multi-source facts;
  * an independent from-scratch reference (written in this file, no datalog
    engine/session code) is compared against the session every 25 batches;
  * evidence is verified bottom-up from live base facts every 25 batches;
  * determinism: the whole batch sequence is replayed on a fresh session
    and must produce identical facts and identical best derivations.

Run:  python -m unittest discover -s tests_extended -v
Expected wall time: about 1-3 minutes on a typical machine.
"""

import random
import time
import unittest

from datalog import Rule, Session, Var, neg, pos

X, Y, Z = Var("X"), Var("Y"), Var("Z")

N_COMPS = 500
COMP_SIZE = 5
NODES = N_COMPS * COMP_SIZE          # 2500 nodes
EDGES_PER_COMP = 15                  # 7500 edges -> 10 000 base facts total
N_BATCHES = 100


def comp_nodes(c):
    return [c * COMP_SIZE + k for k in range(COMP_SIZE)]


def initial_edges():
    """Deterministic per-component graph: a chain plus fixed chords."""
    edges = []
    for c in range(N_COMPS):
        ns = comp_nodes(c)
        for k in range(COMP_SIZE - 1):
            edges.append((ns[k], ns[k + 1]))                     # chain
        for k in range(COMP_SIZE):
            edges.append((ns[k], ns[(k + 2) % COMP_SIZE]))       # skip-1 chords
        for k in range(COMP_SIZE):
            edges.append((ns[k], ns[(k + 3) % COMP_SIZE]))       # skip-2 chords
        edges.append((ns[4], ns[0]))                             # one back edge
    # dedupe while keeping order deterministic
    seen, out = set(), []
    for a, b in edges:
        if (a, b) not in seen:
            seen.add((a, b))
            out.append((a, b))
    return out[:EDGES_PER_COMP * N_COMPS]


def build_program():
    """100 rules over 19 predicates: recursion + 2 negation layers + filters."""
    rules = [
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                         # 0
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),     # 1
    ]
    for i in range(5):                                                     # 2..11
        rules.append(Rule((f"c{i}", X, Y), [pos("edge", X, Y)]))
        rules.append(Rule((f"c{i}", X, Z), [pos(f"c{i}", X, Y), pos("edge", Y, Z)]))
    for i in range(5):                                                     # 12..16
        rules.append(Rule((f"u{i}", X), [pos("node", X), neg(f"c{i}", X, X)]))
    for i in range(5):                                                     # 17..21
        rules.append(Rule((f"v{i}", X), [pos("node", X), neg(f"u{i}", X)]))
    for i in range(78):                                                    # 22..99
        rules.append(Rule(("sel", X), [pos("node", X), pos("edge", X, (i * 37) % NODES)]))
    assert len(rules) == 100
    return rules


def initial_facts():
    facts = [(("node", n), "init") for n in range(NODES)]
    facts += [(("edge", a, b), "init") for a, b in initial_edges()]
    return facts


def make_batches(seed=42):
    """Deterministic batch sequence: each batch touches 2 components."""
    rng = random.Random(seed)
    live = [("edge", a, b) for a, b in initial_edges()]
    live_set = set(live)
    batches = []
    for b in range(N_BATCHES):
        comps = rng.sample(range(N_COMPS), 2)
        cand = [f for f in live if f[1] // COMP_SIZE in comps]
        rems = rng.sample(cand, min(3, len(cand)))
        for f in rems:
            live.remove(f)
            live_set.discard(f)
        adds = []
        for c in comps:
            ns = comp_nodes(c)
            for _ in range(3):
                a, z = rng.choice(ns), rng.choice(ns)
                if a != z and ("edge", a, z) not in live_set:
                    adds.append(("edge", a, z))
                    live.append(("edge", a, z))
                    live_set.add(("edge", a, z))
        batches.append(([(f, f"b{b}") for f in adds], [(f, "init") for f in rems]))
    return batches


# ---------------------------------------------------------------------------
# Independent from-scratch reference (no datalog engine/session code).
# Semi-naive per stratum with a per-argument index; least model only.
# ---------------------------------------------------------------------------

def _ref_stratify(preds, rules):
    stratum = {p: 0 for p in preds}
    for _ in range(len(preds) + 1):
        changed = False
        for r in rules:
            for lit in r.body:
                need = stratum[lit.atom[0]] + (0 if lit.positive else 1)
                if need > stratum[r.head[0]]:
                    stratum[r.head[0]] = need
                    changed = True
        if not changed:
            return stratum
    raise AssertionError("reference: unstratifiable")


def reference_model(base_facts, rules):
    preds = set()
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = _ref_stratify(preds, rules)
    store = {}
    index = {}

    def insert(f):
        s = store.setdefault(f[0], set())
        if f in s:
            return False
        s.add(f)
        idx = index.get(f[0])
        if idx is None:
            idx = [dict() for _ in range(len(f) - 1)]
            index[f[0]] = idx
        for j, a in enumerate(f[1:]):
            idx[j].setdefault(a, set()).add(f)
        return True

    for f in base_facts:
        insert(f)

    def fire(rule, delta):
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        out = set()

        def bt(i, env):
            if i == len(pos_lits):
                for lit in neg_lits:
                    g = (lit.atom[0],) + tuple(
                        env[a] if isinstance(a, Var) else a for a in lit.atom[1:]
                    )
                    if g in store.get(g[0], ()):
                        return
                out.add(
                    (rule.head[0],)
                    + tuple(env[a] if isinstance(a, Var) else a for a in rule.head[1:])
                )
                return
            atom = pos_lits[i].atom
            pred = atom[0]
            cand = None
            if delta is not None and pred in delta:
                cand = delta[pred]
            else:
                pidx = index.get(pred)
                if pidx is not None:
                    for j, a in enumerate(atom[1:]):
                        v = env.get(a) if isinstance(a, Var) else a
                        if isinstance(a, Var) and a not in env:
                            continue
                        bucket = pidx[j].get(v)
                        if bucket is not None and (cand is None or len(bucket) < len(cand)):
                            cand = bucket
            if cand is None:
                cand = store.get(pred, ())
            for fact in cand:
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

    levels = sorted({stratum[r.head[0]] for r in rules})
    for s in levels:
        srules = [r for r in rules if stratum[r.head[0]] == s]
        spreds = {r.head[0] for r in srules}
        delta = {p: set(store.get(p, ())) for p in spreds}
        first = True
        while True:
            if not first and not any(delta.values()):
                break
            new = set()
            for r in srules:
                d = delta if (not first or any(
                    l.atom[0] in spreds for l in r.body if l.positive)) else None
                for f in fire(r, d):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            delta = {}
            for f in new:
                insert(f)
                delta.setdefault(f[0], set()).add(f)
            first = False
    return {f for fs in store.values() for f in fs}


class TestMainScale(unittest.TestCase):
    """100 rules / 10 000 base facts / 100 batches — non-toy main scale."""

    def test_hundred_batches(self):
        rules = build_program()
        facts = initial_facts()
        self.assertEqual(len(facts), 10_000)
        self.assertEqual(len(rules), 100)
        batches = make_batches()
        self.assertEqual(len(batches), N_BATCHES)

        t0 = time.monotonic()
        s = Session(rules, facts=facts)
        t_init = time.monotonic() - t0

        tracker = {}
        for f, src in facts:
            tracker.setdefault(f, set()).add(src)

        t0 = time.monotonic()
        for b, (adds, rems) in enumerate(batches):
            rep = s.apply_batch(add=adds, remove=rems)
            self.assertTrue(rep.complete, f"batch {b}: {rep.reason}")
            for f, src in rems:
                tracker[f].discard(src)
                if not tracker[f]:
                    del tracker[f]
            for f, src in adds:
                tracker.setdefault(f, set()).add(src)
            if (b + 1) % 25 == 0:
                expected = reference_model(list(tracker), rules)
                self.assertEqual(
                    s.current_facts(), expected, f"mismatch after batch {b + 1}"
                )
                ok, problems = s.snapshot().verify_all()
                self.assertTrue(ok, f"batch {b + 1}: {problems[:3]}")
        t_batches = time.monotonic() - t0

        stats = s.stats()
        self.assertGreater(stats["derived_facts"], 50_000)  # non-degenerate
        print(
            f"\n[scale] init={t_init:.2f}s  100 batches={t_batches:.2f}s  "
            f"facts={stats['total_facts']}  nodes={stats['derivation_nodes']}"
        )

        # determinism: replay the identical sequence on a fresh session
        s2 = Session(rules, facts=facts)
        for adds, rems in batches:
            s2.apply_batch(add=adds, remove=rems)
        self.assertEqual(s.current_facts(), s2.current_facts())
        snap1, snap2 = s.snapshot(), s2.snapshot()
        self.assertEqual(snap1._derivations, snap2._derivations)


class TestNegationWorkSetsAtScale(unittest.TestCase):
    """The negation strata recompute only when their inputs actually change."""

    def test_quiet_batch_skips_negation_recompute(self):
        rules = build_program()
        facts = initial_facts()
        s = Session(rules, facts=facts)
        # a batch that only adds a node with no edges: reach/c*i unchanged,
        # but node/1 changes -> unreach-family strata ARE affected.
        rep = s.apply_batch(add=[(("node", NODES + 1), "x")])
        self.assertTrue(rep.complete)
        # a batch touching nothing at all: no stratum may do any work
        rep2 = s.apply_batch(add=[], remove=[])
        self.assertEqual(rep2.strata, ())
        self.assertEqual(rep2.affected_facts, frozenset())


if __name__ == "__main__":
    unittest.main()
