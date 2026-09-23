"""Fast regression tests for the incremental Session (v2 API).

Expected values come from hand-computed small cases and from the
independent naive reference in ``naive_reference.py`` (which shares no
code with the package under test).  The evidence checker is exercised
both as a verifier of good snapshots and — with deliberately corrupted
evidence — as a detector, proving it does not trust solver flags.
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
    verify_evidence,
    verify_retractions,
    verify_snapshot,
)
from naive_reference import SourceTracker, naive_least_model

X, Y, Z, W = Var("X"), Var("Y"), Var("Z"), Var("W")


def reach_rules():
    return [
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                     # 0
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),  # 1
    ]


class TestMultiSourceFacts(unittest.TestCase):
    """One fact, several sources: removing one source changes nothing."""

    def setUp(self):
        self.rules = reach_rules()
        self.s = Session(
            self.rules,
            facts=[
                (("edge", "a", "b"), "src1"),
                (("edge", "a", "b"), "src2"),
                (("edge", "b", "c"), "src1"),
            ],
        )

    def test_removing_one_of_two_sources_is_a_no_op(self):
        before = set(self.s.result().facts)
        rep = self.s.apply_batch(removes=[(("edge", "a", "b"), "src1")])
        self.assertTrue(rep.applied)
        self.assertEqual(rep.added, frozenset())
        self.assertEqual(rep.removed, frozenset())
        self.assertEqual(rep.over_deleted, frozenset())
        self.assertEqual(rep.firings, {})
        after = self.s.result()
        self.assertEqual(set(after.facts), before)
        self.assertEqual(after.sources(("edge", "a", "b")), frozenset({"src2"}))

    def test_removing_last_source_cascades(self):
        self.s.apply_batch(removes=[(("edge", "a", "b"), "src1")])
        rep = self.s.apply_batch(removes=[(("edge", "a", "b"), "src2")])
        self.assertEqual(
            rep.removed,
            frozenset({("edge", "a", "b"), ("reach", "a", "b"), ("reach", "a", "c")}),
        )
        # reach(b, c) survives: it never depended on edge(a, b).
        self.assertIn(("reach", "b", "c"), self.s.result().facts)

    def test_readding_source_restores_without_duplicates(self):
        self.s.apply_batch(removes=[(("edge", "a", "b"), "src1")])
        rep = self.s.apply_batch(adds=[(("edge", "a", "b"), "src1")])
        self.assertEqual(rep.added, frozenset())
        self.assertEqual(
            self.s.result().sources(("edge", "a", "b")), frozenset({"src1", "src2"})
        )

    def test_bare_fact_uses_default_source(self):
        s = Session([], facts=[("e", 1)])
        self.assertEqual(s.result().sources(("e", 1)), frozenset({"default"}))
        rep = s.apply_batch(removes=[("e", 1)])  # bare fact matches default source
        self.assertEqual(rep.removed, frozenset({("e", 1)}))


class TestTwoIndependentPaths(unittest.TestCase):
    """A fact derived two independent ways survives losing one path."""

    def setUp(self):
        self.rules = [
            Rule(("p", X), [pos("e", X)]),  # 0
            Rule(("p", X), [pos("f", X)]),  # 1
            Rule(("g", X), [pos("p", X)]),  # 2
        ]
        self.s = Session(
            self.rules, facts=[(("e", "a"), "s1"), (("f", "a"), "s2")]
        )

    def test_first_path_removed_fact_survives(self):
        rep = self.s.apply_batch(removes=[(("e", "a"), "s1")])
        # p(a) was tentatively deleted, then re-derived via f(a); g(a)
        # (downstream component) is never threatened since p(a) survived.
        self.assertEqual(rep.over_deleted, frozenset({("p", "a")}))
        self.assertEqual(rep.rederived, frozenset({("p", "a")}))
        self.assertEqual(rep.removed, frozenset({("e", "a")}))
        self.assertIn(("p", "a"), self.s.result().facts)
        self.assertIn(("g", "a"), self.s.result().facts)

    def test_both_paths_removed_fact_collapses(self):
        self.s.apply_batch(removes=[(("e", "a"), "s1")])
        rep = self.s.apply_batch(removes=[(("f", "a"), "s2")])
        self.assertEqual(
            rep.removed, frozenset({("f", "a"), ("p", "a"), ("g", "a")})
        )
        self.assertEqual(rep.rederived, frozenset())
        self.assertEqual(self.s.result().query("p", X), [])


class TestPureRecursiveCycle(unittest.TestCase):
    """A positive cycle must not keep itself alive after its base support
    is withdrawn (no self-support via support counts)."""

    def test_two_node_cycle_collapses(self):
        rules = [
            Rule(("p", X), [pos("e", X)]),  # 0
            Rule(("p", X), [pos("q", X)]),  # 1
            Rule(("q", X), [pos("p", X)]),  # 2
        ]
        s = Session(rules, facts=[(("e", "a"), "s1")])
        self.assertEqual(s.result().query("p", X), [("p", "a")])
        self.assertEqual(s.result().query("q", X), [("q", "a")])
        rep = s.apply_batch(removes=[(("e", "a"), "s1")])
        self.assertEqual(rep.over_deleted, frozenset({("p", "a"), ("q", "a")}))
        self.assertEqual(rep.rederived, frozenset())
        self.assertEqual(s.result().query("p", X), [])
        self.assertEqual(s.result().query("q", X), [])
        self.assertEqual(
            set(s.result().facts), naive_least_model(set(), rules)
        )

    def test_three_node_cycle_with_one_anchor(self):
        rules = [
            Rule(("a1", X), [pos("e", X)]),
            Rule(("a1", X), [pos("a3", X)]),
            Rule(("a2", X), [pos("a1", X)]),
            Rule(("a3", X), [pos("a2", X)]),
        ]
        s = Session(rules, facts=[(("e", 1), "s")])
        for p in ("a1", "a2", "a3"):
            self.assertEqual(s.result().query(p, X), [(p, 1)])
        s.apply_batch(removes=[(("e", 1), "s")])
        for p in ("a1", "a2", "a3"):
            self.assertEqual(s.result().query(p, X), [], p)

    def test_cycle_survives_when_one_alternative_remains(self):
        rules = [
            Rule(("p", X), [pos("e", X)]),  # 0
            Rule(("p", X), [pos("f", X)]),  # 1
            Rule(("p", X), [pos("q", X)]),  # 2
            Rule(("q", X), [pos("p", X)]),  # 3
        ]
        s = Session(rules, facts=[(("e", "a"), "s1"), (("f", "a"), "s2")])
        rep = s.apply_batch(removes=[(("e", "a"), "s1")])
        self.assertEqual(rep.rederived, frozenset({("p", "a"), ("q", "a")}))
        self.assertEqual(s.result().query("q", X), [("q", "a")])


class TestNegationStratumReversal(unittest.TestCase):
    """Negation strata flip in both directions as lower strata change."""

    def setUp(self):
        self.rules = reach_rules() + [
            Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),  # 2
        ]
        self.s = Session(
            self.rules,
            facts=[
                ("node", "a"), ("node", "b"), ("node", "z"),
                (("edge", "a", "b"), "s1"),
            ],
        )

    def test_unreach_initial(self):
        # No reflexive reach rule: a is not reachable from a either.
        self.assertEqual(
            self.s.result().query("unreach", X), [("unreach", "a"), ("unreach", "z")]
        )

    def test_negation_flips_off_on_addition(self):
        rep = self.s.apply_batch(adds=[(("edge", "b", "z"), "s2")])
        self.assertIn(("reach", "a", "z"), rep.added)
        self.assertIn(("unreach", "z"), rep.removed)
        self.assertEqual(self.s.result().query("unreach", X), [("unreach", "a")])
        self.assertIn("unreach", rep.recomputed)  # negation stratum recomputed

    def test_negation_flips_back_on_removal(self):
        self.s.apply_batch(adds=[(("edge", "b", "z"), "s2")])
        rep = self.s.apply_batch(removes=[(("edge", "b", "z"), "s2")])
        self.assertIn(("unreach", "z"), rep.added)
        self.assertEqual(
            self.s.result().query("unreach", X), [("unreach", "a"), ("unreach", "z")]
        )
        self.assertEqual(
            set(self.s.result().facts),
            naive_least_model(
                {("node", "a"), ("node", "b"), ("node", "z"), ("edge", "a", "b")},
                self.rules,
            ),
        )

    def test_two_layer_negation_reversal(self):
        rules = [
            Rule(("odd", X), [pos("node", X), neg("ok", X)]),
            Rule(("even", X), [pos("node", X), neg("odd", X)]),
        ]
        s = Session(rules, facts=[("node", 1), ("node", 2), (("ok", 1), "s")])
        self.assertEqual(s.result().query("odd", X), [("odd", 2)])
        self.assertEqual(s.result().query("even", X), [("even", 1)])
        s.apply_batch(removes=[(("ok", 1), "s")])
        self.assertEqual(s.result().query("odd", X), [("odd", 1), ("odd", 2)])
        self.assertEqual(s.result().query("even", X), [])
        s.apply_batch(adds=[(("ok", 2), "s2")])
        self.assertEqual(s.result().query("odd", X), [("odd", 1)])
        self.assertEqual(s.result().query("even", X), [("even", 2)])
        self.assertEqual(
            set(s.result().facts),
            naive_least_model({("node", 1), ("node", 2), ("ok", 2)}, rules),
        )


class TestUnrelatedComponentsNotRecomputed(unittest.TestCase):
    """A large irrelevant subgraph plus a small cyclic change: the
    irrelevant rules must not fire at all."""

    def test_irrelevant_rules_zero_firings(self):
        chains = 100  # 100 chains of 10 nodes: 900 edges, 5500 reach facts
        facts = []
        for c in range(chains):
            base = c * 10
            for i in range(9):
                facts.append((("big_edge", base + i, base + i + 1), "gen"))
        rules = [
            Rule(("big_reach", X, Y), [pos("big_edge", X, Y)]),                    # 0
            Rule(("big_reach", X, Z), [pos("big_reach", X, Y), pos("big_edge", Y, Z)]),  # 1
            Rule(("p", X), [pos("seed", X)]),   # 2
            Rule(("p", X), [pos("q", X)]),      # 3
            Rule(("q", X), [pos("p", X)]),      # 4
        ]
        s = Session(rules, facts=facts + [(("seed", "x"), "s1")])
        big_facts_before = s.result().query("big_reach", X, Y)
        self.assertEqual(len(big_facts_before), chains * 45)  # 9+8+...+1 per chain

        rep = s.apply_batch(removes=[(("seed", "x"), "s1")])
        # The small cycle collapsed; the big component did no work at all.
        self.assertEqual(rep.firings, {})
        self.assertEqual(rep.recomputed, frozenset())
        self.assertEqual(rep.removed, frozenset({("seed", "x"), ("p", "x"), ("q", "x")}))
        self.assertEqual(s.result().query("big_reach", X, Y), big_facts_before)

        rep = s.apply_batch(adds=[(("seed", "y"), "s2")])
        self.assertNotIn(0, rep.firings)  # big_reach rules untouched
        self.assertNotIn(1, rep.firings)
        self.assertEqual(rep.added, frozenset({("seed", "y"), ("p", "y"), ("q", "y")}))


class TestAtomicityAndValidation(unittest.TestCase):
    def setUp(self):
        self.s = Session(reach_rules(), facts=[(("edge", "a", "b"), "s1")])

    def test_invalid_batch_raises_and_changes_nothing(self):
        before = set(self.s.result().facts)
        for bad_adds in (
            [(("edge", "a",),)],               # arity mismatch
            [(("edge", "a", float("nan")),)],  # non-finite constant
            [(("edge", "a", True),)],          # bool constant
            [(("edge", "a", "b"), True)],      # bool source id
            [(("edge", "a", "b"), "")],        # empty source id
            [(("edge", "a", "b"), 1.5)],       # float source id
        ):
            with self.assertRaises(ValidationError, msg=bad_adds):
                self.s.apply_batch(adds=bad_adds)
            self.assertEqual(set(self.s.result().facts), before)

    def test_removing_absent_source_is_noop(self):
        rep = self.s.apply_batch(removes=[(("edge", "a", "b"), "nobody")])
        self.assertTrue(rep.applied)
        self.assertEqual(rep.removed, frozenset())
        self.assertIn(("edge", "a", "b"), self.s.result().facts)

    def test_budget_rejection_rolls_back_everything(self):
        s = Session(
            reach_rules(),
            facts=[(("edge", "a", "b"), "s1")],
            max_facts=4,  # currently 2 facts; budget allows 2 more
        )
        before = set(s.result().facts)
        rep = s.apply_batch(
            adds=[(("edge", "b", "c"), "s2"), (("edge", "c", "d"), "s3")]
        )
        self.assertFalse(rep.applied)
        self.assertIn("budget", rep.reason)
        self.assertEqual(set(s.result().facts), before)
        self.assertEqual(s.result().sources(("edge", "a", "b")), frozenset({"s1"}))
        with self.assertRaises(KeyError):
            s.result().sources(("edge", "b", "c"))

    def test_budget_exactly_sufficient_applies(self):
        s = Session(
            reach_rules(),
            facts=[(("edge", "a", "b"), "s1")],
            max_facts=5,
        )
        rep = s.apply_batch(adds=[(("edge", "b", "c"), "s2")])
        # 2 base + 3 derived (reach a-b, b-c, a-c) = exactly 5.
        self.assertTrue(rep.applied)
        self.assertEqual(len(s.result().facts), 5)

    def test_session_constructor_validation(self):
        with self.assertRaises(ValidationError):
            Session([], max_facts=True)
        with self.assertRaises(ValidationError):
            Session([], max_facts=-1)
        with self.assertRaises(UnsafeRuleError):
            Session([Rule(("p", X), [pos("e", Y)])])
        with self.assertRaises(StratificationError):
            Session([Rule(("p", X), [pos("e", X), neg("p", X)])])
        with self.assertRaises(LimitError):
            Session([], facts=[("e", i) for i in range(50_001)])


class TestRuleReplacement(unittest.TestCase):
    def setUp(self):
        self.rules_v1 = [
            Rule(("p", X), [pos("e", X)]),   # 0
            Rule(("q", X), [pos("p", X)]),   # 1
        ]
        self.s = Session(self.rules_v1, facts=[(("e", "a"), "s1"), (("e", "b"), "s2")])

    def test_publish_new_version(self):
        v2 = self.rules_v1 + [Rule(("r", X), [pos("q", X), neg("gone", X)])]
        rep = self.s.replace_rules(v2)
        self.assertTrue(rep.applied)
        self.assertEqual(rep.rules_version, 1)
        self.assertEqual(rep.added, frozenset({("r", "a"), ("r", "b")}))
        self.assertEqual(rep.removed, frozenset())
        self.assertEqual(self.s.result().rules_version, 1)

    def test_replacement_removes_facts_with_verifiable_reasons(self):
        v2 = [Rule(("p", X), [pos("e", X)])]  # drop the q rule
        rep = self.s.replace_rules(v2)
        self.assertEqual(
            rep.removed, frozenset({("q", "a"), ("q", "b")})
        )
        self.assertTrue(all(r.cause == "rules_replaced" for r in rep.retractions))
        chk = verify_retractions(
            self.s.rules, list(self.s.result().base_sources),
            [r.fact for r in rep.retractions],
        )
        self.assertTrue(chk.ok, chk.violations)

    def test_old_snapshot_keeps_old_version(self):
        old = self.s.result()
        v2 = self.rules_v1 + [Rule(("r", X), [pos("q", X)])]
        self.s.replace_rules(v2)
        self.s.apply_batch(adds=[(("e", "c"), "s3")])
        # The old snapshot is frozen: no r facts, no source s3, old rules.
        self.assertEqual(old.rules_version, 0)
        self.assertNotIn(("r", "a"), old.facts)
        self.assertNotIn(("e", "c"), old.facts)
        self.assertEqual(old.sources(("e", "a")), frozenset({"s1"}))
        self.assertEqual(len(old.rules), 2)
        d = old.derivation(("q", "a"))
        self.assertEqual(d.rule_index, 1)  # index into the OLD rule list
        chk = verify_snapshot(old)
        self.assertTrue(chk.ok, chk.violations)

    def test_rejected_replacements_keep_state(self):
        before = set(self.s.result().facts)
        with self.assertRaises(UnsafeRuleError):
            self.s.replace_rules([Rule(("p", X), [pos("e", Y)])])
        with self.assertRaises(StratificationError):
            self.s.replace_rules([Rule(("p", X), [pos("e", X), neg("p", X)])])
        with self.assertRaises(ValidationError):
            self.s.replace_rules([Rule(("e", X, Y), [pos("e", X)])])  # arity clash
        with self.assertRaises(LimitError):
            self.s.replace_rules([Rule(("p", 1), [])] * 501)
        self.assertEqual(set(self.s.result().facts), before)
        self.assertEqual(self.s.result().rules_version, 0)

    def test_budget_rejection_keeps_old_version(self):
        s = Session(self.rules_v1, facts=[(("e", "a"), "s1")], max_facts=3)
        before = set(s.result().facts)
        v2 = self.rules_v1 + [Rule(("r", X), [pos("q", X)])]
        rep = s.replace_rules(v2)  # would reach 4 facts > 3
        self.assertFalse(rep.applied)
        self.assertIn("budget", rep.reason)
        self.assertEqual(set(s.result().facts), before)
        self.assertEqual(s.result().rules_version, 0)


class TestBaseAndDerivedOverlap(unittest.TestCase):
    """A fact that is both asserted and derivable converts cleanly."""

    def test_base_removed_fact_survives_as_derived(self):
        rules = [Rule(("p", X), [pos("e", X)])]
        s = Session(rules, facts=[(("e", "a"), "s1"), (("p", "a"), "manual")])
        self.assertNotIn(("p", "a"), s.result().derived)  # it is a base fact
        rep = s.apply_batch(removes=[(("p", "a"), "manual")])
        self.assertEqual(rep.removed, frozenset())  # still true, now derived
        self.assertIn(("p", "a"), s.result().derived)
        self.assertEqual(s.result().sources(("p", "a")), frozenset())
        d = s.result().derivation(("p", "a"))
        self.assertEqual(d.rule_index, 0)

    def test_derived_fact_asserted_as_base(self):
        rules = [Rule(("p", X), [pos("e", X)])]
        s = Session(rules, facts=[(("e", "a"), "s1")])
        self.assertIn(("p", "a"), s.result().derived)
        rep = s.apply_batch(adds=[(("p", "a"), "manual")])
        self.assertEqual(rep.added, frozenset())  # truth unchanged
        self.assertNotIn(("p", "a"), s.result().derived)
        # Now removing the rule support leaves the base fact standing.
        s.apply_batch(removes=[(("e", "a"), "s1")])
        self.assertIn(("p", "a"), s.result().facts)
        self.assertEqual(s.result().sources(("p", "a")), frozenset({"manual"}))


class TestEvidenceChecker(unittest.TestCase):
    """The checker recomputes everything from public snapshot data."""

    def setUp(self):
        self.rules = reach_rules() + [
            Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),
        ]
        self.s = Session(
            self.rules,
            facts=[
                ("node", "a"), ("node", "b"), ("node", "z"),
                (("edge", "a", "b"), "s1"),
            ],
        )

    def test_valid_snapshots_pass(self):
        self.assertTrue(verify_snapshot(self.s.result()).ok)
        self.s.apply_batch(adds=[(("edge", "b", "z"), "s2")])
        self.assertTrue(verify_snapshot(self.s.result()).ok)
        self.s.apply_batch(removes=[(("edge", "a", "b"), "s1")])
        self.assertTrue(verify_snapshot(self.s.result()).ok)

    def _snapshot_data(self):
        snap = self.s.result()
        return dict(
            facts=set(snap.facts),
            derived=set(snap.derived),
            base_sources=snap.base_sources,
            derivations={f: snap.derivation(f) for f in snap.derived},
            rules=snap.rules,
            max_facts=snap.max_facts,
        )

    def test_corrupted_child_is_detected(self):
        data = self._snapshot_data()
        f = ("reach", "a", "b")
        d = data["derivations"][f]
        data["derivations"][f] = type(d)(d.rule_index, (("edge", "a", "zzz"),))
        self.assertFalse(verify_evidence(**data).ok)

    def test_cyclic_only_evidence_is_detected(self):
        # p(a) and q(a) "support" each other with no base fact: the
        # checker must reject this even though every fact has a
        # recorded derivation.
        rules = [
            Rule(("p", X), [pos("q", X)]),
            Rule(("q", X), [pos("p", X)]),
        ]
        from datalog import Derivation
        data = dict(
            facts={("p", "a"), ("q", "a")},
            derived={("p", "a"), ("q", "a")},
            base_sources={},
            derivations={
                ("p", "a"): Derivation(0, (("q", "a"),)),
                ("q", "a"): Derivation(1, (("p", "a"),)),
            },
            rules=rules,
        )
        rep = verify_evidence(**data)
        self.assertFalse(rep.ok)
        self.assertTrue(any("well-founded" in v for v in rep.violations))

    def test_sourceless_base_fact_is_detected(self):
        data = self._snapshot_data()
        data["base_sources"][("node", "a")] = frozenset()
        self.assertFalse(verify_evidence(**data).ok)

    def test_satisfied_negation_is_detected(self):
        # Record a derivation for unreach(b) although reach(a, b) exists.
        from datalog import Derivation
        data = self._snapshot_data()
        data["derived"].add(("unreach", "b"))
        data["facts"].add(("unreach", "b"))
        data["derivations"][("unreach", "b")] = Derivation(2, (("node", "b"),))
        rep = verify_evidence(**data)
        self.assertFalse(rep.ok)
        self.assertTrue(any("negated" in v for v in rep.violations))

    def test_budget_violation_is_detected(self):
        data = self._snapshot_data()
        data["max_facts"] = len(data["facts"]) - 1
        self.assertFalse(verify_evidence(**data).ok)

    def test_verify_retractions_catches_lies(self):
        chk = verify_retractions(
            self.rules, [("node", "a"), ("edge", "a", "b")], [("reach", "a", "b")]
        )
        self.assertFalse(chk.ok)  # reach(a,b) is still derivable
        chk2 = verify_retractions(
            self.rules, [("node", "a"), ("edge", "a", "b")], [("reach", "b", "a")]
        )
        self.assertTrue(chk2.ok)


class TestSnapshotDeterminismAndImmutability(unittest.TestCase):
    def test_same_inputs_same_snapshot(self):
        def build():
            s = Session(
                reach_rules(),
                facts=[(("edge", "a", "b"), "s1"), (("edge", "b", "c"), "s2")],
            )
            s.apply_batch(adds=[(("edge", "c", "d"), "s3")])
            s.apply_batch(removes=[(("edge", "b", "c"), "s2")])
            return s.result()

        r1, r2 = build(), build()
        self.assertEqual(r1.facts, r2.facts)
        for f in r1.derived:
            self.assertEqual(r1.derivation(f), r2.derivation(f))

    def test_snapshot_is_frozen(self):
        s = Session(reach_rules(), facts=[(("edge", "a", "b"), "s1")])
        snap = s.result()
        s.apply_batch(removes=[(("edge", "a", "b"), "s1")])
        self.assertIn(("reach", "a", "b"), snap.facts)
        self.assertNotIn(("reach", "a", "b"), s.result().facts)
        self.assertEqual(snap.sources(("edge", "a", "b")), frozenset({"s1"}))

    def test_derivation_matches_hand_computed_best(self):
        # reach(a,c) has a depth-1 proof via bigedge and depth-2 proofs via
        # transitivity; the recorded derivation must be the shortest one.
        rules = [
            Rule(("reach", X, Y), [pos("bigedge", X, Y)]),  # 0
            Rule(("reach", X, Y), [pos("edge", X, Y)]),     # 1
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("reach", Y, Z)]),  # 2
        ]
        s = Session(
            rules,
            facts=[("edge", "a", "b"), ("edge", "b", "c"), ("bigedge", "a", "c")],
        )
        d = s.result().derivation(("reach", "a", "c"))
        self.assertEqual(d.rule_index, 0)
        self.assertEqual(d.children, (("bigedge", "a", "c"),))


class TestEvaluateConsistency(unittest.TestCase):
    """Supplementary round-trip consistency only: the session's static
    snapshot agrees with ``evaluate`` (the v1 solver) on facts and on the
    recorded best derivations.  This is NOT a source of expected values —
    those come from the independent naive reference and hand-computed
    cases above."""

    def test_session_snapshot_matches_evaluate(self):
        from datalog import evaluate
        from test_engine import TestRandomizedCrossCheck

        gen = TestRandomizedCrossCheck()._random_program
        for seed in range(120):
            facts, rules = gen(seed)
            r1 = evaluate(facts, rules)
            self.assertTrue(r1.complete)
            r2 = Session(rules, facts=facts).result()
            self.assertEqual(set(r1.facts), set(r2.facts), f"seed {seed}")
            for f in r1.derived:
                self.assertEqual(
                    r1.derivation(f), r2.derivation(f), f"seed {seed} {f}"
                )


class TestRandomSmallDomainSequences(unittest.TestCase):
    """Random add/remove sequences on a small domain with recursion AND
    negation; after every batch the session is compared against the
    independent from-scratch naive reference and the source tracker."""

    def _program(self, rng):
        consts = [0, 1, 2, "a", "b"]
        rules = [
            Rule(("r", X, Y), [pos("e", X, Y)]),
            Rule(("r", X, Z), [pos("r", X, Y), pos("e", Y, Z)]),
            Rule(("c", X), [pos("n", X), neg("r", 0, X)]),
            Rule(("d", X), [pos("n", X), neg("c", X)]),
            Rule(("m", X), [pos("p", X)]),
            Rule(("m", X), [pos("m", X)]),  # harmless self-cycle
        ]
        facts = []
        for _ in range(rng.randint(2, 6)):
            facts.append(("e", rng.choice(consts), rng.choice(consts)))
        for _ in range(rng.randint(1, 4)):
            facts.append(("n", rng.choice(consts)))
        for _ in range(rng.randint(0, 3)):
            facts.append(("p", rng.choice(consts)))
        return rules, facts

    def test_random_sequences_match_reference(self):
        for seed in range(40):
            rng = random.Random(seed)
            rules, facts = self._program(rng)
            ops = [(f, f"src{rng.randrange(3)}") for f in facts]
            tracker = SourceTracker()
            tracker.apply(adds=ops)
            s = Session(rules, facts=ops)
            consts = [0, 1, 2, "a", "b"]
            for batch_no in range(30):
                adds, removes = [], []
                for _ in range(rng.randint(0, 4)):
                    pred = rng.choice(["e", "n", "p"])
                    fact = (pred, rng.choice(consts)) if pred != "e" else (
                        "e", rng.choice(consts), rng.choice(consts)
                    )
                    src = f"src{rng.randrange(3)}"
                    (adds if rng.random() < 0.6 else removes).append((fact, src))
                rep = s.apply_batch(adds=adds, removes=removes)
                self.assertTrue(rep.applied, f"seed {seed} batch {batch_no}")
                tracker.apply(adds=adds, removes=removes)
                snap = s.result()
                expected = naive_least_model(tracker.facts(), rules)
                self.assertEqual(
                    set(snap.facts), expected,
                    f"seed {seed} batch {batch_no}: facts mismatch",
                )
                # Source validity: base facts and their source ids match
                # the independent tracker exactly.
                self.assertEqual(set(snap.base_sources), tracker.facts())
                for f, srcs in snap.base_sources.items():
                    self.assertEqual(set(srcs), tracker.sources[f])
                chk = verify_snapshot(snap)
                self.assertTrue(chk.ok, f"seed {seed} batch {batch_no}: {chk.violations}")
                # Every retraction the session reported is independently
                # confirmed non-derivable.
                if rep.removed:
                    chk_r = verify_retractions(
                        rules, list(tracker.facts()), rep.removed
                    )
                    self.assertTrue(
                        chk_r.ok,
                        f"seed {seed} batch {batch_no}: {chk_r.violations}",
                    )


if __name__ == "__main__":
    unittest.main()
