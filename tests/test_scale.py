"""Extended validation (scale) for the incremental Session.

Runs only when ``DATALOG_RUN_EXTENDED=1`` is set; the default
``python -m unittest discover -s tests -v`` stays the fast regression.

Scale (meets the required minimums): 100 rules, 10 000 base facts, 100
update batches over 25 independent subgraphs (each 100 nodes in 10
chains of 10, edges up to distance 4).  Every batch is checked with the
independent evidence checker; the full least model is recomputed from
scratch by the independent naive reference at checkpoints and compared
fact-for-fact; per batch the test asserts that rules of untouched
subgraphs did not fire at all (irrelevant work is not recomputed).
"""

import os
import random
import time
import unittest

from datalog import Rule, Session, Var, neg, pos, verify_snapshot
from naive_reference import SourceTracker, naive_least_model

X, Y, Z = Var("X"), Var("Y"), Var("Z")

EXTENDED = os.environ.get("DATALOG_RUN_EXTENDED") == "1"

SUBGRAPHS = 25
NODES = 100          # per subgraph: 10 chains of 10 nodes
CHAIN = 10
MAX_DIST = 4         # edges (i, j) with 0 < j - i <= 4 inside a chain


def build_program():
    """100 rules over 100 predicates (4 per subgraph)."""
    rules = []
    for g in range(SUBGRAPHS):
        start = g * NODES
        rules += [
            Rule((f"reach{g}", X, Y), [pos(f"edge{g}", X, Y)]),
            Rule((f"reach{g}", X, Z), [pos(f"reach{g}", X, Y), pos(f"edge{g}", Y, Z)]),
            Rule((f"reach{g}", X, X), [pos(f"node{g}", X)]),
            Rule((f"unreach{g}", X), [pos(f"node{g}", X), neg(f"reach{g}", start, X)]),
        ]
    return rules


def initial_facts():
    """10 000 base facts: 2 500 node facts + 7 500 edge facts."""
    facts = []
    for g in range(SUBGRAPHS):
        for n in range(NODES):
            facts.append(((f"node{g}", g * NODES + n), "gen"))
        for c in range(NODES // CHAIN):
            base = g * NODES + c * CHAIN
            for i in range(CHAIN):
                for d in range(1, MAX_DIST + 1):
                    if i + d < CHAIN:
                        facts.append(((f"edge{g}", base + i, base + i + d), "gen"))
    return facts


def make_batch(rng, batch_no):
    """One deterministic batch touching 2-3 subgraphs."""
    adds, removes = [], []
    touched = set()
    for _ in range(rng.randint(2, 3)):
        g = rng.randrange(SUBGRAPHS)
        touched.add(g)
        for _ in range(rng.randint(1, 4)):
            c = rng.randrange(NODES // CHAIN)
            base = g * NODES + c * CHAIN
            i = rng.randrange(CHAIN)
            j = rng.randrange(CHAIN)
            if i == j:
                continue
            a, b = min(i, j), max(i, j)
            fact = (f"edge{g}", base + a, base + b)
            src = f"batch{batch_no % 7}"  # small source pool -> multi-source facts
            # Distance-1..4 edges already exist from the generator; those
            # removals test multi-source survival, others test real change.
            (adds if rng.random() < 0.55 else removes).append((fact, src))
    return adds, removes, touched


@unittest.skipUnless(EXTENDED, "extended validation: set DATALOG_RUN_EXTENDED=1")
class TestScale(unittest.TestCase):
    def test_100_rules_10000_facts_100_batches(self):
        rules = build_program()
        self.assertEqual(len(rules), 100)
        facts = initial_facts()
        self.assertEqual(len(facts), 10_000)

        tracker = SourceTracker()
        tracker.apply(adds=facts)
        t0 = time.monotonic()
        s = Session(rules, facts=facts)
        build_s = time.monotonic() - t0

        snap0 = s.result()
        expected0 = naive_least_model(tracker.facts(), rules)
        self.assertEqual(set(snap0.facts), expected0)

        rng = random.Random(20260924)
        t0 = time.monotonic()
        for batch_no in range(100):
            adds, removes, touched = make_batch(rng, batch_no)
            rep = s.apply_batch(adds=adds, removes=removes)
            self.assertTrue(rep.applied, f"batch {batch_no}: {rep.reason}")
            tracker.apply(adds=adds, removes=removes)

            # Irrelevant rules did not fire: only rules of touched
            # subgraphs may appear in the firing counts.
            touched_rules = {4 * g + k for g in touched for k in range(4)}
            for ri in rep.firings:
                self.assertIn(
                    ri, touched_rules,
                    f"batch {batch_no}: rule {ri} of an untouched subgraph fired",
                )
            # Evidence holds bottom-up after every batch.
            snap = s.result()
            chk = verify_snapshot(snap)
            self.assertTrue(chk.ok, f"batch {batch_no}: {chk.violations[:3]}")
            # Source bookkeeping matches the independent tracker.
            self.assertEqual(set(snap.base_sources), tracker.facts())
            for f, srcs in snap.base_sources.items():
                self.assertEqual(set(srcs), tracker.sources[f])
            # Full from-scratch comparison at checkpoints.
            if batch_no % 20 == 19:
                expected = naive_least_model(tracker.facts(), rules)
                self.assertEqual(set(snap.facts), expected, f"batch {batch_no}")
        batches_s = time.monotonic() - t0

        expected = naive_least_model(tracker.facts(), rules)
        self.assertEqual(set(s.result().facts), expected)
        print(
            f"\n[scale] build={build_s:.2f}s  100 batches={batches_s:.2f}s  "
            f"final facts={len(expected)}",
            flush=True,
        )

    def test_large_irrelevant_subgraph_with_small_cycle_edit(self):
        """9 000+ facts in one big component; a tiny 2-rule cycle is
        edited; the big component's rules must not fire at all."""
        chains = 500  # 500 chains of 10 -> 4500 edges, 22500 derived facts
        facts = []
        for c in range(chains):
            base = c * CHAIN
            for i in range(CHAIN - 1):
                facts.append((("big_edge", base + i, base + i + 1), "gen"))
        rules = [
            Rule(("big_reach", X, Y), [pos("big_edge", X, Y)]),                      # 0
            Rule(("big_reach", X, Z), [pos("big_reach", X, Y), pos("big_edge", Y, Z)]),  # 1
            Rule(("p", X), [pos("seed", X)]),  # 2
            Rule(("p", X), [pos("q", X)]),     # 3
            Rule(("q", X), [pos("p", X)]),     # 4
        ]
        s = Session(rules, facts=facts + [(("seed", "x"), "s1")])
        before = s.result().query("big_reach", X, Y)
        self.assertEqual(len(before), chains * 45)

        t0 = time.monotonic()
        rep = s.apply_batch(removes=[(("seed", "x"), "s1")])
        dt = time.monotonic() - t0
        self.assertEqual(rep.firings, {})          # zero rule firings (pure DRed)
        self.assertEqual(rep.recomputed, frozenset())
        self.assertEqual(
            rep.removed, frozenset({("seed", "x"), ("p", "x"), ("q", "x")})
        )
        self.assertEqual(rep.rederived, frozenset())  # cycle cannot self-support
        self.assertEqual(s.result().query("big_reach", X, Y), before)
        self.assertLess(dt, 5.0, "small edit must not recompute the big component")
        print(f"\n[scale] small cycle edit over 22k-fact component: {dt:.4f}s",
              flush=True)


if __name__ == "__main__":
    unittest.main()
