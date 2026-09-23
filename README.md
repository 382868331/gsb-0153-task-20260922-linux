# datalog — 带否定分层的规则查询求值模块（一次性求值 + 增量会话）

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）：

- **一次性求值**（v1.0 起，接口未变）：`evaluate(facts, rules)` 按层求最小模型，
  并为每条派生事实记录字典序最小的最短推导 DAG。
- **增量会话**（v1.1 新增）：`Session` 持续接收原子的事实增删批次，基事实支持
  多来源 ID；正递归层做删除后的失效传播与重新推导（DRed），否定层按依赖顺序
  必要重算；规则集可在一个批次内整体原子替换，旧版本快照继续可查。

- **Python 版本**：3.14.7（仅标准库，无任何第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始

```bash
python -m unittest discover -s tests -v            # 快速回归（约 1 秒）
python -m unittest discover -s tests_extended -v   # 扩展验证：100规则/1万事实/100批次（约 1 分钟）
python demo.py                                     # 演示（约 8 秒，上限 90 秒）
```

## 一次性接口（v1.0，行为未变）

```python
from datalog import evaluate, Rule, Var, pos, neg

X, Y, Z = Var("X"), Var("Y"), Var("Z")
facts = [("edge", "a", "b"), ("edge", "b", "c")]
rules = [
    Rule(("reach", X, Y), [pos("edge", X, Y)]),
    Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
    Rule(("dead", X),   [pos("node", X), neg("reach", "a", X)]),
]
res = evaluate(facts, rules, max_facts=100_000)
res.complete            # bool：是否算出了完整最小模型
res.reason              # 未完成时的原因（complete=True 时为 None）
res.facts / res.derived # frozenset：全部事实 / 仅派生事实
res.query("reach", "a", X)   # 按模式查询，返回排序后的事实列表
res.derivation(("reach", "a", "c"))  # Derivation(rule_index, children)
res.explain(("reach", "a", "c"))     # 打印来源链（推导 DAG 的文本缩进树）
```

## 增量接口（v1.1 新增）

```python
from datalog import Session, Rule, Var, pos, neg

s = Session(rules, facts=[
    (("edge", "a", "b"), "sensor-1"),   # (事实, 来源ID)；裸事实用默认来源 "default"
    (("edge", "a", "b"), "sensor-2"),   # 同一事实允许多个来源
])
rep = s.apply_batch(
    add=[(("edge", "b", "c"), "sensor-3")],
    remove=[(("edge", "a", "b"), "sensor-1")],   # sensor-2 仍在 → 什么都不变
)
rep.complete            # False 表示该批次被整体拒绝（预算），状态零变化
rep.added_facts / rep.removed_facts / rep.affected_facts   # 净变化 / 受影响事实集
rep.retraction_reasons  # {事实: 撤回原因}，可用 s.verify_retraction(f) 独立复核
rep.strata              # 每个受影响层的工作集：deleted/rederived/inserted/rules_fired
snap = s.snapshot()     # 不可变快照：query/derivation/explain/check_evidence/verify_all
s.replace_rules(new_rules)   # 原子替换规则集（先校验安全性和分层，再发布）
s.verify_retraction(fact)    # 独立从头重放，确认事实已不可推导
```

### 输入字段与边界含义（固定缺省）

- **批次条目**：`(事实元组, 来源ID)` 或裸事实元组（缺省来源 `DEFAULT_SOURCE =
  "default"`）。来源 ID 只接受 `str` 或 `int`（拒绝 `bool`）。
- **批内顺序**：同一批次先应用全部删除、再应用全部增加（固定缺省，确定性）。
- **多来源语义**：基事实存活当且仅当至少一个来源仍支持它；删除一个来源不会
  删除仍有其他来源支持的事实及其派生。
- **忽略的删除**：删除一个不存在的 `(事实, 来源)` 对是**无操作**，记入
  `report.ignored_removals`，不报错、不静默改变其他东西。
- **原子性**：批次中任何条目非法（类型、NaN、元数……）→ 整批抛错，状态零变化；
  预算超支 → 整批回滚，`complete=False` 并给出 `reason`，会话可继续后续批次。
