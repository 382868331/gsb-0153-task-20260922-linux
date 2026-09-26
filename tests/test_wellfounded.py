"""Tests for the well-founded (alternating fixpoint) ground evaluator.

Expected values come from an *independent* reference implemented in this
file: for small universes it enumerates every candidate lower bound S,
computes T(T(S)) by the definition of the immediate-consequence operator,
and takes the least fixpoint -- plus hand-computed sets for the targeted
examples.  It shares no code with datalog/wellfounded.py.
"""

import itertools
import random
import unittest

from datalog import (
    LimitError,
    Rule,
    ValidationError,
    Var,
    evaluate_ground,
    neg,
    pos,
)
from datalog.wellfounded import MAX_ATOMS


# ---------------------------------------------------------------------------
# Independent reference: brute-force enumeration of candidate bounds.
# T(J) = least set closed under rules with "not a" read as "a not in J".
# The well-founded lower bound is the least fixpoint of T . T (monotone
# operator on the powerset, so the least fixpoint is unique); the upper
# bound is T applied to it.  Verified purely by the definition.
# ---------------------------------------------------------------------------

def _ref_T(program, universe, neg_interp):
    derived = set()
    changed = True
    while changed:
        changed = False
        for head, body in program:
            if head in derived:
                continue
            if all(
                (atom in derived) if positive else (atom not in neg_interp)
                for atom, positive in body
            ):
                derived.add(head)
                changed = True
    return frozenset(derived)


def reference_wfs(facts, rules):
    """Return (true, false, undefined) by exhaustive fixpoint enumeration."""
    universe = set(facts)
    program = [(f, ()) for f in facts]
    for rule in rules:
        universe.add(rule.head)
        program.append((rule.head, tuple((l.atom, l.positive) for l in rule.body)))
        for lit in rule.body:
            universe.add(lit.atom)
    universe = frozenset(universe)

    least = None
    for size in range(len(universe) + 1):
        for combo in itertools.combinations(sorted(universe), size):
            candidate = frozenset(combo)
            if _ref_T(program, universe, _ref_T(program, universe, candidate)) == candidate:
                least = candidate
                break
        if least is not None:
            break
    assert least is not None, "monotone operator must have a least fixpoint"
    lower = least
    upper = _ref_T(program, universe, lower)
    return lower, frozenset(universe - upper), frozenset(upper - lower)


def random_ground_program(rng, n_atoms=5, n_rules=6):
    atoms = [("p", i) for i in range(n_atoms)]
    facts = [a for a in atoms if rng.random() < 0.2]
    rules = []
    for _ in range(n_rules):
        head = rng.choice(atoms)
        body = []
        for _ in range(rng.randint(0, 3)):
            atom = rng.choice(atoms)
            body.append(pos(*atom) if rng.random() < 0.5 else neg(*atom))
        rules.append(Rule(head, body))
    return facts, rules


# ---------------------------------------------------------------------------
# Targeted cases
# ---------------------------------------------------------------------------

class NegationCycleTests(unittest.TestCase):
    def test_mutual_negation_is_undefined_not_false(self):
        # a :- not b.  b :- not a.  -- neither can be proven, but neither
        # is genuinely false: both stay undefined.
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_ground([], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.true, frozenset())
        self.assertEqual(res.false, frozenset())
        self.assertEqual(res.undefined, frozenset({("a",), ("b",)}))
        self.assertEqual(res.status(("a",)), "undefined")
        self.assertEqual(res.status(("b",)), "undefined")

    def test_adding_fact_breaks_the_cycle(self):
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_ground([("a",)], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.true, frozenset({("a",)}))
        self.assertEqual(res.false, frozenset({("b",)}))
        self.assertEqual(res.undefined, frozenset())

    def test_genuinely_false_atom_is_not_undefined(self):
        # d has no rule and no fact: no positive proof AND it drops out of
        # the upper bound, so it is genuinely false -- distinct from the
        # undefined cycle atoms.
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")]),
                 Rule(("c",), [pos("d")])]
        res = evaluate_ground([], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.false, frozenset({("c",), ("d",)}))
        self.assertEqual(res.undefined, frozenset({("a",), ("b",)}))

    def test_odd_cycle_self_negation_is_undefined(self):
        res = evaluate_ground([], [Rule(("p",), [neg("p")])])
        self.assertTrue(res.complete)
        self.assertEqual(res.undefined, frozenset({("p",)}))

    def test_positive_recursion_still_works(self):
        facts = [("edge", 1, 2), ("edge", 2, 3)]
        rules = [
            Rule(("reach", 1, 2), [pos("edge", 1, 2)]),
            Rule(("reach", 2, 3), [pos("edge", 2, 3)]),
            Rule(("reach", 1, 3), [pos("reach", 1, 2), pos("edge", 2, 3)]),
        ]
        res = evaluate_ground(facts, rules)
        self.assertTrue(res.complete)
        self.assertIn(("reach", 1, 3), res.true)
        self.assertEqual(res.undefined, frozenset())

    def test_rounds_trace_is_monotone_and_partitions(self):
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_ground([], rules)
        self.assertGreaterEqual(len(res.rounds), 1)
        prev_lower, prev_upper = frozenset(), None
        for rd in res.rounds:
            self.assertLessEqual(prev_lower, rd.lower)   # lower ascends
            if prev_upper is not None:
                self.assertLessEqual(rd.upper, prev_upper)  # upper descends
            self.assertLessEqual(rd.lower, rd.upper)
            prev_lower, prev_upper = rd.lower, rd.upper
        # Final bounds partition the universe.
        self.assertEqual(res.true | res.false | res.undefined, res.atoms)
        self.assertEqual(res.true & res.undefined, frozenset())
        self.assertEqual(res.false & res.undefined, frozenset())


