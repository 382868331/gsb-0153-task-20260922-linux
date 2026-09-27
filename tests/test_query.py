"""Tests for query-directed (demand-driven) evaluation, evaluate_query.

Soundness is checked against the *full* evaluator in the same package on
hand cases and on 150 deterministic random stratified programs: for every
generated query the demand-restricted result must contain exactly the full
model's facts matching the query, hold only facts the full model contains,
and record an identical shortest derivation for every materialised fact.
"""

import random
import unittest

from datalog import (
    ValidationError,
    Rule,
    Var,
    evaluate,
    evaluate_query,
    neg,
    pos,
)

X, Y, Z, W = Var("X"), Var("Y"), Var("Z"), Var("W")


def _matches(fact, pattern):
    pred, pargs = pattern[0], pattern[1:]
    if fact[0] != pred or len(fact) - 1 != len(pargs):
        return False
    env = {}
    for p, a in zip(pargs, fact[1:]):
        if isinstance(p, Var):
            if p in env:
                if env[p] != a:
                    return False
            else:
                env[p] = a
        elif p != a:
            return False
    return True


class TestTargeted(unittest.TestCase):
    def setUp(self):
        self.facts = [
            ("edge", "a", "b"), ("edge", "b", "c"), ("edge", "c", "d"),
            ("edge", "a", "e"),
            # disconnected component: irrelevant to a query about a
            ("edge", "x", "y"), ("edge", "y", "z"),
            ("node", "a"), ("node", "b"), ("node", "c"), ("node", "d"),
            ("node", "e"), ("node", "x"), ("node", "y"), ("node", "z"),
        ]
        self.rules = [
            Rule(("reach", X, X), [pos("node", X)]),
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
            Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),
        ]

    def test_answer_matches_full_and_prunes_inputs(self):
        full = evaluate(self.facts, self.rules)
        q = evaluate_query(self.facts, self.rules, ("reach", "a", X))
        self.assertTrue(q.complete)
        self.assertEqual(q.answer(), full.query("reach", "a", X))
        # node('a') is the only node fact the reflexive-rule demand needs;
        # node facts of the disconnected component are never loaded.
        self.assertIn(("node", "a"), q.facts)
        self.assertNotIn(("node", "x"), q.facts)
        self.assertNotIn(("node", "z"), q.facts)
        # No derived fact may mention the disconnected component.
        reach = [f for f in q.facts if f[0] == "reach"]
        self.assertFalse(any("x" in f[1:] or "z" in f[1:] for f in reach))
        self.assertLess(q.stats.input_facts_used, q.stats.input_facts_total)
        self.assertEqual(
            q.stats.input_facts_skipped,
            q.stats.input_facts_total - q.stats.input_facts_used,
        )

    def test_unreachable_rule_is_skipped(self):
        q = evaluate_query(self.facts, self.rules, ("reach", "a", X))
        self.assertEqual(q.query("unreach", X), [])
        # The unreach rule is never fired because unreach has no demand.
        self.assertEqual(q.stats.rules_used, q.stats.rules_total - 1)
        self.assertEqual(q.stats.rules_skipped, 1)

    def test_negation_decision_facts_are_not_pruned(self):
        full = evaluate(self.facts, self.rules)
        q = evaluate_query(self.facts, self.rules, ("unreach", X))
        self.assertEqual(q.answer(), full.query("unreach", X))
        # The x/y/z component is unreachable from a; getting this right
        # requires exactly the reach('a', n) facts the negation inspects,
        # so demand through the negated literal must keep them.
        self.assertEqual([f[1] for f in q.answer()], ["x", "y", "z"])

    def test_derivations_identical_to_full(self):
        full = evaluate(self.facts, self.rules)
        q = evaluate_query(self.facts, self.rules, [
            ("reach", "a", X), ("reach", X, "d"),
        ])
        for fact in q.facts:
            self.assertEqual(
                q.derivation(fact), full.derivation(fact), f"fact {fact!r}"
            )
        self.assertEqual(
            set(q.answer()),
            set(full.query("reach", "a", X))
            | set(full.query("reach", X, "d")),
        )

    def test_repeated_variable_in_pattern(self):
        facts = [("node", 1), ("node", 2)]
        rules = [Rule(("reach", X, X), [pos("node", X)])]
        full = evaluate(facts, rules)
        q = evaluate_query(facts, rules, ("reach", X, X))
        self.assertEqual(q.answer(), [("reach", 1, 1), ("reach", 2, 2)])
        for fact in q.facts:
            self.assertEqual(q.derivation(fact), full.derivation(fact))

    def test_edb_predicate_query(self):
        q = evaluate_query(self.facts, [], ("edge", "a", X))
        self.assertEqual(
            q.answer(), [("edge", "a", "b"), ("edge", "a", "e")]
        )
        self.assertEqual(q.stats.derived_facts, 0)
        self.assertEqual(q.stats.rules_used, 0)

    def test_unknown_predicate_gives_empty_answer(self):
        q = evaluate_query(self.facts, self.rules, ("ghost", X))
        self.assertTrue(q.complete)
        self.assertEqual(q.answer(), [])

    def test_bare_predicate_name_and_multiple_patterns(self):
        q1 = evaluate_query(self.facts, [], "node")
        q2 = evaluate_query(self.facts, [], [("node", X)])
        self.assertEqual(q1.answer(), q2.answer())
        self.assertEqual(len(q1.answer()), 8)

    def test_stats_summary_text(self):
        q = evaluate_query(self.facts, self.rules, ("reach", "a", X))
        s = q.stats.summary()
        self.assertIn("input facts used", s)
        self.assertIn("rules used", s)
        self.assertIn("pruned", s)
        self.assertGreaterEqual(q.stats.irrelevant_derivations_reduced, 0)

    def test_budget_overflow_is_incomplete_not_partial(self):
        q = evaluate_query(
            self.facts, self.rules, ("reach", "a", X), max_facts=8
        )
        self.assertFalse(q.complete)
        self.assertIn("budget", q.reason)