- **规则替换**：`apply_batch(rules=...)` 或 `replace_rules(...)` 在一个批次内
  原子替换整个规则集；安全性与分层先检查，失败则旧规则继续生效。替换后全部
  派生状态在新规则下重建，`rule_version` 递增。旧快照持有旧版本，其证据不可能
  引用新版本之后才出现的来源（快照各自冻结来源表）。

### 输出状态

- `UpdateReport.complete`：`False` 仅表示预算耗尽导致的整批拒绝（`reason` 说明），
  与"推不出任何事实"（正常完成、空派生）严格区分。
- `StratumReport`（仅列出受影响层）：`deleted`（实际撤回）、`rederived`
  （过度删除后被重新推导恢复）、`inserted`（新派生）、`rules_fired`、
  `recomputed`（是否否定层整体重算）。未被波及的规则和子图不出现在任何工作集中。
- 撤回原因（均可由 `verify_retraction` 从头复核）：`("base-lost-source", 来源)`、
  `("support-retracted", 层号)`、`("negation-stratum-recomputed", 层号)`、
  `("rules-replaced", 旧版本号)`。

### 证据与撤回检查（不信任求解器标志）

- `Snapshot.check_evidence(fact)`：沿推导 DAG 从**当前仍存活的基事实**向上逐节点
  重匹配规则体、核对否定字面量缺席，叶子必须有活来源；返回 `(ok, 消息)`。
- `Snapshot.verify_all()`：对快照内全部派生事实做上述检查。
- `Session.verify_retraction(fact)`：用**独立的**从头朴素求值器（不与引擎/会话
  共享代码）重放当前规则与基事实，确认事实已不可推导。检查器不读取任何
  `complete` 之类的布尔标志作为结论依据。

## 数据契约（固定公开结构，v1.0 起未变）

- **变量**：`Var("名字")`；**常量**：`int`（拒绝 `bool`）、有限 `float`、`str`。
- **原子**：普通元组 `(谓词: str, 参数...)`；事实是全常量的原子。
- **字面量**：`pos(...)` / `neg(...)`；**规则**：`Rule(头原子, [字面量...])`。
- 谓词元数固定，全程序一致；无函数符号（原子不可嵌套）。
- 集合语义：重复事实/重复推导自动去重（这是语义而非静默纠错）。

## 输入上限（接口边界）

| 量 | `evaluate`（旧） | `Session`（新，均为显式可选参数） |
|---|---|---|
| 不同谓词数 | 20 | `max_predicates=20` |
| 规则数 | 100 | `max_rules=100` |
| 基事实数（去重后） | 2000 | `max_base_facts=20_000` |
| 事实总预算（基+派生） | `max_facts=100_000` | `max_facts=1_000_000` |

`evaluate` 的上限与行为完全未变；`Session` 的上限是独立的新显式选项
（`SESSION_MAX_*` 常量可查），满足"≥100 规则、≥10 000 基事实、≥100 批次"的规模要求。

## 错误语义（均可定位，不静默纠正）

- `ValidationError`：类型错误、NaN/±Infinity 常量、bool 常量、bool/负数预算、
  事实中含变量、元数不一致、非法来源 ID 等，消息含位置。
- `UnsafeRuleError`（`ValidationError` 子类）：规则头变量或否定体变量未出现在正体中。
- `LimitError`（`ValidationError` 子类）：超出上限；`Session` 初始构建超预算也抛此错。
- `StratificationError`：谓词依赖图中存在穿过负边的环，消息列出涉及谓词。

## 语义与设计取舍

- **分层否定**：按依赖图计算层号（正边同层、负边跨层），逐层求最小模型；含负边
  的环直接拒绝。不宣称支持 well-founded / answer-set 等更广义语义。
- **正递归层增量维护（DRed）**：每个规则实例化都记录为推导节点（完整支持记录，
  共享 DAG）。删除时先**过度删除**（沿 DAG 下游凡依赖被删事实者皆入候选），再
  **结构性重新推导**：候选事实仍有"子事实全部存活"的节点即恢复。纯递归环没有
  环外支撑时整体撤回——**递归不能靠支持计数自我支撑**。
