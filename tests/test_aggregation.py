"""Tests for grouping aggregation (count/sum) in rule bodies.

Reference values for the small datasets are computed with plain Python
sets and per-group sums (``reference_counts`` / ``reference_sums``) —
independent of the engine — plus hand-computed expectations.
"""

import unittest

from datalog import (
    AggDerivation,
    Derivation,
    Rule,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
    Var,
    agg_count,
    agg_sum,
    evaluate,
    neg,
    pos,
)

D, E, A, C, S, X, Y = (Var(n) for n in ("D", "E", "A", "C", "S", "X", "Y"))


# ---------------------------------------------------------------------------
# Independent references: plain-set counting and per-group integer sums.
# ---------------------------------------------------------------------------

def reference_counts(domain, facts, key_pos):
    """{key: number of distinct facts whose key is in the explicit domain}."""
    out = {k: 0 for k in domain}
    for f in set(facts):  # set semantics: duplicates count once
        if f[key_pos] in out:
            out[f[key_pos]] += 1
    return out


def reference_sums(domain, facts, key_pos, val_pos):
    out = {k: 0 for k in domain}
    for f in set(facts):
        if f[key_pos] in out:
            out[f[key_pos]] += f[val_pos]
    return out


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

class TestCount(unittest.TestCase):
    """dept_count(D, C) :- dept(D), count of member(D, E)."""

    def setUp(self):
        self.facts = [
            ("dept", "d1"), ("dept", "d2"), ("dept", "d3"),
            ("emp", "e1", "d1"), ("emp", "e2", "d1"), ("emp", "e3", "d2"),
        ]
        # member is derived by TWO rules producing the same facts:
        # multiple derivations must not inflate the count.
        self.rules = [
            Rule(("member", D, E), [pos("emp", E, D)]),                       # 0
            Rule(("member", D, E), [pos("emp", E, D), pos("emp", E, D)]),     # 1
            Rule(("dept_count", D, C),
                 [pos("dept", D), agg_count("member", D, E, group=(D,), into=C)]),  # 2
        ]

    def test_counts_match_plain_set_reference(self):
        res = evaluate(self.facts, self.rules)
        self.assertTrue(res.complete)
        members = {("member", f[2], f[1]) for f in self.facts if f[0] == "emp"}
        # member facts are derived; reference counts over the same set:
        ref = reference_counts({"d1", "d2", "d3"}, members, 1)
        got = {f[1]: f[2] for f in res.query("dept_count", D, C)}
        self.assertEqual(got, ref)
        self.assertEqual(got, {"d1": 2, "d2": 1, "d3": 0})

    def test_duplicate_derivations_counted_once(self):
        # member(d1, e1) is derivable by rule 0 AND rule 1; count stays 2.
        res = evaluate(self.facts, self.rules)
        self.assertEqual(res.query("dept_count", "d1", C), [("dept_count", "d1", 2)])

    def test_empty_group_yields_zero_only_in_explicit_domain(self):
        res = evaluate(self.facts, self.rules)
        # d3 is in the domain (dept fact) with no members -> 0.
        self.assertEqual(res.query("dept_count", "d3", C), [("dept_count", "d3", 0)])
        # d9 is NOT in the domain -> no fact at all (not even a zero).
        self.assertEqual(res.query("dept_count", "d9", C), [])

    def test_count_provenance_records_contributors_and_value(self):
        res = evaluate(self.facts, self.rules)
        d = res.derivation(("dept_count", "d1", 2))
        self.assertIsInstance(d, AggDerivation)
        self.assertEqual(d.rule_index, 2)
        self.assertEqual(d.op, "count")
        self.assertEqual(d.group, ("d1",))
        self.assertEqual(d.value, 2)
        self.assertEqual(
            set(d.contributors),
            {("member", "d1", "e1"), ("member", "d1", "e2")},
        )
        self.assertEqual(d.children, (("dept", "d1"),))
        # Empty group: zero contributors, value 0.
        d3 = res.derivation(("dept_count", "d3", 0))
        self.assertEqual(d3.contributors, ())
        self.assertEqual(d3.value, 0)
        # Ordinary derivations are unaffected (old provenance still works).
        self.assertIsInstance(res.derivation(("member", "d1", "e1")), Derivation)

    def test_explain_renders_aggregate_aggregate_chain(self):
        res = evaluate(self.facts, self.rules)
        text = res.explain(("dept_count", "d1", 2))
        self.assertIn("count = 2 over group ('d1',)", text)
        self.assertIn("contributors (2):", text)
        self.assertIn("member('d1', 'e1')", text)


# ---------------------------------------------------------------------------
# sum
# ---------------------------------------------------------------------------

