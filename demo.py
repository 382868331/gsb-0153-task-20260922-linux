"""固定输入演示：分层否定 + 递归可达的查询结果与来源链。

运行：``python demo.py``（纯标准库，离线，秒级完成）。

演示内容：
1. 正常结果：城市航线网络上求递归可达 ``reach``，再用分层否定求
   “必须中转才能到达”的城市 ``requires_transfer``，打印查询结果；
2. 对其中一条派生事实打印字典序最小最短推导的来源链；
3. 实际触发的拒绝边界：含否定边依赖环的程序被 ``StratificationError``
   拒绝；非安全规则（头变量未在正体出现）被 ``InvalidProgramError`` 拒绝；
4. 预算边界：极小 ``fact_budget`` 下返回 ``status="incomplete"``，
   查询未求完谓词抛 ``EvaluationIncomplete``——与完整模型下确证不存在的
   ``KeyError`` 明确区分。

数据全部为本文件内的确定性常量/代码生成，无网络、无 sleep、无预录结果。
"""

from datalog import (
    Atom,
    EvaluationIncomplete,
    InvalidProgramError,
    Rule,
    StratificationError,
    evaluate,
)
from datalog.provenance import all_facts_with_depth, format_chain


def A(name, *args, **kwargs):
    return Atom(name, list(args), **kwargs)


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def build_program():
    # 确定性数据：8 个城市 c0..c7 的有向航线（邻接+一条快线，构成菱形多路径）。
    edges = [
        ("c0", "c1"), ("c1", "c2"), ("c2", "c3"),  # 主干链
        ("c0", "c4"), ("c4", "c2"),                 # c0->c2 的另一条路径
        ("c3", "c5"), ("c5", "c6"), ("c2", "c7"),   # 支线
    ]
    facts = [("edge", (u, v)) for u, v in edges]
    rules = [
        # reach(X,Y) 传递闭包（正递归）。
        Rule(A("reach", "X", "Y"), [A("edge", "X", "Y")]),
        Rule(A("reach", "X", "Y"),
             [A("edge", "X", "Z"), A("reach", "Z", "Y")]),
        # requires_transfer(X,Y)：可达但没有直飞 —— 第 1 层（否定 EDB）。
        Rule(A("requires_transfer", "X", "Y"),
             [A("reach", "X", "Y")], [A("edge", "X", "Y")]),
    ]
    return facts, rules


def demo_normal():
    section("1. 正常求值：从 c0 出发必须中转才能到达的城市")
    facts, rules = build_program()
    res = evaluate(facts, rules)
    print(f"status = {res.status}；总事实数 = {res.fact_count}")
    print("分层：", {p: res.strata[p] for p in ("edge", "reach", "requires_transfer")})

    q = res.query(A("requires_transfer", "c0", "Y"))
    rows = [row[0] for row in q.rows]
    print(f"查询 requires_transfer('c0', Y) -> {rows}")
    print("（c1/c4 直飞被否定排除；c2 虽可直飞？否——无 c0-c2 直飞边，故入选）")

    target = "c2"
    print(f"\n派生事实 requires_transfer('c0', {target!r}) 的最短推导来源链：")
    print(format_chain(res, "requires_transfer", ("c0", target)))

    print("\nreach('c0', Y) 全部答案（城市, 最短深度, 最短推导数）：")
    for row, depth, n_deriv in all_facts_with_depth(res, "reach"):
        if row[0] == "c0":
            print(f"  reach('c0', {row[1]!r}): depth={depth}, 最短推导 {n_deriv} 条")


def demo_rejection():
    section("2. 实际触发的拒绝/异常边界")

    # 2a. 含否定边的依赖环：p(X) :- q(X), !r(X);  r(X) :- p(X)
    print("2a. 含否定边的环（p 依赖 !r，r 又依赖 p）：")
    bad_rules = [
        Rule(A("p", "X"), [A("q", "X")], [A("r", "X")]),
        Rule(A("r", "X"), [A("p", "X")]),
    ]
    try:
        evaluate([("q", (1,))], bad_rules)
    except StratificationError as exc:
        print(f"    捕获 {type(exc).__name__}：{exc}")

    # 2b. 非安全规则：头变量 Z 未在正体出现。
    print("\n2b. 非安全规则（头变量 Z 未在正普通体中出现）：")
    try:
        Rule(A("p", "X", "Z"), [A("q", "X")])
    except InvalidProgramError as exc:
        print(f"    捕获 {type(exc).__name__}：{exc}")


def demo_budget_vs_absence():
    section("3. 预算耗尽（未知）≠ 证明无解（确证不存在）")
    # 链状派生：seed(0) + succ(0,1)..(5,6)，path 沿链增长。
    facts = [("seed", (0,))] + [("succ", (i, i + 1)) for i in range(6)]
    rules = [
        Rule(A("path", "X"), [A("seed", "X")]),
        Rule(A("path", "Y"), [A("path", "X"), A("succ", "X", "Y")]),
    ]
    # EDB=7，只给 9 的总预算 -> path 不可能求完整层。
    res = evaluate(facts, rules, fact_budget=9)
    print(f"fact_budget=9 -> status={res.status}, stopped_at_stratum="
          f"{res.stopped_at_stratum}, fact_count={res.fact_count}")
    print("（层失败时整层不提交：path 不出现在部分模型中，EDB 仍可查）")
    print("  succ 行数（EDB 可正常查询）：", len(res.query(A("succ", "X", "Y"))))
    try:
        res.query(A("path", "X"))
    except EvaluationIncomplete as exc:
        print(f"  查询未求完谓词捕获 {type(exc).__name__}：{exc}")

    # 对照：完整模型下确证不存在 -> KeyError，而不是“未知”。
    full = evaluate(facts, rules)
    try:
        full.explain(("path", (100,)))
    except KeyError as exc:
        print(f"  完整模型中 explain(path(100)) 捕获 {type(exc).__name__}：{exc}")
    print(f"  完整模型 status={full.status}，path 共 {len(full.facts['path'])} 个事实")


def main():
    print("datalog 分层否定规则求值演示（Python 标准库，离线确定性计算）")
    demo_normal()
    demo_rejection()
    demo_budget_vs_absence()
    section("演示结束")
    print("以上结果均为现场计算；测试另见 python -m unittest discover -s tests -v")


if __name__ == "__main__":
    main()
