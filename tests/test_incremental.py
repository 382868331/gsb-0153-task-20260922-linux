"""Fast regression tests for the incremental Session API.

Expected values come from an *independent* from-scratch reference
(``reference_model`` below — a naive stratified least-model evaluator that
shares no code with the engine or the session) plus hand-computed small
cases.  Nothing here calls the solver under test to compute expectations.
"""

import random
import unittest

from datalog import (
    LimitError,
    Session,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
    Rule,
    Var,
    neg,
    pos,
)

X, Y, Z, W = Var("X"), Var("Y"), Var("Z"), Var("W")


# ---------------------------------------------------------------------------
# Independent per-batch reference: least model computed from scratch over the
# currently live base facts (a fact is live iff >= 1 source supports it).
# ---------------------------------------------------------------------------

def reference_model(base_facts, rules):
    preds = set()
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
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
            break
    store = {}
    for f in base_facts:
        store.setdefault(f[0], set()).add(f)

    def fire(rule):
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

    for s in sorted({stratum[r.head[0]] for r in rules}):
        srules = [r for r in rules if stratum[r.head[0]] == s]
        while True:
            new = set()
            for r in srules:
                for f in fire(r):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            for f in new:
                store.setdefault(f[0], set()).add(f)
    return {f for fs in store.values() for f in fs}


def live_facts(sources):
    return {f for f, srcs in sources.items() if srcs}


class SourceTracker:
    """Mirror of the multi-source base-fact map, maintained by the test."""

    def __init__(self):
        self.sources = {}

    def apply(self, add, remove):
        for fact, src in remove:
            s = self.sources.get(fact)
            if s:
                s.discard(src)
                if not s:
                    del self.sources[fact]
        for fact, src in add:
            self.sources.setdefault(fact, set()).add(src)


REACH_RULES = [
    Rule(("reach", X, Y), [pos("edge", X, Y)]),
    Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
]


class TestMultiSource(unittest.TestCase):
    """One fact, several source ids: removing one source changes nothing."""

    def test_second_source_keeps_everything(self):
        s = Session(
            REACH_RULES,
            facts=[
                (("edge", "a", "b"), "s1"),
                (("edge", "a", "b"), "s2"),
                (("edge", "b", "c"), "s1"),
            ],
        )
        before = s.current_facts()
        rep = s.apply_batch(remove=[(("edge", "a", "b"), "s1")])
        self.assertTrue(rep.complete)
        self.assertEqual(rep.removed_facts, frozenset())
        self.assertEqual(rep.added_facts, frozenset())
        self.assertEqual(s.current_facts(), before)
        self.assertEqual(rep.strata, ())  # no stratum was even touched

    def test_last_source_removal_cascades(self):
        s = Session(
            REACH_RULES,
            facts=[
                (("edge", "a", "b"), "s1"),
                (("edge", "a", "b"), "s2"),
                (("edge", "b", "c"), "s1"),
            ],
        )
        rep = s.apply_batch(remove=[(("edge", "a", "b"), "s1"), (("edge", "a", "b"), "s2")])
        self.assertEqual(
            rep.removed_facts,
            frozenset({("edge", "a", "b"), ("reach", "a", "b"), ("reach", "a", "c")}),
        )
        self.assertEqual(
            rep.retraction_reasons[("edge", "a", "b")][0], "base-lost-source"
        )
        self.assertEqual(
            rep.retraction_reasons[("reach", "a", "c")][0], "support-retracted"
        )
        for f in rep.removed_facts:
            self.assertTrue(s.verify_retraction(f), f)
        snap = s.snapshot()
        ok, problems = snap.verify_all()
        self.assertTrue(ok, problems)

    def test_ignored_removal_is_reported_not_an_error(self):
        s = Session(REACH_RULES, facts=[(("edge", "a", "b"), "s1")])
        rep = s.apply_batch(remove=[(("edge", "a", "b"), "no-such-source")])
        self.assertTrue(rep.complete)
        self.assertEqual(rep.ignored_removals, ((("edge", "a", "b"), "no-such-source"),))
        self.assertIn(("edge", "a", "b"), s.current_facts())


