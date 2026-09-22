"""输入契约与错误语义测试。"""

import math
import unittest

from datalog import (
    Atom,
    InvalidProgramError,
    Rule,
    evaluate,
    MAX_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
)


class TestScalarValidation(unittest.TestCase):
    def test_reject_nan_infinity(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidProgramError):
                    Atom("p", [bad])
                with self.assertRaises(InvalidProgramError):
                    evaluate([("p", (bad,))], [])

    def test_finite_float_accepted(self):
        r = evaluate([("p", (1.5,)), ("p", (-0.25,))], [])
        self.assertIn(("p", (1.5,)), r)
        self.assertIn(("p", (-0.25,)), r)

    def test_reject_bool_as_integer(self):
        # bool 是 int 子类：事实、参数、预算三处都必须拒绝。
        with self.assertRaises(InvalidProgramError):
            Atom("p", [True])
        with self.assertRaises(InvalidProgramError):
            evaluate([("p", (False,))], [])
        with self.assertRaises(InvalidProgramError):
            evaluate([], [], fact_budget=True)

    def test_integer_normal(self):
        r = evaluate([("p", (0,)), ("p", (-7,)), ("p", (2**40,))], [])
        self.assertEqual(len(r.facts["p"]), 3)

    def test_unsafe_terms(self):
        with self.assertRaises(InvalidProgramError):
            Atom("p", [None])
        with self.assertRaises(InvalidProgramError):
            Atom("p", [(1, 2)])  # 无函数符
        with self.assertRaises(InvalidProgramError):
            Atom("p", [object()])

    def test_uppercase_string_is_variable_or_rejected_constant(self):
        # 大写开头串是变量；事实中出现变量被拒绝。
        with self.assertRaises(InvalidProgramError):
            evaluate([Atom("p", ["X"])], [])
        # 小写串常量可用。
        r = evaluate([Atom("p", ["alice"])], [])
        self.assertIn(Atom("p", ["alice"]), r)

    def test_empty_predicate_name(self):
        with self.assertRaises(InvalidProgramError):
            Atom("", [1])


class TestRuleSafety(unittest.TestCase):
    def test_head_var_not_in_positive_body(self):
        with self.assertRaises(InvalidProgramError):
            Rule(Atom("p", ["X"]), [Atom("q", ["Y"])])

    def test_negated_body_var_not_bound(self):
        with self.assertRaises(InvalidProgramError):
            Rule(Atom("p", ["X"]),
                 [Atom("q", ["X"])],
                 [Atom("r", ["Y"])])

    def test_builtin_var_not_bound(self):
        with self.assertRaises(InvalidProgramError):
            Rule(Atom("p", ["X"]),
                 [Atom("q", ["X"]), Atom("<", ["X", "Y"], builtin="<")])

    def test_safe_negation_accepted(self):
        r = Rule(Atom("p", ["X"]),
                 [Atom("node", ["X"])],
                 [Atom("blocked", ["X"])])
        res = evaluate([("node", (1,)), ("node", (2,)), ("blocked", (2,))], [r])
        self.assertEqual(res.facts["p"], {(1,)})

    def test_ground_fact_rule(self):
        res = evaluate([], [Rule(Atom("p", [1]), [])])
        self.assertEqual(res.facts["p"], {(1,)})
        proof = res.explain(("p", (1,)))
        self.assertEqual(proof.depth, 1)
        self.assertEqual(proof.chosen.rule_index, 0)
        self.assertEqual(proof.chosen.parents, ())

    def test_negated_atom_in_body_list_rejected(self):
        with self.assertRaises(InvalidProgramError):
            Rule(Atom("p", ["X"]), [Atom("q", ["X"], negated=True)])


class TestStructuralLimits(unittest.TestCase):
    def test_arity_conflict(self):
        with self.assertRaises(InvalidProgramError):
            evaluate([("edge", (1, 2))],
                     [Rule(Atom("loop", ["X"]), [Atom("edge", ["X"])])])

    def test_arity_conflict_between_facts(self):
        with self.assertRaises(InvalidProgramError):
            evaluate([("p", (1, 2)), ("p", (3,))], [])

    def test_too_many_facts(self):
        with self.assertRaises(InvalidProgramError):
            evaluate([("p", (i,)) for i in range(MAX_FACTS + 1)], [])

    def test_facts_limit_boundary_accepted(self):
        res = evaluate([("p", (i,)) for i in range(MAX_FACTS)], [])
        self.assertEqual(res.fact_count, MAX_FACTS)

    def test_too_many_rules(self):
        rules = [Rule(Atom("p", [i]), []) for i in range(MAX_RULES + 1)]
        with self.assertRaises(InvalidProgramError):
            evaluate([], rules)

    def test_too_many_predicates(self):
        facts = [(f"p{i}", (0,)) for i in range(MAX_PREDICATES + 1)]
        with self.assertRaises(InvalidProgramError):
            evaluate(facts, [])

    def test_edb_idb_overlap_rejected(self):
        with self.assertRaises(InvalidProgramError):
            evaluate([("p", (1,))], [Rule(Atom("p", ["X"]), [Atom("q", ["X"])])])

    def test_bad_budget(self):
        for bad in (0, -1, 10**9):
            with self.assertRaises(InvalidProgramError):
                evaluate([], [], fact_budget=bad)

    def test_all_limits_together_fast(self):
        # 10 个 EDB 谓词各 200 事实 = 2000；10 个 IDB 谓词，每个由 5 条
        # 传递规则定义（5*10=100 规则，集合语义去重）。谓词总数 20。
        facts = []
        for k in range(10):
            pred = f"e{k}"
            facts.extend((pred, (i,)) for i in range(200))
        rules = []
        for k in range(10):
            dst = f"d{k}"
            for _ in range(10):
                rules.append(Rule(Atom(dst, ["X"]), [Atom(f"e{k}", ["X"])]))
        res = evaluate(facts, rules)
        self.assertEqual(res.status, "complete")
        self.assertEqual(len(res.arities), MAX_PREDICATES)
        self.assertEqual(len(rules), MAX_RULES)
        self.assertEqual(res.fact_count, 2000 + 10 * 200)


if __name__ == "__main__":
    unittest.main()