- **否定层**：输入有变化才按层序整体重算（否定使删除可能引发插入，DRed 不适用）；
  输入未变的层完全跳过。
- **插入**：半朴素 delta 迭代，所有体谓词的 delta 都作为驱动位置（不限递归位置），
  非递归规则只在有 delta 时触发；连接用按参数索引加速（仅影响搜索顺序，不改变结果）。
- **推导选取**：快照中每条事实的最优推导 = 深度最小、再按 `(规则序号, 子事实)`
  字典序最小，与 `evaluate` 的决胜规则一致；推导深度在活跃节点 DAG 上用
  Dijkstra 式最小不动点计算。
- **预算口径**：`max_facts` 是事实总数预算；任一轮新事实会使总数超支时整批回滚。
  预算耗尽（`complete=False`）与"无解/空派生"（`complete=True`、空集）严格区分。
- **字典序比较**：混合类型常量按 `(类型秩, 值)` 比较（数值 < 字符串）。
- **浮点口径**：常量中的 `float` 必须有限；相等性遵循 Python 语义（`2 == 2.0`）。
  本模块不产生浮点计算结果，查询与推导均为精确匹配，无误差区间问题。
- **确定性**：同一构造与批次序列必得同一事实集、同一最优推导、同一报告；
  扩展测试中完整重放 100 个批次逐位核对。

## 兼容性与迁移

- **无破坏性变更**：v1.0 的全部公开入口（`evaluate`、`Result`、`Rule`、`Var`、
  `pos`/`neg`、错误类型、上限常量）行为不变，旧测试原样通过（未删改任何旧测试
  或断言）。旧调用无需任何迁移。
- 本次新增能力全部在**新入口**上（`Session`/`Snapshot`/`UpdateReport`，
  `SESSION_MAX_*` 上限），属于独立的新显式选项，不改变旧格式含义。
- `engine.py` 内部仅做了兼容式重构（私有校验函数增加带默认值的上限参数、
  抽出 explain 渲染辅助函数），公开行为逐条保持一致。

## 已知限制

- 仅集合语义：无包语义、无聚合、无算术、无析取头、无函数符号。
- 否定仅支持分层否定；非分层程序报 `StratificationError`。
- 每条派生事实只保留一条最优推导用于展示/证据；内部为增量维护记录全部推导节点，
  节点数随不同实例化数量增长（规模测试约 28 万节点），超大程序需注意内存。
- 否定层是整体重算而非增量；依赖它的上层会按变化传播。
- `verify_retraction` 是完整从头重放，适合中小域复核，不是为超大状态设计的热路径。
- 预算超支被拒的批次不留任何痕迹；调用方必须检查 `UpdateReport.complete`。

## 代码结构

```
datalog/
  __init__.py      公共导出（v1.1）
  errors.py        错误类型体系
  terms.py         Var / Literal / Rule / pos / neg 与项校验
  engine.py        一次性求值：校验、分层、半朴素求值、推导记录、Result（行为未变）
  incremental.py   增量会话：Session / Snapshot / UpdateReport、DRed、证据与撤回检查器
tests/
  test_engine.py       旧一次性引擎测试（未改动）
  test_incremental.py  快速回归：多来源、双路径、纯递归环、否定翻转、无关子图、
                       规则替换、原子性、确定性、随机小域逐批对照独立参考
tests_extended/
  test_scale.py    扩展验证：100 规则 / 10 000 基事实 / 100 批次，
                   每 25 批对照独立从头参考 + 全量证据核对 + 确定性重放
demo.py            端到端演示（三组新能力 + 棘手边界 + 实际触发的拒绝 + 主规模）
```

测试中的期望值来自各测试文件内**独立编写**的参考实现（`naive_evaluate` /
`reference_model`，与被测核心不共享任何代码）及手工计算的小实例；参考验证
不调用被测求解器得到期望值。