class TestTwoIndependentPaths(unittest.TestCase):
    """A fact derived by two independent paths survives losing one of them."""

    def test_redundant_derivation_paths(self):
        rules = [
            Rule(("p", X), [pos("e1", X)]),
            Rule(("p", X), [pos("e2", X)]),
            Rule(("q", X), [pos("p", X)]),
        ]
        s = Session(rules, facts=[(("e1", 1), "src"), (("e2", 1), "src")])
        self.assertIn(("q", 1), s.current_facts())
        rep = s.apply_batch(remove=[(("e1", 1), "src")])
        self.assertEqual(rep.removed_facts, frozenset({("e1", 1)}))
        self.assertIn(("p", 1), s.current_facts())
        self.assertIn(("q", 1), s.current_facts())
        # The over-deleted facts were recovered: they appear in the rederive
        # work set, not in the deleted one.
        st = rep.strata[0]
        self.assertEqual(st.deleted, frozenset())
        self.assertEqual(st.rederived, frozenset({("p", 1), ("q", 1)}))
        rep2 = s.apply_batch(remove=[(("e2", 1), "src")])
        self.assertEqual(
            rep2.removed_facts,
            frozenset({("e2", 1), ("p", 1), ("q", 1)}),
        )
        for f in rep2.removed_facts:
            self.assertTrue(s.verify_retraction(f), f)


class TestCycleNoSelfSupport(unittest.TestCase):
    """A pure recursive cycle must not keep itself alive by support counting."""

    def test_cycle_collapses_when_base_support_removed(self):
        rules = [
            Rule(("p", X), [pos("e", X)]),
            Rule(("p", X), [pos("q", X)]),
            Rule(("q", X), [pos("p", X)]),
        ]
        s = Session(rules, facts=[(("e", 1), "s")])
        self.assertEqual(s.current_facts() - {("e", 1)}, {("p", 1), ("q", 1)})
        rep = s.apply_batch(remove=[(("e", 1), "s")])
        self.assertEqual(
            rep.removed_facts, frozenset({("e", 1), ("p", 1), ("q", 1)})
        )
        self.assertTrue(s.verify_retraction(("p", 1)))
        self.assertTrue(s.verify_retraction(("q", 1)))
        # And the cycle can be rebuilt later from fresh support.
        rep2 = s.apply_batch(add=[(("e", 1), "s2")])
        self.assertIn(("p", 1), s.current_facts())
        self.assertIn(("q", 1), s.current_facts())
        ok, problems = s.snapshot().verify_all()
        self.assertTrue(ok, problems)

    def test_cycle_with_two_supports_loses_one(self):
        rules = [
            Rule(("p", X), [pos("e", X)]),
            Rule(("p", X), [pos("q", X)]),
            Rule(("q", X), [pos("p", X)]),
        ]
        s = Session(rules, facts=[(("e", 1), "s1"), (("e", 1), "s2")])
        rep = s.apply_batch(remove=[(("e", 1), "s1")])
        self.assertEqual(rep.removed_facts, frozenset())
        self.assertIn(("p", 1), s.current_facts())


class TestNegationFlip(unittest.TestCase):
    """Negation strata flip in both directions as lower strata change."""

    RULES = REACH_RULES + [
        Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),
        Rule(("bonus", X), [pos("node", X), neg("unreach", X)]),
    ]

    def test_flip_both_ways(self):
        s = Session(
            self.RULES,
            facts=[(("node", "a"), "n"), (("node", "b"), "n"), (("node", "c"), "n")],
        )
        self.assertEqual(
            {f[1] for f in s.current_facts() if f[0] == "unreach"}, {"a", "b", "c"}
        )
        rep = s.apply_batch(add=[(("edge", "a", "b"), "e")])
        self.assertIn(("unreach", "b"), rep.removed_facts)
        self.assertIn(("bonus", "b"), rep.added_facts)
        self.assertEqual(
            rep.retraction_reasons[("unreach", "b")][0], "negation-stratum-recomputed"
        )
        # negation strata were recomputed, not DRed-maintained
        kinds = {st.kind: st.recomputed for st in rep.strata}
        self.assertTrue(kinds.get("negation"))
        self.assertTrue(s.verify_retraction(("unreach", "b")))
        # flip back
        rep2 = s.apply_batch(remove=[(("edge", "a", "b"), "e")])
        self.assertIn(("unreach", "b"), rep2.added_facts)
        self.assertIn(("bonus", "b"), rep2.removed_facts)
        self.assertEqual(
            s.current_facts(), reference_model(
                [("node", "a"), ("node", "b"), ("node", "c")], self.RULES
            )
        )


