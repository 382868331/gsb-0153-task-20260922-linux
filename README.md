# datalog — 带否定分层的规则查询求值模块（v2：增量会话）

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器。提供两代公开入口：

- **v1（保持不变）**：`evaluate(facts, rules)` 一次性求最小模型，为每条派生
  事实记录字典序最小的最短推导 DAG。
- **v2（本次新增）**：`Session` 持续接收**事实增删批次**（多来源、原子），
  正递归分量用 DRed（过度删除 + 重新推导）做失效传播，否定分量按依赖顺序
  重算；支持**原子替换整个规则集**（先验证后发布、版本化快照）；附带独立
  **证据检查器**，从仍存在的基事实向上验证，不信任求解器的任何布尔标志。

- **Python 版本**：3.14.7（仅标准库，无第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始

```bash
python -m unittest discover -s tests -v     # 快速回归（v1 + v2，约 5 秒）
DATALOG_RUN_EXTENDED=1 python -m unittest discover -s tests -v
                                            # 快速回归 + 扩展验证（约 2.5 分钟）
python demo.py                              # v1 演示（约 0.1 秒）
python demo_incremental.py                  # v2 端到端演示（约 15 秒，上限 90 秒）
```

扩展验证在 `tests/test_scale.py`：100 条规则、10 000 条基事实、100 个更新
批次（每批做证据校验，检查点与独立朴素参考全量比对），以及"大无关子图 +
小循环修改"的零重算验证。默认不运行，设 `DATALOG_RUN_EXTENDED=1` 后开启。

## v1 接口（原有行为，逐字保留）

```python
from datalog import evaluate, Rule, Var, pos, neg

X, Y, Z = Var("X"), Var("Y"), Var("Z")
facts = [("edge", "a", "b"), ("edge", "b", "c")]
rules = [
    Rule(("reach", X, Y), [pos("edge", X, Y)]),
    Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
]
res = evaluate(facts, rules, max_facts=100_000)
res.complete            # 是否算出完整最小模型
res.reason              # 未完成原因（complete=True 时为 None）
res.facts / res.derived # frozenset：全部事实 / 仅派生事实
res.query("reach", "a", X)
res.derivation(("reach", "a", "c"))
res.explain(("reach", "a", "c"))
```

v1 的输入上限（接口边界，未变）：20 谓词 / 100 规则 / 2000 输入事实 /
`max_facts` 默认 100 000。错误语义未变：`ValidationError` /
`UnsafeRuleError` / `LimitError` / `StratificationError`，均可定位。
`max_facts` 超支时该轮整体不应用，返回 `complete=False` 的明确未完成结果。

## v2 接口：增量会话

```python
from datalog import Session, Rule, Var, pos, neg, verify_snapshot

s = Session(rules, facts=[(("edge", "a", "b"), "sensor-1")], max_facts=100_000)
rep = s.apply_batch(
    adds=[(("edge", "b", "c"), "sensor-2"), ("node", "d")],   # 裸事实用默认来源
    removes=[(("edge", "a", "b"), "sensor-1")],
)
rep.applied        # False 表示整批回滚（预算超支），状态与调用前完全一致
rep.added / rep.removed        # 本批净增/净减事实（frozenset）
rep.over_deleted / rep.rederived  # DRed 工作集： tentative 删除 / 其中被重新推导
rep.recomputed     # 被整体重算的分量谓词名（否定依赖变化的层）
rep.firings        # {规则序号: 本批触发次数}；无关规则不出现（零重算可核查）
rep.retractions    # 每条撤回事实的 Retraction(fact, cause, detail)

snap = s.result()  # 不可变快照，接口与 v1 Result 一致，另有：
snap.rules_version # 规则集版本号
snap.sources(fact) # 基事实的来源 ID 集合（派生事实为空集）
snap.base_sources  # {基事实: 来源集合} 映射副本

rep2 = s.replace_rules(new_rules)   # 原子替换规则集；先验证后发布
rep2.added / rep2.removed / rep2.retractions / rep2.rules_version
```

### 输入字段与边界含义（缺省已固定）

