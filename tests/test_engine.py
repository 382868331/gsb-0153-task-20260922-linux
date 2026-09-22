"""核心求值测试：递归可达、否定分层、重复推导、最短推导 DAG、预算。"""

import random
import unittest

from datalog import (
    Atom,
    EvaluationIncomplete,
    InvalidProgramError,
    Rule,
    StratificationError,
    evaluate,
)
from tests.reference_naive import naive_minimal_model
from datalog.model import derivation_sort_key


def A(name, *args, **kw):
    return Atom(name, list(args), **kw)


class TestRecursion(unittest.TestCase):
    def test_transitive_closure(self):
        facts = [("edge", (u, v)) for u, v in
                 [(1, 2), (2, 3), (3, 4), (4, 5), (1, 6)]]
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.status, "complete")
        expected = {
            (1, 2), (2, 3), (3, 4), (4, 5), (1, 6),
            (1, 3), (1, 4), (1, 5), (2, 4), (2, 5), (3, 5),
        }
        self.assertEqual(res.facts["reach"], expected)

    def test_cycle_terminates(self):
        # 自环与双向环：集合语义 + 不动点必须终止。
        facts = [("edge", (1, 2)), ("edge", (2, 1)), ("edge", (2, 2))]
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.facts["reach"], {(1, 1), (1, 2), (2, 1), (2, 2)})

    def test_same_generation_mutual_recursion(self):
        # same_generation：sg(X,X) :- node(X); sg(X,Y) :- edge(X,A), sg(A,B), edge(Y,B)
        facts = [("node", (i,)) for i in range(1, 7)]
        facts += [("edge", (1, 3)), ("edge", (2, 4)),
                  ("edge", (3, 5)), ("edge", (4, 6))]
        rules = [
            Rule(A("sg", "X", "X"), [A("node", "X")]),
            Rule(A("sg", "X", "Y"),
                 [A("edge", "X", "A"), A("sg", "A", "B"), A("edge", "Y", "B")]),
        ]
        res = evaluate(facts, rules)
        ref = naive_minimal_model(facts, rules)
        self.assertEqual(res.facts["sg"], ref["sg"])

    def test_random_graphs_against_naive(self, n=25, seed=20260922):
        rng = random.Random(seed)
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"),
                 [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        for trial in range(n):
            nv = rng.randint(1, 8)
            facts = [("edge", (u, v))
                     for u in range(nv) for v in range(nv)
                     if rng.random() < 0.28]
            res = evaluate(facts, rules)
            ref = naive_minimal_model(facts, rules)
            self.assertEqual(res.facts.get("reach", frozenset()),
                             ref.get("reach", set()),
                             msg=f"trial {trial}, {facts}")


class TestStratifiedNegation(unittest.TestCase):
    def test_basic_stratified(self):
        # p := 所有未被 blocked 的 node
        facts = [("node", (i,)) for i in range(1, 6)]
        facts += [("blocked", (2,)), ("blocked", (4,))]
        rules = [Rule(A("ok", "X"), [A("node", "X")], [A("blocked", "X")])]
        res = evaluate(facts, rules)
        self.assertEqual(res.strata["ok"], 1)
        self.assertEqual(res.facts["ok"], {(1,), (3,), (5,)})

    def test_three_strata(self):
        # reach 层1；good(X) :- reach(a,X), !bad(X) 层1(neg edb);
        # answer(X) :- good(X), !excluded(X) 其中 excluded 依赖 good（否定）-> 层3
        facts = [("edge", ("a", "b")), ("edge", ("b", "c")), ("edge", ("c", "d")),
                 ("bad", ("c",)), ("excluded_src", ("d",))]
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"),
                 [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
            Rule(A("good", "X"),
                 [A("reach", "A", "X")], [A("bad", "X")]),
            Rule(A("excluded", "X"),
                 [A("good", "X"), A("excluded_src", "X")]),
            Rule(A("answer", "X"),
                 [A("good", "X")], [A("excluded", "X")]),
        ]
        res = evaluate(facts, rules)
        ref = naive_minimal_model(facts, rules)
        for pred in ("reach", "good", "excluded", "answer"):
            self.assertEqual(set(res.facts.get(pred, ())), ref.get(pred, set()),
                             msg=pred)
        self.assertEqual(res.facts["answer"], {("b",)})
        self.assertGreaterEqual(res.strata["answer"], res.strata["excluded"] + 1)

    def test_negative_cycle_rejected(self):
        # p(X) :- q(X), !r(X);  r(X) :- p(X)  —— 含否定边的环。
        rules = [
            Rule(A("p", "X"), [A("q", "X")], [A("r", "X")]),
            Rule(A("r", "X"), [A("p", "X")]),
        ]
        with self.assertRaises(StratificationError):
            evaluate([("q", (1,))], rules)

    def test_negative_self_loop_rejected(self):
        rules = [Rule(A("p", "X"), [A("q", "X")], [A("p", "X")])]
        with self.assertRaises(StratificationError):
            evaluate([("q", (1,))], rules)

    def test_positive_cycle_with_negation_outside_is_fine(self):
        # p,q 正互递归同层；r 依赖 !p 在更高层。
        facts = [("seed", (1,)), ("seed", (2,))]
        rules = [
            Rule(A("p", "X"), [A("seed", "X"), A("q", "X")]),
            Rule(A("q", "X"), [A("seed", "X")]),
            Rule(A("r", "X"), [A("seed", "X")], [A("p", "X")]),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.strata["p"], res.strata["q"])
        self.assertGreater(res.strata["r"], res.strata["p"])
        # q 由 seed 直接得；p 需要 q，第一轮后才有，故 r 为空。
        self.assertEqual(res.facts.get("r", frozenset()), set())

    def test_negation_random_against_naive(self, seed=77):
        rng = random.Random(seed)
        # 固定的可分层程序模板：
        # e(X,Y) EDB；tc；unreachable := node,!tc-from-0；isolated_pair...
        for trial in range(12):
            nv = rng.randint(2, 7)
            facts = [("node", (i,)) for i in range(nv)]
            facts += [("edge", (u, v)) for u in range(nv) for v in range(nv)
                      if u != v and rng.random() < 0.3]
            facts.append(("root", (0,)))
            rules = [
                Rule(A("tc", "X", "Y"), [A("edge", "X", "Y")]),
                Rule(A("tc", "X", "Y"),
                     [A("edge", "X", "Z"), A("tc", "Z", "Y")]),
                Rule(A("reachable", "X"),
                     [A("root", "R"), A("tc", "R", "X")]),
                Rule(A("unreachable", "X"),
                     [A("node", "X")], [A("reachable", "X")]),
                # 孤立点：没有任何入边或出边
                Rule(A("hasout", "X"), [A("edge", "X", "Y")]),
                Rule(A("hasin", "Y"), [A("edge", "X", "Y")]),
                Rule(A("isolated", "X"),
                     [A("node", "X")], [A("hasout", "X"), A("hasin", "X")]),
            ]
            res = evaluate(facts, rules)
            ref = naive_minimal_model(facts, rules)
            for pred in ("tc", "reachable", "unreachable", "isolated"):
                self.assertEqual(set(res.facts.get(pred, ())),
                                 ref.get(pred, set()),
                                 msg=f"trial {trial} pred {pred}")


class TestBuiltins(unittest.TestCase):
    def test_comparisons(self):
        facts = [("val", (i,)) for i in range(1, 8)]
        rules = [
            Rule(A("big", "X"), [A("val", "X"),
                                 Atom("<", [4, "X"], builtin="<")]),
            Rule(A("nonself_pair", "X", "Y"),
                 [A("val", "X"), A("val", "Y"),
                  Atom("!=", ["X", "Y"], builtin="!=")]),
            Rule(A("equals3", "X"),
                 [A("val", "X"), Atom("=", ["X", 3], builtin="=")]),
        ]
        res = evaluate(facts, rules)
        self.assertEqual(res.facts["big"], {(5,), (6,), (7,)})
        self.assertEqual(res.facts["equals3"], {(3,)})
        self.assertEqual(len(res.facts["nonself_pair"]), 7 * 6)

    def test_against_naive_builtins(self):
        facts = [("val", (i,)) for i in range(6)]
        rules = [
            Rule(A("q", "X", "Y"),
                 [A("val", "X"), A("val", "Y"),
                  Atom("<", ["X", "Y"], builtin="<")]),
        ]
        res = evaluate(facts, rules)
        ref = naive_minimal_model(facts, rules)
        self.assertEqual(set(res.facts["q"]), ref["q"])


class TestDuplicateDerivations(unittest.TestCase):
    def setUp(self):
        # 菱形：a->b,a->c,b->d,c->d。d(5) 可从多条路径推出。
        self.facts = [("edge", (1, 2)), ("edge", (1, 3)),
                      ("edge", (2, 4)), ("edge", (3, 4)),
                      ("edge", (0, 1))]
        self.rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"),
                 [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        self.res = evaluate(self.facts, self.rules)

    def test_set_semantics_no_duplicate_facts(self):
        self.assertEqual(len(self.res.facts["reach"]),
                         len({r for r in self.res.facts["reach"]}))

    def test_shortest_proof_depth(self):
        # reach(1,4) 最短 2 步；reach(0,4) 最短 3 步。
        self.assertEqual(self.res.explain(("reach", (1, 4))).depth, 2)
        self.assertEqual(self.res.explain(("reach", (0, 4))).depth, 3)
        self.assertEqual(self.res.explain(("reach", (2, 4))).depth, 1)

    def test_all_shortest_derivations_recorded(self):
        # reach(1,4) 在深度 2 恰有 2 条最短推导（经 2、经 3）。
        proof = self.res.explain(("reach", (1, 4)))
        self.assertEqual(proof.depth, 2)
        self.assertEqual(len(proof.derivations), 2)
        parents_sets = {d.parents for d in proof.derivations}
        self.assertEqual(len(parents_sets), 2)
        # 每条推导都标注规则号与有序父节点。
        for d in proof.derivations:
            self.assertEqual(d.rule_index, 1)
            self.assertEqual(len(d.parents), 2)
            self.assertTrue(all(p[2] is True for p in d.parents))

    def test_lexicographically_smallest_chosen(self):
        proof = self.res.explain(("reach", (1, 4)))
        ordered = sorted(proof.derivations, key=derivation_sort_key)
        self.assertEqual(proof.chosen, ordered[0])
        # 字典序：edge(1,2) 先于 edge(1,3)。
        edge_parent = proof.chosen.parents[0]
        self.assertEqual(edge_parent[1], (1, 2))

    def test_dag_parents_exist_and_shallower(self):
        for (name, row), p in self.res.proof.items():
            for d in p.derivations:
                for pn, prow, pos in d.parents:
                    self.assertTrue(
                        pos, msg="正递归程序中不应出现否定见证")
                    sub = self.res.proof[(pn, prow)]
                    self.assertLess(sub.depth, p.depth)

    def test_depth_rerelaxation(self):
        # 构造“晚到的更短推导”：
        # 直达边 1->4 在多跳路径之后才在规则顺序中生效不影响结果（规则无序）；
        # 改为：长链与短路径并存，深度必须取最短。
        facts = [("edge", (1, 2)), ("edge", (2, 3)), ("edge", (3, 4)),
                 ("edge", (1, 4))]
        res = evaluate(facts, self.rules)
        self.assertEqual(res.explain(("reach", (1, 4))).depth, 1)


class TestProvenanceChain(unittest.TestCase):
    def test_edb_provenance(self):
        res = evaluate([("edge", (1, 2))], [])
        p = res.explain(("edge", (1, 2)))
        self.assertEqual(p.depth, 0)
        self.assertIsNone(p.chosen.rule_index)
        self.assertEqual(p.chosen.parents, ())

    def test_negation_witness_in_proof(self):
        facts = [("node", (1,)), ("node", (2,)), ("blocked", (2,))]
        rules = [Rule(A("ok", "X"), [A("node", "X")], [A("blocked", "X")])]
        res = evaluate(facts, rules)
        p = res.explain(("ok", (1,)))
        self.assertEqual(len(p.chosen.parents), 2)
        node_parent, witness = p.chosen.parents
        self.assertEqual(node_parent, ("node", (1,), True))
        self.assertEqual(witness, ("blocked", (1,), False))

    def test_query_and_provenance(self):
        facts = [("edge", (1, 2)), ("edge", (2, 3))]
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"),
                 [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        res = evaluate(facts, rules)
        q = res.query(A("reach", "X", 3))
        self.assertEqual(q.variables, ("X",))
        self.assertEqual(q.rows, ((1,), (2,)))
        proof = q.provenance((1,))
        self.assertEqual(proof.depth, 2)

    def test_proven_absence_is_keyerror_not_false_negative(self):
        res = evaluate([("node", (1,))],
                       [Rule(A("ok", "X"), [A("node", "X")],
                             [A("blocked", "X")])])
        # blocked(1) 在完整模型中确证不存在。
        with self.assertRaises(KeyError):
            res.explain(("blocked", (1,)))
        self.assertNotIn(("blocked", (1,)), res)


class TestBudget(unittest.TestCase):
    def _chain_program(self, n, budget):
        facts = [("seed", (0,))] + [("succ", (i, i + 1)) for i in range(n - 1)]
        rules = [
            Rule(A("path", "X"), [A("seed", "X")]),
            Rule(A("path", "Y"),
                 [A("path", "X"), A("succ", "X", "Y")]),
        ]
        return evaluate(facts, rules, fact_budget=budget)

    def test_complete_under_budget(self):
        res = self._chain_program(5, budget=100)
        self.assertEqual(res.status, "complete")
        self.assertEqual(len(res.facts["path"]), 5)

    def test_incomplete_is_distinct_from_failure(self):
        # EDB 9 个；path 派生长度受预算限制，第 0 层无法求完。
        res = self._chain_program(8, budget=12)
        self.assertEqual(res.status, "incomplete")
        self.assertEqual(res.stopped_at_stratum, 0)
        # 未完成层查询必须报错，而不是返回“查无”。
        with self.assertRaises(EvaluationIncomplete):
            res.query(A("path", "X"))
        with self.assertRaises(EvaluationIncomplete):
            _ = A("path", 99) in res
        # EDB 仍然可读（已完整的低层前缀）。
        self.assertIn(("seed", (0,)), res)
        self.assertEqual(len(res.query(A("succ", "X", "Y"))), 7)

    def test_budget_boundary_exact(self):
        # EDB=3 (1 seed + 2 succ)；path 恰好 3 个 -> 总 6。
        res = self._chain_program(3, budget=6)
        self.assertEqual(res.status, "complete")
        res2 = self._chain_program(4, budget=6)
        self.assertEqual(res2.status, "incomplete")

    def test_incomplete_result_carries_counts(self):
        res = self._chain_program(10, budget=15)
        self.assertEqual(res.status, "incomplete")
        self.assertLessEqual(res.fact_count, res.budget)

    def test_edb_over_budget_is_locatable_error(self):
        facts = [("p", (i,)) for i in range(10)]
        # EDB 自身超限：完成不可能，属于可定位配置错误，不冒充搜索耗尽。
        with self.assertRaises(InvalidProgramError):
            evaluate(facts, [], fact_budget=5)

    def test_no_partial_stratum_committed(self):
        # 预算超限时，低层完整、高层不可见；且同层不出现半层结果。
        facts = [("node", (i,)) for i in range(6)]
        rules = [
            Rule(A("big", "X"),
                 [A("node", "X"), Atom("<", [2, "X"], builtin="<")]),
            Rule(A("huge", "X"),
                 [A("node", "X"), Atom("<", [4, "X"], builtin="<")]),
        ]
        res = evaluate(facts, rules, fact_budget=7)  # EDB=6，只剩 1 个名额
        self.assertEqual(res.status, "incomplete")
        # big 有 3 个、huge 有 1 个，无法整层放入 -> 同层全部不提交。
        self.assertNotIn("big", res.facts)
        self.assertNotIn("huge", res.facts)
        self.assertEqual(res.fact_count, 6)


class TestQueryContract(unittest.TestCase):
    def test_unknown_predicate_rejected(self):
        res = evaluate([("p", (1,))], [])
        with self.assertRaises(InvalidProgramError):
            res.query(A("q", "X"))
        with self.assertRaises(InvalidProgramError):
            _ = A("q", 1) in res

    def test_arity_mismatch_rejected(self):
        res = evaluate([("p", (1, 2))], [])
        with self.assertRaises(InvalidProgramError):
            res.query(A("p", "X"))
        with self.assertRaises(InvalidProgramError):
            _ = ("p", (1,)) in res

    def test_repeated_variable_equality(self):
        facts = [("t", (1, 1)), ("t", (1, 2)), ("t", (2, 2))]
        res = evaluate(facts, [])
        self.assertEqual(res.query(A("t", "X", "X")).rows, ((1,), (2,)))

    def test_ground_query(self):
        res = evaluate([("p", (1,)), ("p", (2,))], [])
        self.assertEqual(len(res.query(A("p", 1))), 1)
        self.assertEqual(len(res.query(A("p", 9))), 0)

    def test_float_comparison(self):
        facts = [("v", (1.5,)), ("v", (2.0,)), ("v", (0.25,))]
        rules = [Rule(A("w", "X"), [A("v", "X"),
                                    Atom("<", [1.0, "X"], builtin="<")])]
        res = evaluate(facts, rules)
        self.assertEqual(res.facts["w"], {(1.5,), (2.0,)})

    def test_int_float_equal_constant_semantics(self):
        # 文档化的 Python 语义：1 == 1.0。
        res = evaluate([("p", (1.0,))], [])
        self.assertIn(("p", (1,)), res)


class TestChosenAcrossRules(unittest.TestCase):
    def test_rule_index_breaks_tie(self):
        # 同一事实可由两条不同规则一步推出；chosen 取规则下标小者。
        facts = [("a", (1,)), ("b", (1,))]
        rules = [
            Rule(A("p", "X"), [A("b", "X")]),   # 规则 0
            Rule(A("p", "X"), [A("a", "X")]),   # 规则 1
        ]
        res = evaluate(facts, rules)
        proof = res.explain(("p", (1,)))
        self.assertEqual(proof.depth, 1)
        self.assertEqual(len(proof.derivations), 2)
        self.assertEqual(proof.chosen.rule_index, 0)

    def test_shorter_rule_path_wins_over_index(self):
        # 规则下标大但推导更短 -> 深度优先，再比字典序。
        facts = [("a", (1,)), ("m", (1, 2)), ("m", (2, 3))]
        rules = [
            # 规则 0：两跳（经 q）
            Rule(A("q", "Y"), [A("m", "X", "Y")]),
            Rule(A("p", "Y"), [A("m", "X", "Y"), A("q", "Y")]),
            # 规则 2：a 直接一步
            Rule(A("p", "X"), [A("a", "X")]),
        ]
        res = evaluate(facts, rules)
        proof = res.explain(("p", (1,)))
        self.assertEqual(proof.depth, 1)
        self.assertEqual(proof.chosen.rule_index, 2)


class TestIdempotence(unittest.TestCase):
    def test_repeated_evaluate_isolated(self):
        facts = [("edge", (1, 2))]
        rules = [Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")])]
        r1 = evaluate(facts, rules)
        r2 = evaluate(facts, rules)
        self.assertEqual(r1.facts, r2.facts)
        # 输入未被修改。
        self.assertEqual(facts, [("edge", (1, 2))])

    def test_roundtrip_query_covers_facts(self):
        # 补充性检查：query(全变量) 的行集合 == facts 关系。
        facts = [("edge", (u, v)) for u in range(4) for v in range(4) if u != v]
        rules = [
            Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
            Rule(A("reach", "X", "Y"),
                 [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        ]
        res = evaluate(facts, rules)
        qrows = set(res.query(A("reach", "X", "Y")).rows)
        self.assertEqual(qrows, set(res.facts["reach"]))


if __name__ == "__main__":
    unittest.main()
