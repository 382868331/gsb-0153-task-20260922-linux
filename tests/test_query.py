"""Tests for demand-driven query evaluation (evaluate_query).

Consistency expectations are checked against the module's own full
evaluation (``evaluate``) — the requirement is that focused query results
and their provenance are identical to full evaluation while irrelevant
derivations are skipped — plus hand-computed small cases.
"""

import unittest

from datalog import (
    EvalStats,
    FocusStats,
    Rule,
    ValidationError,
    Var,
    evaluate,
    evaluate_query,
    neg,
    pos,
)

from test_engine import TestRandomizedCrossCheck

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def graph_program():
    """Reachability + stratified negation, plus an irrelevant sub-program."""
    facts = (
        [("node", n) for n in range(6)]
        + [("edge", 0, 1), ("edge", 1, 2), ("edge", 2, 3), ("edge", 0, 4)]
        + [("color", "red"), ("color", "blue"), ("paint", "red", 1)]
    )
    rules = [
        Rule(("reach", X, X), [pos("node", X)]),                     # 0
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                  # 1
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),  # 2
        Rule(("unreach", X), [pos("node", X), neg("reach", 0, X)]),  # 3
        # Irrelevant to reach/unreach queries:
        Rule(("hue", X), [pos("color", X)]),                         # 4
        Rule(("shade", X), [pos("hue", X), pos("paint", X, Y)]),     # 5
    ]
    return facts, rules


class TestFocusedQuery(unittest.TestCase):
    def test_answers_match_full_evaluation(self):
        facts, rules = graph_program()
        full = evaluate(facts, rules)
        foc = evaluate_query(facts, rules, "reach", 0, X)
        self.assertTrue(foc.complete)
        self.assertEqual(foc.answers, full.query("reach", 0, X))
        self.assertEqual([f[2] for f in foc.answers], [0, 1, 2, 3, 4])

    def test_irrelevant_rules_and_facts_pruned(self):
        facts, rules = graph_program()
        foc = evaluate_query(facts, rules, "reach", 0, X)
        focus = foc.focus
        self.assertIsInstance(focus, FocusStats)
        self.assertEqual(focus.query_pred, "reach")
        self.assertEqual(focus.pattern, (0, X))
        self.assertEqual(focus.relevant_predicates, {"reach", "node", "edge"})
        self.assertEqual(focus.pruned_rules, (3, 4, 5))
        self.assertEqual(focus.pruned_facts, 3)  # color/paint facts dropped
        # The focused model is exactly the relevant slice of the full model.
        full = evaluate(facts, rules)
        self.assertEqual(
            set(foc.facts),
            {f for f in full.facts if f[0] in focus.relevant_predicates},
        )

    def test_reduction_is_recorded(self):
        facts, rules = graph_program()
        foc = evaluate_query(facts, rules, "reach", 0, X, measure_baseline=True)
        self.assertIsInstance(foc.stats, EvalStats)
        avoided = foc.focus.derivations_avoided
        self.assertIsNotNone(avoided)
        self.assertGreater(avoided, 0)
        full = evaluate(facts, rules)
        self.assertEqual(avoided, full.stats.firings - foc.stats.firings)
        # Without measure_baseline the cheap path leaves it unknown.
        cheap = evaluate_query(facts, rules, "reach", 0, X)
        self.assertIsNone(cheap.focus.derivations_avoided)

    def test_provenance_identical_to_full_evaluation(self):
        facts, rules = graph_program()
        full = evaluate(facts, rules)
        foc = evaluate_query(facts, rules, "reach", 0, X)
        for fact in foc.answers:
            self.assertEqual(foc.derivation(fact), full.derivation(fact))
            self.assertEqual(foc.explain(fact), full.explain(fact))


class TestNegationDependenciesKept(unittest.TestCase):
    def test_negative_dependency_not_pruned(self):
        facts, rules = graph_program()
        foc = evaluate_query(facts, rules, "unreach", X)
        # unreach depends (negatively) on reach: reach/node/edge stay.
        self.assertEqual(
            foc.focus.relevant_predicates,
            {"unreach", "reach", "node", "edge"},
        )
        self.assertEqual(foc.focus.pruned_rules, (4, 5))
        full = evaluate(facts, rules)
        self.assertEqual(foc.answers, full.query("unreach", X))
        self.assertEqual([f[1] for f in foc.answers], [5])

    def test_two_layer_negation_matches_full(self):
        facts = [("node", 1), ("node", 2), ("node", 3), ("ok", 1)]
        rules = [
            Rule(("odd", X), [pos("node", X), neg("ok", X)]),
            Rule(("even", X), [pos("node", X), neg("odd", X)]),
            Rule(("noise", X), [pos("node", X), pos("ok", X)]),  # irrelevant
        ]
        full = evaluate(facts, rules)
        foc = evaluate_query(facts, rules, "even", X)
        self.assertEqual(foc.answers, full.query("even", X))
        self.assertEqual([f[1] for f in foc.answers], [1])
        # "odd" decides the negation in even's body: must be kept.
        self.assertIn("odd", foc.focus.relevant_predicates)
        self.assertEqual(foc.focus.pruned_rules, (2,))


