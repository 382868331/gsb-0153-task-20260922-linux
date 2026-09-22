"""核心求值：校验、分层、半朴素 delta 迭代、最短推导 DAG。

算法概述
========

1. **校验**：谓词元数一致、EDB/IDB 不相交、规则安全（在
   :mod:`datalog.model` 中完成）、数量上限。
2. **分层**：在“体谓词 -> 头谓词”的依赖图上求 SCC（Tarjan）；
   任一 SCC 内含否定边即含负边环，抛 :class:`StratificationError`。
   凝聚 DAG 上按最长路定级：正边 ``s(头) >= s(体)``，
   负边 ``s(头) >= s(体) + 1``。
3. **逐层半朴素求值**：
   * 第 0 轮用完整关系触发本层全部规则；
   * 此后每个正体原子位置一个 delta 变体（该位置扫上一轮改进集，
     其余扫全集），即标准 semi-naive；
   * delta 不只是“新事实”，还包括“最短深度变小的事实”——不同低层
     事实的推导深度可能差异很大，事实首次出现的轮次不保证给出最短
     深度，因此深度更新必须再次传播（超图上的 Bellman-Ford 式松弛）；
   * 否定体只可能引用更低层（分层保证），按闭世界判定缺席，
     见证深度取该关系最终模型中的最大推导深度（空关系为 0）。
4. **预算**：事实总数（EDB+IDB 常驻）超过 ``fact_budget`` 时，
   当前层整体不提交，返回 ``status="incomplete"`` 与已完整的低层前缀。
"""

from __future__ import annotations

from .errors import InvalidProgramError, StratificationError
from .model import (
    DEFAULT_FACT_BUDGET,
    MAX_FACT_BUDGET,
    MAX_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
    Atom,
    Derivation,
    EDB,
    FactProof,
    Result,
    Rule,
    derivation_sort_key,
    is_variable,
)

Key = tuple[str, tuple]


# ---------------------------------------------------------------- 输入校验 ----

def _normalize_facts(facts) -> dict[str, set[tuple]]:
    if not isinstance(facts, (list, tuple)):
        raise InvalidProgramError("facts 必须是 list/tuple")
    if len(facts) > MAX_FACTS:
        raise InvalidProgramError(f"事实数 {len(facts)} 超过上限 {MAX_FACTS}")
    rels: dict[str, set[tuple]] = {}
    for i, f in enumerate(facts):
        if isinstance(f, Atom):
            if f.negated or f.is_builtin:
                raise InvalidProgramError(f"facts[{i}]: 事实必须是普通非否定原子: {f.format()}")
            if any(is_variable(t) for t in f.args):
                raise InvalidProgramError(f"facts[{i}]: 事实不得含变量: {f.format()}")
            name, args = f.name, f.args
        elif (isinstance(f, tuple) and len(f) == 2 and isinstance(f[0], str)
              and isinstance(f[1], (list, tuple))):
            name, args = f[0], tuple(f[1])
            for t in args:
                if is_variable(t):
                    raise InvalidProgramError(f"facts[{i}]: 事实不得含变量 {t!r}")
                # 复用 Atom 的标量校验。
                Atom(name, args)
        else:
            raise InvalidProgramError(
                f"facts[{i}]: 事实必须是 ground Atom 或 (谓词名, (常量, ...))"
            )
        rels.setdefault(name, set()).add(args)
    return rels


def _normalize_rules(rules) -> list[Rule]:
    if not isinstance(rules, (list, tuple)):
        raise InvalidProgramError("rules 必须是 list/tuple")
    if len(rules) > MAX_RULES:
        raise InvalidProgramError(f"规则数 {len(rules)} 超过上限 {MAX_RULES}")
    out: list[Rule] = []
    for i, r in enumerate(rules):
        if not isinstance(r, Rule):
            raise InvalidProgramError(f"rules[{i}] 不是 Rule（下标用于来源定位）")
        out.append(r)
    return out


def _check_arities(edb: dict[str, set[tuple]], rules: list[Rule]) -> dict[str, int]:
    arity: dict[str, int] = {}

    def add(name: str, n: int, where: str):
        old = arity.get(name)
        if old is not None and old != n:
            raise InvalidProgramError(
                f"{where}: 谓词 {name!r} 元数冲突：已见 {old}，此处为 {n}（谓词固定元数）"
            )
        arity[name] = n

    for name, rows in edb.items():
        n = len(next(iter(rows))) if rows else 0
        for row in rows:
            if len(row) != n:
                raise InvalidProgramError(
                    f"EDB 谓词 {name!r} 元数冲突：事实 {row!r} 有 {len(row)} 个参数，"
                    f"应为 {n}（谓词固定元数）"
                )
        add(name, n, f"EDB 谓词 {name!r}")
    for i, rule in enumerate(rules):
        add(rule.head.name, len(rule.head.args), f"rules[{i}] 头 {rule.head.format()}")
        for atom in (*rule.body, *rule.neg_body):
            if not atom.is_builtin:
                add(atom.name, len(atom.args),
                    f"rules[{i}] 体原子 {atom.format()}")
    if len(arity) > MAX_PREDICATES:
        raise InvalidProgramError(f"谓词数 {len(arity)} 超过上限 {MAX_PREDICATES}")
    return arity


