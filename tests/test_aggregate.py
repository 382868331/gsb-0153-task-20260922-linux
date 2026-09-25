"""Tests for group aggregation (count/sum) in the stratified Datalog engine.

Expected values come from an *independent* naive reference implemented in
this file (``naive_aggregate``) — plain Python sets plus per-group loops,
sharing no code with the engine — plus hand-computed cases.
"""

import unittest

from datalog import (
    Aggregate,
    AggregateDerivation,
    AggregateError,
    AggregateStratificationError,
    Derivation,
    Rule,
    UnsafeRuleError,
    ValidationError,
    Var,
    evaluate,
    neg,
    pos,
)

X, Y, Z, V, N, W = Var("X"), Var("Y"), Var("Z"), Var("V"), Var("N"), Var("W")


# ---------------------------------------------------------------------------
# Independent naive reference: plain sets + per-group loops.  No engine code.
# Stratifies on its own (a strict edge is a negated literal or any positive
# literal of an aggregate rule), fires aggregate rules once per stratum and
# ordinary rules to a fixpoint.
# ---------------------------------------------------------------------------

def naive_aggregate(facts, rules, groups=None):
    groups = groups or {}
    store = set(facts)

    preds = {f[0] for f in facts}
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])

    edges = []
    for r in rules:
        for lit in r.body:
            strict = (not lit.positive) or (r.aggregate is not None and lit.positive)
            edges.append((r.head[0], lit.atom[0], strict))

    stratum = {p: 0 for p in preds}
    for _ in range(len(preds) + 1):
        changed = False
        for h, b, strict in edges:
            need = stratum[b] + (1 if strict else 0)
            if need > stratum[h]:
                stratum[h] = need
                changed = True
        if not changed:
            break
    else:
        raise AssertionError("reference: unstratifiable program")

    def subst(atom, env):
        return (atom[0],) + tuple(
            env[a] if isinstance(a, Var) else a for a in atom[1:]
        )

    def match(atom, f, env):
        if f[0] != atom[0] or len(f) != len(atom):
            return None
        env2 = dict(env)
        for a, v in zip(atom[1:], f[1:]):
            if isinstance(a, Var):
                if a in env2:
                    if env2[a] != v:
                        return None
                else:
                    env2[a] = v
            elif a != v:
                return None
        return env2

    def fire_plain(rule):
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        out = set()

        def bt(i, env):
            if i == len(pos_lits):
                for lit in neg_lits:
                    if subst(lit.atom, env) in store:
                        return
                out.add(subst(rule.head, env))
                return
            atom = pos_lits[i].atom
            for f in store:
                env2 = match(atom, f, env)
                if env2 is not None:
                    bt(i + 1, env2)

        bt(0, {})
        return out

    def fire_agg(ri, rule):
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        rows = {}  # (group key, matched-fact tuple) -> value

        def bt(i, env, matched):
            if i == len(pos_lits):
                for lit in neg_lits:
                    if subst(lit.atom, env) in store:
                        return
                gkey = tuple(env[a] for a in rule.head[1:-1])
                rowkey = (gkey, tuple(matched))
                if rule.aggregate.func == "count":
                    rows.setdefault(rowkey, None)
                else:
                    rows.setdefault(rowkey, env[rule.aggregate.value])
                return
            atom = pos_lits[i].atom
            for f in store:
                env2 = match(atom, f, env)
                if env2 is not None:
                    bt(i + 1, env2, matched + [f])

        bt(0, {}, [])
        by_group = {}
        for (gkey, _fs), val in rows.items():
            by_group.setdefault(gkey, []).append(val)
        out = set()
        for gkey, vals in by_group.items():
            n = len(vals) if rule.aggregate.func == "count" else sum(vals)
            out.add((rule.head[0],) + gkey + (n,))
        for gkey in groups.get(ri, ()):
            gkey = tuple(gkey)
            if gkey not in by_group:
                out.add((rule.head[0],) + gkey + (0,))
        return out

    by_stratum = {}
    for i, r in enumerate(rules):
        by_stratum.setdefault(stratum[r.head[0]], []).append((i, r))
    for s in sorted(by_stratum):
        srules = by_stratum[s]
        for i, r in srules:
            if r.aggregate is not None:
                store.update(fire_agg(i, r))
        plain = [r for _i, r in srules if r.aggregate is None]
        while True:
            new = set()
            for r in plain:
                new |= fire_plain(r) - store
            if not new:
                break
            store.update(new)
    return store