class FixpointDefinitionTests(unittest.TestCase):
    """Cross-check against the independent enumeration reference and verify
    the alternating fixpoint property T(upper) == lower, T(lower) == upper
    directly by definition."""

    def test_hand_cases_match_reference(self):
        cases = [
            ([], [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]),
            ([("a",)], [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]),
            ([("p", 1)], [Rule(("q", 1), [pos("p", 1), neg("r", 1)]),
                          Rule(("r", 1), [neg("q", 1)])]),
        ]
        for facts, rules in cases:
            res = evaluate_ground(facts, rules)
            self.assertTrue(res.complete)
            self.assertEqual(
                (res.true, res.false, res.undefined),
                reference_wfs(facts, rules),
            )

    def test_random_programs_match_reference(self):
        for seed in range(60):
            rng = random.Random(seed)
            facts, rules = random_ground_program(rng)
            res = evaluate_ground(facts, rules)
            self.assertTrue(res.complete, f"seed {seed} did not converge")
            expected = reference_wfs(facts, rules)
            self.assertEqual(
                (res.true, res.false, res.undefined),
                expected,
                f"seed {seed}: {facts} {rules}",
            )


class BudgetTests(unittest.TestCase):
    def test_budget_exhaustion_is_not_an_undefined_answer(self):
        # The a/b cycle needs 2 rounds to converge; max_rounds=1 must give
        # an explicitly incomplete result, not a fake three-valued answer.
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_ground([], rules, max_rounds=1)
        self.assertFalse(res.complete)
        self.assertIn("budget", res.reason)
        self.assertIsNone(res.true)
        self.assertIsNone(res.false)
        self.assertIsNone(res.undefined)
        self.assertEqual(len(res.rounds), 1)  # trace still inspectable
        with self.assertRaises(RuntimeError):
            res.status(("a",))

    def test_invalid_max_rounds_rejected(self):
        for bad in (True, 0, -3, 1.5, "10"):
            with self.assertRaises(ValidationError):
                evaluate_ground([], [], max_rounds=bad)


class ValidationTests(unittest.TestCase):
    def test_variables_rejected_with_location(self):
        with self.assertRaises(ValidationError) as ctx:
            evaluate_ground([], [Rule(("p", Var("X")), [pos("q", Var("X"))])])
        self.assertIn("rule 0", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            evaluate_ground([("p", Var("X"))], [])
        self.assertIn("fact 0", str(ctx.exception))

    def test_arity_mismatch_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_ground([("p", 1)], [Rule(("p", 1, 2), ())])

    def test_atom_universe_limit(self):
        facts = [("p", i) for i in range(MAX_ATOMS + 1)]
        with self.assertRaises(LimitError):
            evaluate_ground(facts, [])
        # Exactly at the limit is fine.
        res = evaluate_ground([("p", i) for i in range(MAX_ATOMS)], [])
        self.assertTrue(res.complete)
        self.assertEqual(len(res.true), MAX_ATOMS)

    def test_malformed_inputs_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_ground("not-a-list", [])
        with self.assertRaises(ValidationError):
            evaluate_ground([], [("not", "a", "rule")])
        with self.assertRaises(ValidationError):
            evaluate_ground([(True,)], [])  # bool constant rejected
        with self.assertRaises(ValidationError):
            evaluate_ground([("p", float("nan"))], [])

    def test_status_unknown_atom(self):
        res = evaluate_ground([("p",)], [])
        with self.assertRaises(KeyError):
            res.status(("q",))


if __name__ == "__main__":
    unittest.main()