class TestFocusedEdgeCases(unittest.TestCase):
    def test_unknown_predicate_query(self):
        facts, rules = graph_program()
        foc = evaluate_query(facts, rules, "nope", X)
        self.assertTrue(foc.complete)
        self.assertEqual(foc.answers, [])
        self.assertEqual(set(foc.facts), set())

    def test_pattern_with_constants(self):
        facts, rules = graph_program()
        foc = evaluate_query(facts, rules, "reach", 0, 2)
        self.assertEqual(foc.answers, [("reach", 0, 2)])

    def test_pattern_validation(self):
        facts, rules = graph_program()
        with self.assertRaises(ValidationError):
            evaluate_query(facts, rules, "reach", float("nan"))
        with self.assertRaises(ValidationError):
            evaluate_query(facts, rules, "reach", True)
        with self.assertRaises(ValidationError):
            evaluate_query(facts, rules, 7, X)
        with self.assertRaises(ValidationError):
            evaluate_query(facts, rules, "reach", X, measure_baseline=1)
        with self.assertRaises(ValidationError):
            evaluate_query(facts, rules, "reach", X, max_facts=True)

    def test_budget_applies_to_focused_run(self):
        facts, rules = graph_program()
        # Budget smaller than the relevant input facts alone.
        foc = evaluate_query(facts, rules, "reach", 0, X, max_facts=2)
        self.assertFalse(foc.complete)
        self.assertIn("budget", foc.reason)
        # A budget that full evaluation would blow but the focused run
        # fits into is fine: pruning reduces the fact count.
        full = evaluate(facts, rules)
        exact = evaluate_query(facts, rules, "reach", 0, X)
        self.assertLess(len(exact.facts), len(full.facts))
        foc2 = evaluate_query(
            facts, rules, "reach", 0, X, max_facts=len(exact.facts)
        )
        self.assertTrue(foc2.complete)
        self.assertEqual(foc2.answers, full.query("reach", 0, X))

    def test_evaluate_exposes_stats(self):
        facts, rules = graph_program()
        res = evaluate(facts, rules)
        self.assertIsInstance(res.stats, EvalStats)
        self.assertGreater(res.stats.firings, 0)
        self.assertEqual(res.stats.derived, len(res.derived))
        self.assertGreaterEqual(res.stats.rounds, 1)
        self.assertIsNone(res.focus)


class TestFocusedVsFullRandomPrograms(unittest.TestCase):
    """Focused evaluation must agree with full evaluation on random
    stratified programs, for every predicate as the query target."""

    def test_random_programs(self):
        gen = TestRandomizedCrossCheck()._random_program
        for seed in range(120):
            facts, rules = gen(seed)
            full = evaluate(facts, rules)
            self.assertTrue(full.complete, f"seed {seed}: {full.reason}")
            preds = sorted({f[0] for f in full.facts})
            for pred in preds:
                arity = len(next(f for f in full.facts if f[0] == pred)) - 1
                pattern = [Var(f"V{i}") for i in range(arity)]
                foc = evaluate_query(facts, rules, pred, *pattern)
                self.assertTrue(foc.complete, f"seed {seed} {pred}")
                self.assertEqual(
                    foc.answers,
                    full.query(pred, *pattern),
                    f"seed {seed} pred {pred}: answers differ",
                )
                # Provenance of every derived relevant fact is identical.
                for fact in foc.derived:
                    self.assertEqual(
                        foc.derivation(fact),
                        full.derivation(fact),
                        f"seed {seed} pred {pred}: provenance of {fact}",
                    )
                # Focused model = relevant slice of the full model.
                self.assertEqual(
                    set(foc.facts),
                    {
                        f
                        for f in full.facts
                        if f[0] in foc.focus.relevant_predicates
                    },
                    f"seed {seed} pred {pred}: focused model slice",
                )
                # Firings never increase by focusing.
                self.assertLessEqual(foc.stats.firings, full.stats.firings)


if __name__ == "__main__":
    unittest.main()