class TestSum(unittest.TestCase):
    def test_sum_with_negative_integers(self):
        facts = [
            ("acct", "a1"), ("acct", "a2"), ("acct", "a3"),
            ("txn", "a1", 10), ("txn", "a1", -4), ("txn", "a1", -6),
            ("txn", "a2", -3),
        ]
        rules = [
            Rule(("balance", D, S),
                 [pos("acct", D), agg_sum("txn", D, A, group=(D,), value=A, into=S)]),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        ref = reference_sums({"a1", "a2", "a3"},
                             [f for f in facts if f[0] == "txn"], 1, 2)
        got = {f[1]: f[2] for f in res.query("balance", D, S)}
        self.assertEqual(got, ref)
        self.assertEqual(got, {"a1": 0, "a2": -3, "a3": 0})  # negatives sum exactly

    def test_sum_provenance_value(self):
        facts = [("acct", "a1"), ("txn", "a1", 7), ("txn", "a1", -2)]
        rules = [
            Rule(("balance", D, S),
                 [pos("acct", D), agg_sum("txn", D, A, group=(D,), value=A, into=S)]),
        ]
        res = evaluate(facts, rules)
        d = res.derivation(("balance", "a1", 5))
        self.assertEqual(d.op, "sum")
        self.assertEqual(d.value, 5)
        self.assertEqual(set(d.contributors), {("txn", "a1", 7), ("txn", "a1", -2)})

    def test_sum_over_non_integer_rejected(self):
        for bad in ("oops", 1.5):
            facts = [("acct", "a1"), ("txn", "a1", bad)]
            rules = [
                Rule(("balance", D, S),
                     [pos("acct", D), agg_sum("txn", D, A, group=(D,), value=A, into=S)]),
            ]
            with self.assertRaises(ValidationError, msg=bad) as cm:
                evaluate(facts, rules)
            self.assertIn("rule 0", str(cm.exception))
            self.assertIn("int", str(cm.exception))


# ---------------------------------------------------------------------------
# Aggregates compose with higher strata (ordinary rules and negation)
# ---------------------------------------------------------------------------

class TestAggregateComposition(unittest.TestCase):
    def test_aggregate_result_usable_by_higher_rules_and_negation(self):
        # counted(D, C): only tracked depts get a count row;
        # big(D): ordinary higher-stratum rule over the aggregate result;
        # uncounted(D): negation over the aggregate predicate (higher stratum).
        facts = [
            ("dept", "d1"), ("dept", "d2"), ("dept", "d3"),
            ("tracked", "d1"), ("tracked", "d2"),
            ("emp", "e1", "d1"), ("emp", "e2", "d1"), ("emp", "e3", "d2"),
        ]
        rules = [
            Rule(("counted", D, C),
                 [pos("tracked", D), agg_count("emp", E, D, group=(D,), into=C)]),  # 0
            Rule(("big", D), [pos("counted", D, 2)]),                              # 1
            Rule(("has_count", D), [pos("counted", D, C)]),                        # 2
            Rule(("uncounted", D), [pos("dept", D), neg("has_count", D)]),         # 3
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(
            res.query("counted", D, C),
            [("counted", "d1", 2), ("counted", "d2", 1)],
        )
        self.assertEqual(res.query("big", D), [("big", "d1")])
        # d3 has no counted row (not tracked) -> negation fires at a higher stratum.
        self.assertEqual(res.query("uncounted", D), [("uncounted", "d3")])
        # Provenance chain crosses the aggregate edge upward.
        d = res.derivation(("big", "d1"))
        self.assertIsInstance(d, Derivation)
        self.assertEqual(d.children, (("counted", "d1", 2),))
        agg_d = res.derivation(d.children[0])
        self.assertIsInstance(agg_d, AggDerivation)

    def test_positive_recursion_same_stratum_as_aggregate_survives(self):
        # reach is ordinary positive recursion; the aggregate sits in the
        # next stratum and counts its results.
        facts = [("edge", "a", "b"), ("edge", "b", "c"), ("src", "a")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),                       # 0
            Rule(("reach", X, Y), [pos("reach", X, D), pos("edge", D, Y)]),   # 1
            Rule(("nreach", X, C),
                 [pos("src", X), agg_count("reach", X, Y, group=(X,), into=C)]),  # 2
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("nreach", "a", C), [("nreach", "a", 2)])


# ---------------------------------------------------------------------------
# Stratification: recursion through an aggregate edge is rejected
# ---------------------------------------------------------------------------

class TestAggregateStratification(unittest.TestCase):
    def test_direct_aggregate_self_cycle_rejected(self):
        rules = [
            Rule(("p", X, C),
                 [pos("e", X), agg_count("p", X, Y, group=(X,), into=C)]),
        ]
        with self.assertRaises(StratificationError) as cm:
            evaluate([("e", 1)], rules)
        self.assertIn("'p'", str(cm.exception))

    def test_indirect_aggregate_cycle_rejected(self):
        # p aggregates over q; q depends positively on p -> cycle through agg.
        rules = [
            Rule(("p", X, C),
                 [pos("e", X), agg_count("q", X, Y, group=(X,), into=C)]),
            Rule(("q", X, Y), [pos("p", X, C), pos("f", X, Y)]),
        ]
        with self.assertRaises(StratificationError):
            evaluate([("e", 1), ("f", 1, 2)], rules)

    def test_aggregate_over_same_stratum_predicate_rejected(self):
        # r is derived by an ordinary rule in the same stratum as the
        # aggregate head; aggregating over it must still force stratification.
        rules = [
            Rule(("r", X), [pos("e", X)]),
            Rule(("s", X, C),
                 [pos("e", X), agg_count("r", X, group=(X,), into=C)]),
            Rule(("r", X), [pos("s", X, C)]),
        ]
        with self.assertRaises(StratificationError):
            evaluate([("e", 1)], rules)


# ---------------------------------------------------------------------------
# Safety / structural validation of aggregates
# ---------------------------------------------------------------------------

class TestAggregateValidation(unittest.TestCase):
    def test_unbound_group_key_rejected(self):
        rules = [
            Rule(("p", X, C),
                 [pos("e", X), agg_count("f", Y, group=(Y,), into=C)]),
        ]
        with self.assertRaises(UnsafeRuleError) as cm:
            evaluate([("e", 1), ("f", 2)], rules)
        self.assertIn("group key", str(cm.exception))

    def test_result_variable_collision_rejected(self):
        rules = [
            Rule(("p", X, C),
                 [pos("e", X, C), agg_count("f", X, group=(X,), into=C)]),
        ]
        with self.assertRaises(UnsafeRuleError):
            evaluate([("e", 1, 2), ("f", 1)], rules)

    def test_sum_value_variable_must_be_local(self):
        # value var bound by the ordinary body -> rejected
        rules = [
            Rule(("p", X, S),
                 [pos("e", X, S), agg_sum("f", X, A, group=(X,), value=A, into=S)]),
        ]
        with self.assertRaises(UnsafeRuleError):
            evaluate([("e", 1, 2), ("f", 1, 3)], rules)
        # value var not occurring in the aggregated atom -> rejected
        rules2 = [
            Rule(("p", X, S),
                 [pos("e", X), agg_sum("f", X, group=(X,), value=A, into=S)]),
        ]
        with self.assertRaises(ValidationError):
            evaluate([("e", 1), ("f", 1)], rules2)

    def test_structural_errors(self):
        # aggregate not last
        with self.assertRaises(ValidationError):
            evaluate([("e", 1), ("f", 1)],
                     [Rule(("p", X, C),
                           [agg_count("f", X, group=(X,), into=C), pos("e", X)])])
        # empty group
        with self.assertRaises(ValidationError):
            evaluate([("e", 1), ("f", 1)],
                     [Rule(("p", X, C),
                           [pos("e", X), agg_count("f", X, group=(), into=C)])])
        # unknown op
        with self.assertRaises(ValidationError):
            from datalog import Aggregate
            evaluate([("e", 1), ("f", 1)],
                     [Rule(("p", X, C),
                           [pos("e", X),
                            Aggregate("max", ("f", X), (X,), None, C)])])
        # count with a value var
        with self.assertRaises(ValidationError):
            from datalog import Aggregate
            evaluate([("e", 1), ("f", 1)],
                     [Rule(("p", X, C),
                           [pos("e", X),
                            Aggregate("count", ("f", X), (X,), A, C)])])


# ---------------------------------------------------------------------------
# Budget: no partially published totals
# ---------------------------------------------------------------------------

class TestAggregateBudget(unittest.TestCase):
    def test_budget_overflow_publishes_no_totals(self):
        facts = [("dept", "d1"), ("dept", "d2"),
                 ("emp", "e1", "d1"), ("emp", "e2", "d2")]
        rules = [
            Rule(("dept_count", D, C),
                 [pos("dept", D), agg_count("emp", E, D, group=(D,), into=C)]),
        ]
        full = evaluate(facts, rules)
        self.assertTrue(full.complete)
        self.assertEqual(len(full.query("dept_count", D, C)), 2)
        # Budget admits the inputs but not the aggregate round: the whole
        # round is discarded — zero totals published, explicitly incomplete.
        tight = evaluate(facts, rules, max_facts=len(facts))
        self.assertFalse(tight.complete)
        self.assertIn("budget", tight.reason)
        self.assertEqual(tight.query("dept_count", D, C), [])
        self.assertEqual(tight.derived, frozenset())

    def test_budget_exactly_sufficient_publishes_complete_totals(self):
        facts = [("dept", "d1"), ("emp", "e1", "d1"), ("emp", "e2", "d1")]
        rules = [
            Rule(("dept_count", D, C),
                 [pos("dept", D), agg_count("emp", E, D, group=(D,), into=C)]),
        ]
        res = evaluate(facts, rules, max_facts=len(facts) + 1)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("dept_count", D, C), [("dept_count", "d1", 2)])


if __name__ == "__main__":
    unittest.main()
