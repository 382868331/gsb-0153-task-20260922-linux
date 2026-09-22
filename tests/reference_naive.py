"""独立朴素不动点参考实现（仅测试使用）。

本模块**不导入被测核心** ``datalog.engine``，只用数据结构
``Atom`` / ``Rule``（它们是题面固定的输入格式，不是算法）。
期望值由“每轮对全部规则做全量触发的朴素迭代”直接算出，
与半朴素 delta 实现相互独立，用于交叉验证最小模型。

只支持测试中用到的安全、可分层、无函数符子集。
"""

from dataclasses import dataclass

from datalog.model import Atom, Rule  # 仅复用输入数据结构，不接触求值算法


def _is_var(t):
    return isinstance(t, str) and bool(t) and t[0].isupper()


@dataclass
class _Program:
    edb: dict
    rules: list
    strata: dict


def _stratify(edb, rules):
    """自有实现：否定边 SCC 检测 + 凝聚 DAG 最长路定级。"""
    preds = set(edb)
    for r in rules:
        preds.add(r.head.name)
        for a in (*r.body, *r.neg_body):
            if not a.is_builtin:
                preds.add(a.name)

    # 朴素 SCC：Kosaraju（两遍 DFS），与被测的 Tarjan 相互独立。
    succ = {p: set() for p in preds}
    for r in rules:
        for a in r.body:
            if not a.is_builtin:
                succ[a.name].add(r.head.name)
        for a in r.neg_body:
            succ[a.name].add(r.head.name)
    pred = {p: set() for p in preds}
    for u, outs in succ.items():
        for v in outs:
            pred[v].add(u)

    order: list[str] = []
    seen: set[str] = set()

    def dfs1(u):
        seen.add(u)
        for v in succ[u]:
            if v not in seen:
                dfs1(v)
        order.append(u)

    for p in sorted(preds):
        if p not in seen:
            dfs1(p)

    comp: dict[str, int] = {}

    def dfs2(u, c):
        comp[u] = c
        for v in pred[u]:
            if v not in comp:
                dfs2(v, c)

    cid = 0
    for p in reversed(order):
        if p not in comp:
            dfs2(p, cid)
            cid += 1

    neg = set()
    for r in rules:
        for a in r.neg_body:
            neg.add((a.name, r.head.name))
    for a, b in neg:
        if comp[a] == comp[b]:
            raise AssertionError(f"参考实现也无法分层：{a} -> {b} 为含否定边的环")

    # 约束：正边 lv[h]>=lv[b]，负边 lv[h]>=lv[b]+1；朴素求最小不动点定级。
    level = {p: 0 for p in preds}
    changed = True
    while changed:
        changed = False
        for r in rules:
            h = r.head.name
            for a in r.body:
                if not a.is_builtin and level[h] < level[a.name]:
                    level[h] = level[a.name]
                    changed = True
            for a in r.neg_body:
                need = level[a.name] + 1
                if level[h] < need:
                    level[h] = need
                    changed = True
    return level


def _unify(atom, row, env):
    env = dict(env)
    for term, value in zip(atom.args, row, strict=True):
        if _is_var(term):
            if term in env and env[term] != value:
                return None
            env[term] = value
        elif term != value:
            return None
    return env


def _builtin_ok(atom, env):
    a, b = atom.args
    va = env.get(a) if _is_var(a) else a
    vb = env.get(b) if _is_var(b) else b
    if atom.builtin == "=":
        return va == vb
    if atom.builtin == "!=":
        return va != vb
    if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
        return False
    return va < vb


def _inst(atom, env):
    return tuple(env[t] if _is_var(t) else t for t in atom.args)


def naive_minimal_model(facts, rules, rounds: int = 10000):
    """朴素全量触发求分层最小模型，返回 {谓词: set(行)}。"""
    edb: dict[str, set[tuple]] = {}
    for f in facts:
        name, args = (f.name, tuple(f.args)) if isinstance(f, Atom) else (f[0], tuple(f[1]))
        edb.setdefault(name, set()).add(tuple(args))
    level = _stratify(edb, rules)

    rels: dict[str, set[tuple]] = {p: set() for p in level}
    for name, rows in edb.items():
        rels[name] = set(rows)

    max_level = max(level.values(), default=0)
    for s in range(max_level + 1):
        layer = [r for r in rules if level[r.head.name] == s]
        for _ in range(rounds):
            new: dict[str, set[tuple]] = {}
            for rule in layer:
                envs = [{}]
                for atom in rule.body:
                    if atom.is_builtin:
                        envs = [e for e in envs if _builtin_ok(atom, e)]
                    else:
                        envs = [u for e in envs
                                for row in sorted(rels[atom.name])
                                if (u := _unify(atom, row, e)) is not None]
                for env in envs:
                    if all(_inst(a, env) not in rels[a.name] for a in rule.neg_body):
                        new.setdefault(rule.head.name, set()).add(_inst(rule.head, env))
            added_any = False
            for name, rows in new.items():
                fresh = rows - rels[name]
                if fresh:
                    rels[name] |= fresh
                    added_any = True
            if not added_any:
                break
        else:
            raise AssertionError("参考实现在轮次上限内未达不动点")
    return {name: rows for name, rows in rels.items() if rows}