# ---------------------------------------------------------------------------
# Targeted aggregate tests
# ---------------------------------------------------------------------------

class TestCount(unittest.TestCase):
    def test_count_per_group(self):
        facts = [("sale", "east", 10), ("sale", "east", 20), ("sale", "west", 5)]
        rules = [
            Rule(("n_sales", X, N), [pos("sale", X, Y)], Aggregate("count")),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        got = {(f[1], f[2]) for f in res.query("n_sales", X, N)}
        self.assertEqual(got, {("east", 2), ("west", 1)})

    def test_count_dedups_same_fact_from_multiple_derivations(self):
        # item(a) is derived by two different rules; count must see it once.
        facts = [("base", "a"), ("alt", "a"), ("base", "b")]
        rules = [
            Rule(("item", X), [pos("base", X)]),                          # 0
            Rule(("item", X), [pos("alt", X)]),                           # 1
            Rule(("n_items", N), [pos("item", X)], Aggregate("count")),   # 2
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("n_items", N), [("n_items", 2)])
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_count_distinct_join_rows(self):
        # Two-literal body: rows are (fact, fact) pairs; duplicates collapse.
        facts = [("p", "a", 1), ("p", "a", 2), ("q", "a")]
        rules = [
            Rule(("pairs", X, N), [pos("p", X, Y), pos("q", X)], Aggregate("count")),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.query("pairs", "a", N), [("pairs", "a", 2)])
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))