# ---------------------------------------------------------------- 分层 --------

def _build_graph(rules: list[Rule]):
    """返回 (邻接表, 否定边集合)。边：体谓词 -> 头谓词。"""
    adj: dict[str, set[str]] = {}
    neg_edges: set[tuple[str, str]] = set()
    for rule in rules:
        h = rule.head.name
        adj.setdefault(h, set())
        for atom in rule.body:
            if not atom.is_builtin:
                adj.setdefault(atom.name, set()).add(h)
        for atom in rule.neg_body:
            adj.setdefault(atom.name, set()).add(h)
            neg_edges.add((atom.name, h))
    return adj, neg_edges


def _tarjan_scc(adj: dict[str, set[str]], nodes: list[str]) -> list[list[str]]:
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    counter = 0
    components: list[list[str]] = []

    def strong(v):
        nonlocal counter
        index_of[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in index_of:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index_of[w])
        if low[v] == index_of[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for v in nodes:  # 确定性：按名字序遍历
        if v not in index_of:
            strong(v)
    return components


def _path_within(adj: dict[str, set[str]], allowed: frozenset[str],
                 src: str, dst: str) -> list[str] | None:
    """在顶点集 ``allowed`` 内找一条 src -> dst 的简单路径（用于报错定位）。"""
    if src == dst:
        return [src]
    queue = [(src, [src])]
    seen = {src}
    while queue:
        node, path = queue.pop(0)
        for w in sorted(adj.get(node, ())):
            if w not in allowed or w in seen:
                continue
            if w == dst:
                return path + [w]
            seen.add(w)
            queue.append((w, path + [w]))
    return None


def _stratify(arity, edb, rules):
    adj, neg_edges = _build_graph(rules)
    for name in arity:
        adj.setdefault(name, set())
    nodes = sorted(adj)
    components = _tarjan_scc(adj, nodes)
    comp_of: dict[str, int] = {}
    for ci, comp in enumerate(components):
        for name in comp:
            comp_of[name] = ci

    # 含否定边的环：否定边两端落在同一 SCC。
    for a, b in sorted(neg_edges):
        if comp_of[a] == comp_of[b]:
            cycle = _path_within(adj, frozenset(components[comp_of[a]]), b, a)
            # cycle 为 b -> ... -> a 的路径，再接上否定边 a -> b 即闭合成环。
            chain = " -> ".join(cycle + [b]) if cycle else f"{a} -> {b}"
            raise StratificationError(
                f"依赖环含否定边，无法分层：{chain}"
                f"（其中 {a} -> {b} 为否定依赖边）"
            )

    # 凝聚 DAG 的最长路定级（边 src 体 -> dst 头）。
    dag: dict[int, set[int]] = {ci: set() for ci in range(len(components))}
    indeg: dict[int, int] = {ci: 0 for ci in range(len(components))}
    edge_extra: dict[tuple[int, int], int] = {}
    for src, outs in adj.items():
        for dst in outs:
            c1, c2 = comp_of[src], comp_of[dst]
            if c1 == c2:
                continue
            if c2 not in dag[c1]:
                dag[c1].add(c2)
                indeg[c2] += 1
            add = 1 if (src, dst) in neg_edges else 0
            prev = edge_extra.get((c1, c2), 0)
            edge_extra[(c1, c2)] = max(prev, add)

    level = [0] * len(components)
    ready = sorted(ci for ci, d in indeg.items() if d == 0)
    topo: list[int] = []
    while ready:
        ci = ready.pop(0)
        topo.append(ci)
        for c2 in sorted(dag[ci]):
            level[c2] = max(level[c2], level[ci] + edge_extra[(ci, c2)])
            indeg[c2] -= 1
            if indeg[c2] == 0:
                ready.append(c2)
                ready.sort()

    strata = {name: level[comp_of[name]] for name in arity}
    rules_by_stratum: dict[int, list[int]] = {}
    for i, rule in enumerate(rules):
        rules_by_stratum.setdefault(strata[rule.head.name], []).append(i)
    return strata, rules_by_stratum


# ---------------------------------------------------------------- 联接 --------

def _unify(atom: Atom, row: tuple, env: dict) -> dict | None:
    """把关系行 ``row`` 与原子在当前绑定 ``env`` 下合一，成功返回（可能新建的）env。"""
    new_env = env
    for term, value in zip(atom.args, row, strict=True):
        if is_variable(term):
            if term in new_env:
                if new_env[term] != value:
                    return None
            else:
                if new_env is env:
                    new_env = dict(env)
                new_env[term] = value
        elif term != value:
            return None
    return new_env


def _scan(rel, atom: Atom, env: dict):
    for row in rel:
        unified = _unify(atom, row, env)
        if unified is not None:
            yield unified


def _builtin_holds(atom: Atom, env: dict) -> bool:
    a, b = atom.args
    va = env.get(a) if is_variable(a) else a
    vb = env.get(b) if is_variable(b) else b
    op = atom.builtin
    if op == "=":
        return va == vb
    if op == "!=":
        return va != vb
    # "<"：变量未绑定（不应发生，安全规则保证）或绑定到非数值时为假。
    if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
        return False
    return va < vb


def _instantiate(atom: Atom, env: dict) -> tuple:
    return tuple(env[t] if is_variable(t) else t for t in atom.args)


# ---------------------------------------------------------------- 求值 --------

def evaluate(facts, rules, fact_budget: int = DEFAULT_FACT_BUDGET) -> Result:
    """求 Datalog 程序的（按层）最小模型。

    ``facts``：ground ``Atom`` 或 ``(谓词, (常量...))`` 的列表（集合语义去重）。
    ``rules``：:class:`Rule` 列表；其下标即来源链中的规则号。
    ``fact_budget``：正整数（非 bool），事实总数上限；超出则返回
    ``status="incomplete"`` 的 :class:`Result`，不抛异常。

    非法程序抛 :class:`InvalidProgramError` /
    :class:`StratificationError`（均为 ``ValueError`` 子类）。
    """
    if isinstance(fact_budget, bool) or not isinstance(fact_budget, int):
        raise InvalidProgramError("fact_budget 必须是 int（拒绝 bool）")
    if fact_budget <= 0 or fact_budget > MAX_FACT_BUDGET:
        raise InvalidProgramError(
            f"fact_budget 必须是 1..{MAX_FACT_BUDGET} 之间的整数，得到 {fact_budget}"
        )

    edb = _normalize_facts(facts)
    rule_list = _normalize_rules(rules)
    arity = _check_arities(edb, rule_list)
    strata, rules_by_stratum = _stratify(arity, edb, rule_list)

    edb_preds = frozenset(edb)
    for i, rule in enumerate(rule_list):
        if rule.head.name in edb_preds:
            raise InvalidProgramError(
                f"rules[{i}] 头谓词 {rule.head.name!r} 同时由 EDB 事实定义："
                "EDB 与 IDB 谓词必须不相交"
            )

    # 全局状态。
    rels: dict[str, set[tuple]] = {name: set(rows) for name, rows in edb.items()}
    for name in arity:
        rels.setdefault(name, set())
    proof: dict[Key, FactProof] = {}
    for name, rows in edb.items():
        edb_deriv = Derivation(None, ())
        for row in rows:
            proof[(name, row)] = FactProof(0, (edb_deriv,), edb_deriv)

    base_total = sum(len(v) for v in rels.values())
    if base_total > fact_budget:
        # 输入事实本身已超过总预算：完成在数学上不可能，这是可定位的
        # 配置错误（而非搜索预算耗尽，也不是证无解），直接报错。
        raise InvalidProgramError(
            f"EDB 事实数 {base_total} 已超过 fact_budget={fact_budget}："
            "完整模型不可能容纳，请调大预算或减少输入事实"
        )
    witness: dict[str, int] = {}
    for name in sorted(edb):
        witness[name] = 0  # EDB 推导深度为 0（含空关系：见证深度 0）

    status = "complete"
    stopped_at: int | None = None

    for s in sorted(rules_by_stratum):
        idxs = rules_by_stratum[s]
        stratum_preds = {rule_list[j].head.name for j in idxs}
        added, new_proof, ok = _eval_stratum(
            s, idxs, rule_list, rels, proof, strata, witness, fact_budget,
            base_total,
        )
        if not ok:
            status = "incomplete"
            stopped_at = s
            break
        # 整层提交（原子性：层失败不留部分变更）。
        for name, rows in added.items():
            rels[name].update(rows)
        proof.update(new_proof)
        base_total += sum(len(v) for v in added.values())
        for name in stratum_preds:
            witness[name] = max(
                (proof[(name, r)].depth for r in rels[name]), default=0)

    facts_view = {name: frozenset(rows) for name, rows in rels.items() if rows}
    return Result(
        status=status,
        facts=facts_view,
        proof=proof,
        strata=dict(strata),
        edb_predicates=edb_preds,
        budget=fact_budget,
        fact_count=sum(len(v) for v in facts_view.values()),
        stopped_at_stratum=stopped_at,
        rules=tuple(rule_list),
        witness_depth=dict(witness),
        arities=dict(arity),
    )


def _eval_stratum(s, idxs, rule_list, rels, proof, strata, witness,
                  budget, base_total):
    """求一个层的最小模型。

    成功返回 ``(added, new_proof, True)``；预算超限返回
    ``(空, 空, False)``，且不修改全局 ``rels`` / ``proof``。
    """
    added: dict[str, set[tuple]] = {}
    local_depth: dict[Key, int] = {}
    local_derivs: dict[Key, set[Derivation]] = {}

    def depth_of(key: Key) -> int:
        if key in local_depth:
            return local_depth[key]
        return proof[key].depth

    def full_rel(pred: str) -> set[tuple]:
        new_rows = added.get(pred)
        if not new_rows:
            return rels[pred]
        return rels[pred] | new_rows

    # candidates[head] = list[(depth, Derivation)]，一轮内汇总后统一松弛。
    def fire(rule_i: int, delta_pos: int | None, delta_rows: set[tuple] | None):
        rule = rule_list[rule_i]
        ordinary = [(k, a) for k, a in enumerate(rule.body) if not a.is_builtin]
        envs: list[dict] = [{}]
        for ord_pos, (_, atom) in enumerate(ordinary):
            if ord_pos == delta_pos:
                scan_rel = delta_rows
            else:
                scan_rel = full_rel(atom.name)
            envs = [e2 for e in envs for e2 in _scan(scan_rel, atom, e)]
            if not envs:
                return
        for env in envs:
            if not all(_builtin_holds(a, env) for a in rule.body if a.is_builtin):
                continue
            parents: list = []
            depth = 0
            bad = False
            for _, atom in ordinary:
                row = _instantiate(atom, env)
                key = (atom.name, row)
                if key not in proof and key not in local_depth:
                    bad = True  # 扫自全集，理论上不会发生；防御。
                    break
                parents.append((atom.name, row, True))
                depth = max(depth, depth_of(key))
            if bad:
                continue
            for atom in rule.neg_body:
                row = _instantiate(atom, env)
                if row in full_rel(atom.name):
                    bad = True
                    break
                parents.append((atom.name, row, False))
                depth = max(depth, witness.get(atom.name, 0))
            if bad:
                continue
            head_args = _instantiate(rule.head, env)
            head = (rule.head.name, head_args)
            deriv = Derivation(rule_i, tuple(parents))
            cand.setdefault(head, []).append((depth + 1, deriv))

    # 第 0 轮：完整触发。
    cand: dict[Key, list] = {}
    for rule_i in idxs:
        fire(rule_i, None, None)
    delta = _apply_candidates(cand, added, local_depth, local_derivs)

    while delta:
        if base_total + sum(len(v) for v in added.values()) > budget:
            return {}, {}, False
        cand = {}
        for rule_i in idxs:
            rule = rule_list[rule_i]
            ordinary = [(k, a) for k, a in enumerate(rule.body)
                        if not a.is_builtin]
            for ord_pos, (_, atom) in enumerate(ordinary):
                if strata[atom.name] != s:
                    continue  # 低层谓词在本层无 delta
                rows = delta.get(atom.name)
                if not rows:
                    continue
                fire(rule_i, ord_pos, rows)
        delta = _apply_candidates(cand, added, local_depth, local_derivs)

    if base_total + sum(len(v) for v in added.values()) > budget:
        return {}, {}, False

    new_proof: dict[Key, FactProof] = {}
    for (name, row), derivs in local_derivs.items():
        ordered = tuple(sorted(derivs, key=derivation_sort_key))
        new_proof[(name, row)] = FactProof(
            local_depth[(name, row)], ordered, ordered[0])
    return added, new_proof, True


def _apply_candidates(cand, added, local_depth, local_derivs):
    """把一轮候选按最短深度松弛进本层状态，返回下一轮 delta（按谓词分组）。"""
    delta: dict[str, set[tuple]] = {}
    for head, entries in cand.items():
        name, row = head
        min_d = min(d for d, _ in entries)
        cur = local_depth.get(head)
        if cur is None:
            local_depth[head] = min_d
            local_derivs[head] = {d2 for d, d2 in entries if d == min_d}
            added.setdefault(name, set()).add(row)
            delta.setdefault(name, set()).add(row)
        elif min_d < cur:
            local_depth[head] = min_d
            local_derivs[head] = {d2 for d, d2 in entries if d == min_d}
            delta.setdefault(name, set()).add(row)
        elif min_d == cur:
            # 等深度新推导：成员身份与他者推导深度均不变，故不放入 delta。
            local_derivs[head].update(d2 for d, d2 in entries if d == cur)
    return delta