- **批次操作**：`adds` / `remove` 中每项是 `事实` 或 `(事实, 来源ID)`。
  事实是全常量元组 `(谓词, 常量...)`（与 v1 同一数据契约）；来源 ID 是
  非空 `str` 或非 bool `int`。裸事实的缺省来源为 `DEFAULT_SOURCE =
  "default"`。
- **多来源语义**：同一事实可携带多个来源 ID；只有最后一个来源被删除时
  事实才消失。删除不存在的来源是**无操作**（文档化的幂等语义，不是静默
  纠错；非法类型仍然报错）。
- **原子性**：整批先校验（类型、元数、谓词上限），任何非法输入抛
  `ValidationError` 且状态零改动；批次使事实总数超 `max_facts` 时整批
  回滚，返回 `applied=False` 及原因——绝不留下未说明的部分变更。
- **批内顺序**：先净结算基事实来源（同批删了又加 = 无变化），再按分量
  拓扑序先删（DRed）后增（半朴素）。同批被删又被重新推出的事实只计入
  工作集，不计入净增/净减。
- **撤回原因**：`source_removed`（基事实最后来源被撤，`detail` 为本批
  移除的来源集合）、`support_lost`（派生事实失去全部推导）、
  `rules_replaced`（规则集替换后不在新最小模型中）。全部可用
  `datalog.checker.verify_retractions(rules, base_facts, retracted)` 独立
  验证——检查器从当前基事实从头重算，不采信求解器结论。
- **版本**：规则集从版本 0 开始，每次成功 `replace_rules` 加一。旧快照
  永久冻结：其事实、推导（旧版本规则序号）与来源不随后续批次或替换改变，
  旧证据不会引用新版本的来源。
- **替换失败语义**：新规则集的类型/上限/安全性/分层校验在发布前完成，
  失败抛原有异常类型（`ValidationError` / `UnsafeRuleError` /
  `LimitError` / `StratificationError`），旧版本继续生效；新模型超预算时
  返回 `applied=False`，旧版本保留。

### v2 输入上限（与 v1 不同，见"两版接口的差异"）

| 量 | v1 `evaluate` | v2 `Session` |
|---|---|---|
| 不同谓词数 | 20 | 100 |
| 规则数 | 100 | 500 |
| 基事实数（去重后） | 2000 | 50 000 |
| 事实总预算 `max_facts` | 参数，默认 100 000 | 参数，默认 100 000 |

## 证据检查器（独立，不采信求解器标志）

```python
from datalog import verify_snapshot, verify_evidence, verify_retractions, recompute_least_model

chk = verify_snapshot(snap)   # EvidenceReport: ok / violations / checked
```

检查器只读取快照的公开数据（事实集、基事实来源映射、每条派生事实的记录
推导、规则列表），自底向上重新验证：基事实必须带来源；记录推导的规则
序号、头谓词、正体子事实可重放合一；否定体字面量对应基事实不在当前事实
集中；从基事实出发沿推导能到达每条派生事实（纯循环"证据"无基事实支撑，
判为不成立）；事实总数不超预算。`recompute_least_model` 是检查器自带的
朴素不动点实现，与 `datalog.engine` / `datalog.incremental` 不共享代码。

## 语义与设计取舍

- **分量化维护**：谓词按正依赖图划分为强连通分量，按拓扑序处理。输入
  未变的分量完全不触发（`firings` 为零），无关规则不重算——测试用
  大无关子图 + 小循环修改验证。
- **正递归层 DRed**：删除时沿共享支持 DAG 做过度删除（凡是经记录推导
  可达被删事实的派生事实全部 tentative 撤回），再从剩余事实重新推导仍
  有支撑者。递归不靠支持计数自我支撑：纯循环在基支撑撤走后整体坍塌
  （有测试）。`over_deleted` / `rederived` 即该层的实际撤回/重推工作集。
- **否定层**：否定依赖变化的分量整体重算（分层否定非单调，增量 delta
  不安全），按依赖顺序进行；未受影响的否定分量同样零触发。
- **支持 DAG**：每条派生事实保存当前全部有效立即推导（规则序号 + 正体
  子事实），事实间共享子推导。快照中每条派生事实的公开推导仍按 v1 口径
  取最短（深度最小，同深度取字典序最小的 `(规则序号, 子事实)`），用
  Dijkstra 式一遍计算，支持图中的循环不会成为证据。