class TestQueryValidation(unittest.TestCase):
    def test_arity_mismatch(self):
        facts = [("e", 1)]
        with self.assertRaises(ValidationError) as cm:
            evaluate_query(facts, [], ("e", X, X))
        self.assertIn("query pattern 0", str(cm.exception))
        self.assertIn("arity", str(cm.exception))

    def test_bad_argument(self):
        with self.assertRaises(ValidationError):
            evaluate_query([("e", 1)], [], ("e", float("nan")))

    def test_empty_and_non_iterable(self):
        with self.assertRaises(ValidationError):
            evaluate_query([("e", 1)], [], [])
        with self.assertRaises(ValidationError):
            evaluate_query([("e", 1)], [], 42)

    def test_bad_max_facts(self):
        with self.assertRaises(ValidationError):
            evaluate_query([("e", 1)], [], ("e", X), max_facts=True)
        with self.assertRaises(ValidationError):
            evaluate_query([("e", 1)], [], ("e", X), max_facts=-3)


class TestRandomizedCrossCheck(unittest.TestCase):
    """Demand-restricted evaluation vs full evaluation on random programs."""

    def _program(self, rng):
        consts = [0, 1, 2, "a", "b", "c"]
        preds = [(f"p{i}", rng.randint(1, 2)) for i in range(rng.randint(2, 5))]
        facts = []
        for name, ar in preds:
            for _ in range(rng.randint(0, 5)):
                facts.append((name, *[rng.choice(consts) for _ in range(ar)]))
        rules = []
        for _ in range(rng.randint(1, 8)):
            hi = rng.randrange(len(preds))
            hname, har = preds[hi]
            body = []
            pos_vars = []
            for _ in range(rng.randint(1, 2)):
                bi = rng.randint(0, hi)
                bname, bar = preds[bi]
                args = []
                for _ in range(bar):
                    if rng.random() < 0.6:
                        v = Var(rng.choice("XYZW"))
                        args.append(v)
                        pos_vars.append(v)
                    else:
                        args.append(rng.choice(consts))
                body.append(pos(bname, *args))
            if hi > 0 and rng.random() < 0.5 and pos_vars:
                bi = rng.randint(0, hi - 1)
                bname, bar = preds[bi]
                args = [
                    rng.choice(pos_vars) if rng.random() < 0.7
                    else rng.choice(consts)
                    for _ in range(bar)
                ]
                body.append(neg(bname, *args))
            head_args = [
                rng.choice(pos_vars) if pos_vars and rng.random() < 0.7
                else rng.choice(consts)
                for _ in range(har)
            ]
            rules.append(Rule((hname, *head_args), body))
        return preds, facts, rules

    def _query_patterns(self, rng, preds):
        patterns = []
        for name, ar in rng.sample(preds, rng.randint(1, len(preds))):
            args = []
            for _ in range(ar):
                if rng.random() < 0.5:
                    args.append(rng.choice([0, 1, "a", "b", "c"]))
                else:
                    args.append(Var("X"))
            patterns.append((name, *args))
        return patterns

    def test_query_matches_full(self):
        rng = random.Random(20260927)
        for seed in range(150):
            rng.seed(10000 + seed)
            preds, facts, rules = self._program(rng)
            patterns = self._query_patterns(rng, preds)
            full = evaluate(facts, rules)
            self.assertTrue(full.complete, f"seed {seed}: {full.reason}")
            q = evaluate_query(facts, rules, patterns)
            self.assertTrue(q.complete, f"seed {seed}: {q.reason}")

            expected = {
                f for f in full.facts
                if any(_matches(f, p) for p in patterns)
            }
            self.assertEqual(set(q.answer()), expected, f"seed {seed}")
            self.assertTrue(q.facts <= full.facts, f"seed {seed}: phantom facts")

            for fact in q.facts:
                self.assertEqual(
                    q.derivation(fact),
                    full.derivation(fact),
                    f"seed {seed}: derivation of {fact!r}",
                )

            self.assertEqual(q.stats.derived_facts, len(q.derived))
            self.assertGreaterEqual(q.stats.pruned_derivations, 0)


if __name__ == "__main__":
    unittest.main()