class TestIrrelevantSubgraphNotRecomputed(unittest.TestCase):
    """A large unrelated component must not be recomputed for a small change."""

    def test_work_sets_exclude_unrelated_rules_and_facts(self):
        n = 300
        facts = [(("big_edge", i, i + 1), "big") for i in range(n)]
        facts += [(("cyc_edge", i, (i + 1) % 5), "cyc") for i in range(5)]
        rules = [
            Rule(("big_reach", X, Y), [pos("big_edge", X, Y)]),          # 0
            Rule(("big_reach", X, Z), [pos("big_reach", X, Y), pos("big_edge", Y, Z)]),  # 1
            Rule(("cyc_reach", X, Y), [pos("cyc_edge", X, Y)]),          # 2
            Rule(("cyc_reach", X, Z), [pos("cyc_reach", X, Y), pos("cyc_edge", Y, Z)]),  # 3
        ]
        s = Session(rules, facts=facts)
        rep = s.apply_batch(
            remove=[(("cyc_edge", 0, 1), "cyc")],
            add=[(("cyc_edge", 4, 2), "cyc")],
        )
        self.assertTrue(rep.complete)
        work = set()
        rules_fired = set()
        for st in rep.strata:
            work |= st.deleted | st.rederived | st.inserted
            rules_fired |= st.rules_fired
        self.assertTrue(work, "the small change must produce some work")
        self.assertTrue(all(f[0].startswith("cyc") for f in work), work)
        self.assertLess(len(work), 60)
        self.assertTrue(rules_fired <= {2, 3}, rules_fired)
        # the big component is bit-identical
        big = {f for f in s.current_facts() if f[0].startswith("big")}
        self.assertEqual(len(big), n + n * (n + 1) // 2)


class TestRuleReplacement(unittest.TestCase):
    def test_replacement_publishes_atomically_and_keeps_old_snapshots(self):
        s = Session([Rule(("p", X), [pos("e", X)])], facts=[(("e", 1), "a"), (("e", 2), "a")])
        old_snap = s.snapshot()
        self.assertEqual(old_snap.rule_version, 0)
        rep = s.replace_rules([
            Rule(("p", X), [pos("e", X)]),
            Rule(("q", X), [pos("p", X)]),
        ])
        self.assertTrue(rep.complete)
        self.assertTrue(rep.rules_replaced)
        self.assertEqual(rep.rule_version, 1)
        self.assertEqual(rep.added_facts, frozenset({("q", 1), ("q", 2)}))
        # old snapshot still answers for version 0 and cannot see new sources
        self.assertEqual(old_snap.query("q", X), [])
        self.assertEqual([f[1] for f in old_snap.query("p", X)], [1, 2])
        s.apply_batch(add=[(("e", 3), "brand-new-source")])
        self.assertNotIn(("e", 3), old_snap.sources)
        self.assertNotIn("brand-new-source", {src for ss in old_snap.sources.values() for src in ss})
        ok, problems = old_snap.verify_all()
        self.assertTrue(ok, problems)
        ok, problems = s.snapshot().verify_all()
        self.assertTrue(ok, problems)

    def test_retraction_reasons_after_replacement(self):
        s = Session([Rule(("p", X), [pos("e", X)])], facts=[(("e", 1), "a")])
        rep = s.replace_rules([Rule(("r", X), [pos("e", X)])])
        self.assertEqual(rep.removed_facts, frozenset({("p", 1)}))
        self.assertEqual(rep.retraction_reasons[("p", 1)], ("rules-replaced", 0))
        self.assertTrue(s.verify_retraction(("p", 1)))

    def test_invalid_replacement_is_rejected_and_keeps_old_rules(self):
        s = Session([Rule(("p", X), [pos("e", X)])], facts=[(("e", 1), "a")])
        before = s.current_facts()
        with self.assertRaises(UnsafeRuleError):
            s.replace_rules([Rule(("p", X), [pos("e", Y)])])
        with self.assertRaises(StratificationError):
            s.replace_rules([Rule(("p", X), [pos("e", X), neg("p", X)])])
        self.assertEqual(s.current_facts(), before)
        self.assertEqual(s.rule_version, 0)
        # session still fully usable
        rep = s.apply_batch(add=[(("e", 2), "a")])
        self.assertTrue(rep.complete)
        self.assertIn(("p", 2), s.current_facts())


class TestAtomicity(unittest.TestCase):
    def test_invalid_entry_aborts_whole_batch(self):
        s = Session(REACH_RULES, facts=[(("edge", "a", "b"), "s")])
        before = s.current_facts()
        with self.assertRaises(ValidationError):
            s.apply_batch(add=[(("edge", "b", "c"), "s"), (("edge", float("nan"), "d"), "s")])
        self.assertEqual(s.current_facts(), before)

    def test_budget_rejection_rolls_back_everything(self):
        facts = [(("edge", i, i + 1), "s") for i in range(6)]
        s = Session(REACH_RULES, facts=facts[:3])
        before = s.current_facts()
        rep = s.apply_batch(add=facts[3:], rules=None)
        self.assertTrue(rep.complete)
        # now force a budget failure: shrink is impossible, so build a tight session
        s2 = Session(REACH_RULES, facts=facts[:3], max_facts=14)
        before2 = s2.current_facts()
        nodes_before = s2.stats()["derivation_nodes"]
        rep2 = s2.apply_batch(add=facts[3:])
        self.assertFalse(rep2.complete)
        self.assertIn("budget", rep2.reason)
        self.assertEqual(s2.current_facts(), before2)
        self.assertEqual(s2.stats()["derivation_nodes"], nodes_before)
        # and the session still works afterwards
        rep3 = s2.apply_batch(add=[(("edge", 3, 4), "s")])
        self.assertTrue(rep3.complete)

    def test_budget_rejection_on_rule_replacement(self):
        s = Session([Rule(("p", X), [pos("e", X)])], facts=[(("e", i), "s") for i in range(3)],
                    max_facts=7)
        before = s.current_facts()
        rep = s.replace_rules([
            Rule(("p", X), [pos("e", X)]),
            Rule(("q", X), [pos("p", X)]),
            Rule(("r", X), [pos("q", X)]),
        ])
        self.assertFalse(rep.complete)
        self.assertEqual(s.current_facts(), before)
        self.assertEqual(s.rule_version, 0)


class TestDeterminism(unittest.TestCase):
    def test_same_batches_same_result(self):
        def build():
            s = Session(
                TestNegationFlip.RULES,
                facts=[(("node", i), "n") for i in range(6)],
            )
            rng = random.Random(7)
            for b in range(12):
                adds = [(("edge", rng.randrange(6), rng.randrange(6)), f"b{b}")
                        for _ in range(3)]
                rems = [(("edge", rng.randrange(6), rng.randrange(6)), f"b{b - 4}")
                        for _ in range(2)] if b >= 4 else []
                s.apply_batch(add=adds, remove=rems)
            return s.snapshot()

        a, b = build(), build()
        self.assertEqual(a.facts, b.facts)
        self.assertEqual(a._derivations, b._derivations)


class TestRandomizedCrossCheck(unittest.TestCase):
    """Random add/remove sequences on a small domain with recursion *and*
    negation; every batch is checked against the from-scratch reference and
    the evidence checker."""

    RULES = [
        Rule(("reach", X, Y), [pos("edge", X, Y)]),
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        Rule(("unreach", X), [pos("node", X), neg("reach", 0, X)]),
        Rule(("bonus", X), [pos("node", X), neg("unreach", X)]),
    ]

    def test_random_batches_match_reference(self):
        consts = list(range(4))
        pool = [("edge", a, b) for a in consts for b in consts]
        pool += [("node", a) for a in consts]
        sources = ["s0", "s1", "s2"]
        for seed in range(30):
            rng = random.Random(1000 + seed)
            tracker = SourceTracker()
            s = Session(self.RULES)
            for batch in range(25):
                adds = [
                    (rng.choice(pool), rng.choice(sources))
                    for _ in range(rng.randrange(4))
                ]
                live = [(f, src) for f, ss in tracker.sources.items() for src in ss]
                rems = [rng.choice(live) for _ in range(min(len(live), rng.randrange(3)))]
                rep = s.apply_batch(add=adds, remove=rems)
                self.assertTrue(rep.complete, rep.reason)
                tracker.apply(adds, rems)
                expected = reference_model(live_facts(tracker.sources), self.RULES)
                snap = s.snapshot()
                self.assertEqual(
                    set(snap.facts), expected, f"seed {seed} batch {batch}"
                )
                ok, problems = snap.verify_all()
                self.assertTrue(ok, f"seed {seed} batch {batch}: {problems}")
                for f in rep.removed_facts:
                    self.assertTrue(
                        s.verify_retraction(f), f"seed {seed} batch {batch}: {f}"
                    )
                self.assertEqual(
                    set(rep.retraction_reasons), set(rep.removed_facts)
                )


if __name__ == "__main__":
    unittest.main()