- **基事实与派生重叠**：同一事实既可被断言也可被推出。断言来源全部撤
  走后若仍有推导支撑，事实转为派生继续存在；反之派生事实被断言后成为
  基事实。
- **预算语义**：v1 超支返回 `complete=False` 的未完成结果；v2 会话必须
  始终处于完整最小模型，因此超支批次/替换整体拒绝并回滚。两者都是如实
  报告，不用部分结果冒充完整结果。
- **确定性**：同一输入序列必得同一结果；所有对外集合为 frozenset，报告
  中排序输出；触发计数与求值顺序确定。
- **浮点口径**：与 v1 相同——常量须为有限值（NaN/±Infinity 拒绝），
  `2 == 2.0` 视为同一常量；本模块不产生浮点计算结果，无误差区间问题；
  数值未决（非有限输入）在入口处即拒绝并定位。

## 两版接口的差异（逐条变更与迁移办法）

本次迭代**没有修改任何 v1 行为**：`evaluate` 的签名、限制、异常类型、
推导选取口径与确定性结果全部保留，旧测试原样通过（未删改任何旧测试或
断言）。差异仅在新增接口：

1. **上限不同**：v2 `Session` 的上限（100 谓词 / 500 规则 / 50 000 基
   事实）高于 v1（20 / 100 / 2000）。原因：本题要求至少 100 规则 /
   10 000 基事实 / 100 批次，与 v1 上限直接冲突；v1 上限是其公开契约的
   一部分且有测试锁定，故不为 v1 放宽，而为 v2 设定独立上限。
   **迁移**：无需迁移——旧调用行为不变；需要更大规模的调用改用
   `Session`。
2. **预算失败的表现不同**：v1 返回 `complete=False`；v2 拒绝整批并回滚
   （`applied=False`）。原因：会话是有状态的，"不完整的状态"无法合法
   存在。**迁移**：v1 调用方检查 `complete`；v2 调用方检查 `applied`。
3. **`__version__`** 从 `1.0.0` 升为 `2.0.0`；`datalog/__init__.py` 新增
   导出（见下），旧导出名称与语义不变。

兼容适配：若调用方只有 v1 用法，无需任何改动；若需把一次性求值迁移到
会话，`Session(rules, facts=facts).result()` 与 `evaluate(facts, rules)`
给出相同的事实集与相同的（最短、字典序最小）推导。

## 代码结构

```
datalog/
  __init__.py     公共导出（v1 + v2）
  errors.py       错误类型体系（未变）
  terms.py        Var / Literal / Rule / pos / neg 与项校验（未变）
  engine.py       v1：校验、分层、半朴素求值、推导记录、Result
  incremental.py  v2：Session、分量分析、DRed、批次/替换报告、快照
  checker.py      独立证据检查器 + 朴素从头重算参考
tests/
  test_engine.py       v1 测试（原有，未改动）
  test_incremental.py  v2 快速回归（含小域随机序列对照独立参考）
  test_scale.py        扩展验证（DATALOG_RUN_EXTENDED=1 才运行）
  naive_reference.py   测试共享的独立朴素参考与来源账本
demo.py                v1 演示（原有）
demo_incremental.py    v2 端到端演示（三组新能力 + 规模实例）
```

## 已知限制

- 仅集合语义：无包语义、无聚合、无算术、无析取头、无函数符号。
- 否定仅支持分层否定；非分层程序报 `StratificationError`。
- 否定依赖变化的分量整体重算（正确性优先）；只有纯正分量享受增量
  delta/DRed。分量粒度是谓词强连通分量，同一分量内的事实级局部性不再
  细分。
- 支持 DAG 保存全部有效立即推导，内存随推导数线性增长；超大规模稠密
  递归（如长链全闭包）需相应提高机器预算。
- 快照中每条派生事实只公开一条最优推导（与 v1 一致），不枚举全部证明；
  完整支持 DAG 是会话内部结构。
- 会话为单线程设计；快照不可变，可安全跨线程读取。
- 扩展验证（100 规则 / 10 000 基事实 / 100 批次 + 无关子图零重算）约
  2.5 分钟；快速回归约 5 秒；两个演示分别约 0.1 秒和 15 秒。
