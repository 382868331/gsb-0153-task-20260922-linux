"""Tests for the well-founded grounded-rule entry point (datalog.wellfounded).

The reference oracle is an independent naive enumeration: it enumerates all
candidate (lower, upper) bound pairs with lower <= upper <= atoms and checks
the alternating-fixpoint definition directly (T(upper) == lower and
T(lower) == upper), then takes the unique minimal pair.  It never calls
datalog.wellfounded.
"""

import itertools
import unittest

from datalog import (
    LimitError,
    Rule,
    ValidationError,
    Var,
    evaluate,
    evaluate_wf,
    neg,
    pos,
)
from datalog.wellfounded import MAX_ATOMS


def naive_T(rules, facts, true_set):
    """Independent immediate-consequence operator for the oracle."""
    derived = set(facts)
    changed = True
    while changed:
        changed = False
        for head, body in rules:
            if head in derived:
                continue
            ok = True
            for atom, positive in body:
                if positive and atom not in derived:
                    ok = False
                    break
                if not positive and atom in true_set:
                    ok = False
                    break
            if ok:
                derived.add(head)
                changed = True
    return frozenset(derived)


def naive_well_founded(facts, rules):
    """Enumerate candidate bounds and verify the fixpoint by definition.

    Returns (true, false, undefined) frozensets.  Raises if no unique
    minimal fixpoint pair exists (should not happen for these programs).
    """
    atoms = set(facts)
    for head, body in rules:
        atoms.add(head)
        for atom, _ in body:
            atoms.add(atom)
    atoms = sorted(atoms)
    pairs = []
    for r in range(len(atoms) + 1):
        for lower in itertools.combinations(atoms, r):
            lower = frozenset(lower)
            upper = naive_T(rules, facts, lower)
            if naive_T(rules, facts, upper) == lower and lower <= upper:
                pairs.append((lower, upper))
    assert pairs, "no fixpoint pair found"
    best_lower = frozenset.intersection(*(p[0] for p in pairs))
    best = [p for p in pairs if p[0] == best_lower]
    assert len(best) == 1
    lower, upper = best[0]
    return lower, frozenset(atoms) - upper, upper - lower


def as_pairs(rules):
    return [(r.head, [(lit.atom, lit.positive) for lit in r.body]) for r in rules]


class MutualNegationTests(unittest.TestCase):
    def test_cycle_is_undefined_not_false(self):
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_wf([], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.true, frozenset())
        self.assertEqual(res.false, frozenset())
        self.assertEqual(res.undefined, frozenset({("a",), ("b",)}))

    def test_fact_breaks_cycle(self):
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_wf([("a",)], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.true, frozenset({("a",)}))
        self.assertEqual(res.false, frozenset({("b",)}))
        self.assertEqual(res.undefined, frozenset())

    def test_rounds_converge_monotonically(self):
        rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
        res = evaluate_wf([("a",)], rules)
        self.assertGreaterEqual(len(res.rounds), 2)
        for (lo1, up1), (lo2, up2) in zip(res.rounds, res.rounds[1:]):
            self.assertTrue(lo1 <= lo2, "lower bound must grow")
            self.assertTrue(up2 <= up1, "upper bound must shrink")
        lo, up = res.rounds[-1]
        self.assertEqual(lo, res.true)
        self.assertEqual(up, res.atoms - res.false)


class ReferenceComparisonTests(unittest.TestCase):
    PROGRAMS = [
        ([], [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]),
        ([("a",)], [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]),
        ([], [Rule(("p",), [neg("p")])]),
        ([("q",)], [Rule(("p",), [neg("q")])]),
        (
            [("edge", 1, 2)],
            [
                Rule(("win", 1), [pos("edge", 1, 2), neg("win", 2)]),
                Rule(("win", 2), [neg("win", 1)]),
            ],
        ),
        (
            [],
            [
                Rule(("a",), [neg("b")]),
                Rule(("b",), [neg("a")]),
                Rule(("c",), [pos("a")]),
                Rule(("d",), [neg("c"), neg("b")]),
            ],
        ),
    ]

    def test_matches_naive_enumeration(self):
        for facts, rules in self.PROGRAMS:
            with self.subTest(facts=facts, rules=rules):
                res = evaluate_wf(facts, rules)
                self.assertTrue(res.complete)
                t, f, u = naive_well_founded(facts, as_pairs(rules))
                self.assertEqual(res.true, t)
                self.assertEqual(res.false, f)
                self.assertEqual(res.undefined, u)

    def test_stratified_program_agrees_with_engine(self):
        # Supplementary round-trip: on stratified programs the well-founded
        # true set must equal the stratified engine's least model.
        facts = [("edge", "a", "b"), ("edge", "b", "c"), ("node", "c")]
        rules = [
            Rule(("reach", "a", "b"), [pos("edge", "a", "b")]),
            Rule(("reach", "a", "c"), [pos("reach", "a", "b"), pos("edge", "b", "c")]),
            Rule(("dead", "c"), [pos("node", "c"), neg("reach", "a", "c")]),
        ]
        eng = evaluate(facts, rules)
        wf = evaluate_wf(facts, rules)
        self.assertTrue(wf.complete)
        self.assertEqual(wf.undefined, frozenset())
        self.assertEqual(wf.true, eng.facts)


class ErrorPathTests(unittest.TestCase):
    def test_variable_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_wf([], [Rule(("p", Var("X")), [pos("q", Var("X"))])])

    def test_variable_in_body_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_wf([], [Rule(("p",), [neg("q", Var("X"))])])

    def test_atom_limit(self):
        facts = [("p", i) for i in range(MAX_ATOMS + 1)]
        with self.assertRaises(LimitError):
            evaluate_wf(facts, [])

    def test_bad_max_rounds(self):
        for bad in (True, 0, -3, "x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    evaluate_wf([], [], max_rounds=bad)

    def test_non_rule_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate_wf([], [("p",)])

    def test_budget_exhaustion_is_not_undefined_answer(self):
        # This program needs 3 rounds to converge; 2 rounds must report an
        # honest incomplete result rather than a fake undefined answer.
        facts = [("q",)]
        rules = [Rule(("p",), [neg("q")])]
        res = evaluate_wf(facts, rules, max_rounds=2)
        self.assertFalse(res.complete)
        self.assertIn("budget", res.reason)
        self.assertEqual(res.undefined, frozenset())
        self.assertTrue(res.rounds)  # partial bounds remain inspectable
        full = evaluate_wf(facts, rules)
        self.assertTrue(full.complete)
        self.assertEqual(full.true, frozenset({("q",)}))
        self.assertEqual(full.false, frozenset({("p",)}))


if __name__ == "__main__":
    unittest.main()