class TestSum(unittest.TestCase):
    def test_sum_per_group_matches_naive(self):
        facts = [
            ("sale", "east", 10), ("sale", "east", 20),
            ("sale", "west", 5), ("sale", "north", 7),
        ]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        got = {(f[1], f[2]) for f in res.query("total", X, N)}
        self.assertEqual(got, {("east", 30), ("west", 5), ("north", 7)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_negative_integer_sum(self):
        facts = [("txn", "a", 10), ("txn", "a", -4), ("txn", "a", -6),
                 ("txn", "b", -3)]
        rules = [
            Rule(("balance", X, N), [pos("txn", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        got = {(f[1], f[2]) for f in res.query("balance", X, N)}
        self.assertEqual(got, {("a", 0), ("b", -3)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_sum_dedups_same_fact_from_multiple_derivations(self):
        facts = [("base", "a", 5), ("alt", "a", 5)]
        rules = [
            Rule(("item", X, V), [pos("base", X, V)]),
            Rule(("item", X, V), [pos("alt", X, V)]),
            Rule(("total", N), [pos("item", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        # item(a,5) is one fact despite two derivations: sum is 5, not 10.
        self.assertEqual(res.query("total", N), [("total", 5)])
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_sum_rejects_non_integer(self):
        for bad in (1.5, "x"):
            with self.assertRaises(AggregateError, msg=bad):
                evaluate(
                    [("txn", "a", bad)],
                    [Rule(("t", X, N), [pos("txn", X, V)], Aggregate("sum", V))],
                )

    def test_bool_value_rejected_upstream(self):
        with self.assertRaises(ValidationError):
            evaluate([("txn", "a", True)], [])


class TestExplicitGroupDomain(unittest.TestCase):
    FACTS = [("sale", "east", 10), ("sale", "west", 5)]
    RULES = [Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V))]

    def test_empty_group_only_from_explicit_domain(self):
        res = evaluate(self.FACTS, self.RULES, groups={0: [("east",), ("south",)]})
        got = {(f[1], f[2]) for f in res.query("total", X, N)}
        self.assertEqual(got, {("east", 10), ("west", 5), ("south", 0)})
        d = res.derivation(("total", "south", 0))
        self.assertIsInstance(d, AggregateDerivation)
        self.assertTrue(d.explicit)
        self.assertEqual(d.contributors, ())
        self.assertEqual(d.contributor_ids, ())
        self.assertEqual(d.value, 0)

    def test_unlisted_empty_group_produces_nothing(self):
        res = evaluate(self.FACTS, self.RULES)
        got = {f[1] for f in res.query("total", X, N)}
        self.assertEqual(got, {"east", "west"})

    def test_explicit_key_with_rows_is_not_marked_explicit(self):
        res = evaluate(self.FACTS, self.RULES, groups={0: [("east",)]})
        d = res.derivation(("total", "east", 10))
        self.assertFalse(d.explicit)
        self.assertEqual(d.value, 10)

    def test_groups_domain_validation(self):
        with self.assertRaises(ValidationError):
            evaluate(self.FACTS, self.RULES, groups={0: [("a", "b")]})  # arity
        with self.assertRaises(ValidationError):
            evaluate(self.FACTS, self.RULES, groups={0: [(X,)]})        # not ground
        with self.assertRaises(ValidationError):
            evaluate(self.FACTS, self.RULES, groups={7: [("a",)]})      # bad index
        with self.assertRaises(ValidationError):
            evaluate(self.FACTS, self.RULES, groups="nope")

    def test_explicit_empty_count_group(self):
        facts = [("hit", "a")]
        rules = [Rule(("hits", X, N), [pos("hit", X)], Aggregate("count"))]
        res = evaluate(facts, rules, groups={0: [("a",), ("b",)]})
        got = {(f[1], f[2]) for f in res.query("hits", X, N)}
        self.assertEqual(got, {("a", 1), ("b", 0)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules, {0: [("a",), ("b",)]}))


class TestAggregateProvenance(unittest.TestCase):
    def test_contributors_and_stable_ids(self):
        facts = [("sale", "east", 10), ("sale", "east", 20), ("sale", "west", 5)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        d = res.derivation(("total", "east", 30))
        self.assertIsInstance(d, AggregateDerivation)
        self.assertEqual(d.rule_index, 0)
        self.assertEqual(d.value, 30)
        contrib = {c for row in d.contributors for c in row}
        self.assertEqual(contrib, {("sale", "east", 10), ("sale", "east", 20)})
        ids = {i for row in d.contributor_ids for i in row}
        self.assertEqual(
            ids,
            {res.fact_id(("sale", "east", 10)), res.fact_id(("sale", "east", 20))},
        )
        # Ids are stable across identical runs.
        res2 = evaluate(facts, rules)
        self.assertEqual(res.fact_id(("sale", "east", 10)),
                         res2.fact_id(("sale", "east", 10)))
        self.assertEqual(res.fact_id(("total", "east", 30)),
                         res2.fact_id(("total", "east", 30)))

    def test_explain_renders_aggregate_chain(self):
        facts = [("sale", "east", 10), ("sale", "east", 20)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        text = res.explain(("total", "east", 30))
        self.assertIn("aggregate rule 0", text)
        self.assertIn("sale('east', 10)", text)
        self.assertIn("sale('east', 20)", text)

    def test_aggregate_fact_consumed_by_higher_rule(self):
        facts = [("sale", "east", 10), ("sale", "east", 20), ("sale", "west", 5)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),  # 0
            Rule(("big", X), [pos("total", X, N)]),                           # 1
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("big", X), [("big", "east"), ("big", "west")])
        d = res.derivation(("big", "east"))
        self.assertIsInstance(d, Derivation)
        self.assertIn(("total", "east", 30), d.children)
        text = res.explain(("big", "east"))
        self.assertIn("total('east', 30)", text)
        self.assertIn("aggregate rule 0", text)


class TestAggregateThenNegation(unittest.TestCase):
    def test_negation_over_aggregate_result(self):
        facts = [("sale", "east", 10), ("sale", "east", 20), ("sale", "west", 5),
                 ("grp", "east"), ("grp", "west")]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),  # 0
            Rule(("big_total", X), [pos("total", X, 30)]),                    # 1
            Rule(("small", X), [pos("grp", X), neg("big_total", X)]),         # 2
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("small", X), [("small", "west")])
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))


class TestAggregateRecursionRejected(unittest.TestCase):
    def test_direct_aggregate_self_cycle(self):
        rules = [
            Rule(("t", X, N), [pos("t", X, V)], Aggregate("sum", V)),
        ]
        with self.assertRaises(AggregateStratificationError) as cm:
            evaluate([("t", "a", 1)], rules)
        self.assertIn("aggregate", str(cm.exception))

    def test_indirect_cycle_through_aggregate(self):
        rules = [
            Rule(("t", X, N), [pos("s", X, V)], Aggregate("sum", V)),  # 0
            Rule(("s", X, V), [pos("t", X, V)]),                       # 1
        ]
        with self.assertRaises(AggregateStratificationError):
            evaluate([("s", "a", 1)], rules)

    def test_aggregate_cycle_is_stratification_error_subclass(self):
        from datalog import StratificationError
        rules = [Rule(("t", X, N), [pos("t", X, V)], Aggregate("sum", V))]
        with self.assertRaises(StratificationError):
            evaluate([("t", "a", 1)], rules)

    def test_same_stratum_positive_recursion_still_allowed(self):
        # Positive recursion untouched: reachability plus an aggregate over
        # its completed result.
        facts = [("edge", "a", "b"), ("edge", "b", "c")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
            Rule(("n_reach", X, N), [pos("reach", X, Y)], Aggregate("count")),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        got = {(f[1], f[2]) for f in res.query("n_reach", X, N)}
        self.assertEqual(got, {("a", 2), ("b", 1)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))


class TestAggregateSafetyAndValidation(unittest.TestCase):
    def test_group_key_must_be_bound(self):
        with self.assertRaises(UnsafeRuleError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", X, N), [pos("sale", Y, V)], Aggregate("sum", V))],
            )

    def test_sum_value_must_be_bound(self):
        with self.assertRaises(UnsafeRuleError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", X, N), [pos("sale", X, Y)], Aggregate("sum", V))],
            )

    def test_result_column_must_be_fresh_variable(self):
        with self.assertRaises(UnsafeRuleError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", X, V), [pos("sale", X, V)], Aggregate("sum", V))],
            )
        with self.assertRaises(UnsafeRuleError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", X, 0), [pos("sale", X, V)], Aggregate("sum", V))],
            )

    def test_group_key_must_be_variable(self):
        with self.assertRaises(UnsafeRuleError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", "east", N), [pos("sale", X, V)], Aggregate("sum", V))],
            )

    def test_bad_aggregate_object(self):
        with self.assertRaises(ValidationError):
            Aggregate("avg")
        with self.assertRaises(ValidationError):
            Aggregate("sum", 5)
        with self.assertRaises(ValidationError):
            evaluate(
                [("sale", "a", 1)],
                [Rule(("t", X, N), [pos("sale", X, V)], "count")],
            )

    def test_aggregate_rule_needs_body(self):
        with self.assertRaises(ValidationError):
            evaluate([], [Rule(("t", N), [], Aggregate("count"))])


class TestAggregateBudget(unittest.TestCase):
    def test_budget_overflow_publishes_no_partial_totals(self):
        # 3 groups would be produced; the budget allows only 2 new facts.
        facts = [("sale", "a", 1), ("sale", "b", 2), ("sale", "c", 3)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules, max_facts=len(facts) + 2)
        self.assertFalse(res.complete)
        self.assertIn("budget", res.reason)
        # Nothing from the aggregate batch was published.
        self.assertEqual(res.query("total", X, N), [])
        self.assertEqual(res.derived, frozenset())

    def test_budget_exactly_sufficient_commits_whole_batch(self):
        facts = [("sale", "a", 1), ("sale", "b", 2)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules, max_facts=len(facts) + 2)
        self.assertTrue(res.complete)
        self.assertEqual(len(res.query("total", X, N)), 2)


class TestAggregateExtraShapes(unittest.TestCase):
    def test_aggregate_body_with_negation(self):
        # Count only sales with no refund: the negated literal keeps the
        # aggregate one strict edge above both body predicates.
        facts = [("sale", "a", 10), ("sale", "b", 20), ("refund", "b")]
        rules = [
            Rule(("kept", X, N),
                 [pos("sale", X, V), neg("refund", X)],
                 Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        got = {(f[1], f[2]) for f in res.query("kept", X, N)}
        self.assertEqual(got, {("a", 10)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_multi_column_group_key(self):
        facts = [
            ("hit", "a", "x", 1), ("hit", "a", "x", 2), ("hit", "a", "y", 3),
        ]
        rules = [
            Rule(("n", X, Y, N), [pos("hit", X, Y, Z)], Aggregate("count")),
        ]
        res = evaluate(facts, rules)
        got = {(f[1], f[2], f[3]) for f in res.query("n", X, Y, N)}
        self.assertEqual(got, {("a", "x", 2), ("a", "y", 1)})
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_aggregate_over_aggregate_is_two_layers(self):
        facts = [("sale", "east", 10), ("sale", "east", 20), ("sale", "west", 5)]
        rules = [
            Rule(("total", X, N), [pos("sale", X, V)], Aggregate("sum", V)),  # 0
            Rule(("grand", N), [pos("total", X, V)], Aggregate("sum", V)),   # 1
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("grand", N), [("grand", 35)])

    def test_join_row_multiplicity_is_distinct_rows(self):
        # p(a,10) joins q(a,1) and q(a,2): two distinct rows, so count=2
        # and sum=20 — set facts dedup, but distinct join rows both count.
        facts = [("p", "a", 10), ("q", "a", 1), ("q", "a", 2)]
        rules = [
            Rule(("c", X, N), [pos("p", X, V), pos("q", X, W)], Aggregate("count")),
            Rule(("s", X, N), [pos("p", X, V), pos("q", X, W)], Aggregate("sum", V)),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.query("c", "a", N), [("c", "a", 2)])
        self.assertEqual(res.query("s", "a", N), [("s", "a", 20)])
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules))

    def test_no_group_keys_global_aggregate(self):
        facts = [("v", 1), ("v", 2), ("v", 3)]
        rules = [Rule(("s", N), [pos("v", V)], Aggregate("sum", V))]
        res = evaluate(facts, rules)
        self.assertEqual(res.query("s", N), [("s", 6)])
        res0 = evaluate(facts, rules, groups={0: [()]})
        self.assertEqual(res0.query("s", N), [("s", 6)])
        # Empty store with explicit global group -> zero.
        res_empty = evaluate([], [Rule(("s0", N), [pos("v", V)], Aggregate("count"))],
                             groups={0: [()]})
        self.assertEqual(res_empty.query("s0", N), [("s0", 0)])


class TestAggregateCrossCheck(unittest.TestCase):
    def test_mixed_program_matches_naive(self):
        facts = [
            ("emp", "e1", "eng", 10), ("emp", "e2", "eng", 20),
            ("emp", "e3", "ops", 5), ("emp", "e4", "ops", -2),
            ("dept", "eng"), ("dept", "ops"), ("dept", "hr"),
        ]
        rules = [
            Rule(("payroll", X, N), [pos("emp", Y, X, V)], Aggregate("sum", V)),  # 0
            Rule(("staff", X, N), [pos("emp", Y, X, V)], Aggregate("count")),    # 1
            Rule(("has_staff", X), [pos("staff", X, N)]),                        # 2
            Rule(("costly", X), [pos("payroll", X, 30)]),                        # 3
            Rule(("idle", X), [pos("dept", X), neg("has_staff", X)]),            # 4
        ]
        groups = {0: [("hr",)], 1: [("hr",)]}
        res = evaluate(facts, rules, groups=groups)
        self.assertTrue(res.complete)
        self.assertEqual(set(res.facts), naive_aggregate(facts, rules, groups))
        got_pay = {(f[1], f[2]) for f in res.query("payroll", X, N)}
        self.assertEqual(got_pay, {("eng", 30), ("ops", 3), ("hr", 0)})
        got_staff = {(f[1], f[2]) for f in res.query("staff", X, N)}
        self.assertEqual(got_staff, {("eng", 2), ("ops", 2), ("hr", 0)})
        self.assertEqual(res.query("costly", X), [("costly", "eng")])
        # hr has an explicit zero staff row, so it is not idle.
        self.assertEqual(res.query("idle", X), [])


if __name__ == "__main__":
    unittest.main()
