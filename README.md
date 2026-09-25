# datalog — 带否定分层与分组聚合的规则查询求值模块

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器：给定基事实与规则，按层求最小模型（least model），并为每条派生事实
记录字典序最小的最短推导 DAG（来源链）。规则体最后可接一个分组聚合
（`count` / `sum`），聚合是分层边：被聚合谓词必须属于严格更低的层。

- **Python 版本**：3.14.7（仅标准库，无需安装任何第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始（可复现路径）

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 全部测试（含独立朴素参考对照）
./.venv/bin/python demo.py                            # 演示（实测约 0.05 秒，上限 90 秒）
```

演示现场计算并打印：正常递归+否定结果与来源链、预算超支的未完成结果、
负边环拒绝、分组 count/sum（含负整数求和、空组取零、聚合来源链）、
聚合递归环拒绝。失败示例见下文「聚合的失败示例」。

## 接口

```python
from datalog import evaluate, Rule, Var, pos, neg

X, Y, Z = Var("X"), Var("Y"), Var("Z")
facts = [("edge", "a", "b"), ("edge", "b", "c")]        # 基事实：元组 (谓词, 常量...)
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

### 分组聚合（count / sum）

规则体**最后**可以接一个聚合字面量（每条规则至多一个）：

```python
from datalog import agg_count, agg_sum
D, E, A, C, S = Var("D"), Var("E"), Var("A"), Var("C"), Var("S")
rules = [
    # 每个部门的人数：按 D 分组，对 member(D, E) 的去重事实集计数
    Rule(("headcount", D, C),
         [pos("dept", D), agg_count("member", D, E, group=(D,), into=C)]),
    # 每个部门的账务合计：对 txn(D, A) 中 A 的整数值求和（允许负整数）
    Rule(("balance", D, S),
         [pos("dept", D), agg_sum("txn", D, A, group=(D,), value=A, into=S)]),
]
res = evaluate(facts, rules)
d = res.derivation(("balance", "eng", 100))  # AggDerivation
d.op, d.group, d.value        # ('sum', ('eng',), 100)
d.contributors                # 贡献事实（去重后的稳定 ID，按规范序排序）
```

语义要点：

- **分层边**：被聚合谓词必须在严格更低的层（与负边同权）。因此聚合的
  输入是**已完成的**有限事实集，任何穿过聚合边的递归环（直接或间接）
  都在求值前以 `StratificationError` 拒绝；同层普通正递归不受影响。
- **显式分组域**：分组键（`group=` 的变量）必须由规则普通正体绑定——
  这些绑定就是调用者给出的显式分组域。域内的键即使没有贡献事实也
  产生 0（count/sum 均为 0）；域外的键不产生任何事实（连 0 也没有）。
- **集合语义**：计数/求和前先对匹配事实去重，同一事实的多条推导只算
  一次。
- **sum 值必须是整数**：贡献事实中被求和的值不是 `int` 时抛
  `ValidationError`（消息含规则序号与事实）。负整数合法。
- **来源记录**：聚合事实的推导是 `AggDerivation(rule_index, children,
  op, group, value, contributors)`——保存普通正体子事实、分组键、聚合
  值与全部贡献事实（事实元组即其稳定 ID，集合去重后唯一）。
- **向上组合**：聚合结果是普通派生事实，可被更高层的普通规则与否定
  字面量引用。
- **预算**：聚合结果同样计入 `max_facts`；若所在轮超支则整轮不应用，
  绝不发布算了一半的总数（聚合输入在进层前已完成，每个总数要么完整
  算出、要么不出现）。

### 聚合的失败示例（均可复现）

```python
from datalog import evaluate, Rule, Var, pos, agg_count, agg_sum
from datalog import StratificationError, UnsafeRuleError, ValidationError
X, Y, C, A, S = Var("X"), Var("Y"), Var("C"), Var("A"), Var("S")

# 1) 穿过聚合边的递归环 -> StratificationError
evaluate([("e", 1)],
         [Rule(("p", X, C), [pos("e", X), agg_count("p", X, Y, group=(X,), into=C)])])

# 2) 分组键未在普通正体绑定 -> UnsafeRuleError
evaluate([("e", 1), ("f", 2)],
         [Rule(("p", X, C), [pos("e", X), agg_count("f", Y, group=(Y,), into=C)])])

# 3) sum 遇到非整数值 -> ValidationError（求值期、可定位）
evaluate([("acct", "a"), ("txn", "a", "oops")],
         [Rule(("bal", X, S), [pos("acct", X), agg_sum("txn", X, A, group=(X,), value=A, into=S)])])
```

### 数据契约（固定公开结构）

- **变量**：`Var("名字")`；**常量**：`int`（拒绝 `bool`）、有限 `float`、`str`。
- **原子**：普通元组 `(谓词: str, 参数...)`；事实是全常量的原子。
- **字面量**：`pos(...)` / `neg(...)`；**规则**：`Rule(头原子, [字面量...])`，
  规则体最后可接一个 `agg_count(...)` / `agg_sum(...)` 聚合（至多一个）。
- 谓词元数固定，全程序一致；无函数符号（原子不可嵌套）。
- 集合语义：重复事实/重复推导自动去重（这是语义而非静默纠错）。

### 输入上限（接口边界）

| 量 | 上限 |
|---|---|
| 不同谓词数 | 20 |
| 规则数 | 100 |
| 输入事实数（去重后） | 2000 |
| 事实总预算 `max_facts`（输入+派生） | 参数，默认 100 000 |

### 错误语义（均可定位，不静默纠正）

- `ValidationError`：类型错误、NaN/±Infinity 常量、bool 常量、bool/负数
  `max_facts`、事实中含变量、元数不一致、聚合结构非法（未知 op、空分组、
  聚合不在体末、count 带 value、sum 的 value 不在聚合原子中）、以及求值期
  sum 遇到非整数值等，消息含 `fact i` / `rule i, body literal j` 等位置。
- `UnsafeRuleError`（`ValidationError` 子类）：规则头变量或否定体变量
  未出现在正体中；聚合分组键未在普通正体绑定；聚合结果变量或 sum 值
  变量与普通体变量冲突。
- `LimitError`（`ValidationError` 子类）：超出上表前三项上限。
- `StratificationError`：谓词依赖图中存在穿过负边**或聚合边**的环，
  消息列出涉及谓词。

### 预算耗尽 ≠ 无解

`max_facts` 是**事实总数预算**。若下一轮半朴素迭代产生的事实会使总数超支，
该轮**整体不应用**（不留未说明的部分变更），返回 `complete=False` 且
`reason` 说明原因的结果——绝不会用部分答案冒充完整结果。反之，规则推不出
任何新事实（"无解"/空派生）是正常完成：`complete=True`、结果为空集。两者
严格区分。

## 语义与设计取舍

- **分层否定与聚合**：按依赖图计算层号（正边同层、负边与聚合边跨层），
  逐层求最小模型；含负边或聚合边的环直接拒绝。这是分层 Datalog 的标准
  语义，不宣称支持 well-founded / answer-set 等更广义语义。
- **正递归**：层内用半朴素（semi-naive）delta 迭代，每轮只用上一轮的新增
  事实驱动，避免重复推导；非递归规则只在首轮触发。
- **推导 DAG**：每条派生事实记录一条**最短**推导（规则应用深度最小；半朴素
  的首个派生轮即最小深度），同深度时取字典序最小的
  `(规则序号, 正体子事实按体序)`。子事实指规则正体匹配到的事实；否定字面量
  是缺席检查，不计入子节点。各事实的推导共享子推导，整体构成 DAG。
  每条事实只保留这一条最优推导，不枚举全部证明。
- **字典序比较**：混合类型常量按 `(类型秩, 值)` 比较（数值 < 字符串），
  避免 Python 3 中 `int` 与 `str` 不可比的问题。
- **浮点口径**：常量中的 `float` 必须是有限值；相等性遵循 Python 语义，
  即 `2 == 2.0` 视为同一常量（哈希一致）。本模块不产生浮点计算结果，
  因此无浮点误差区间问题；查询与推导均为精确匹配。
- **连接求值**：正体按书写顺序回溯连接，未做连接顺序优化/索引——上限规模
  （20 谓词 / 100 规则 / 2000 事实）下足够，换取实现的简单与可审计。

## 已知限制

- 仅集合语义：无包（bag）语义、无算术比较、无析取头、无函数符号。
- 聚合仅 `count` / `sum`：每条规则至多一个且必须在体末；分组键非空；
  `sum` 只接受 `int` 值；不支持全局（无分组键）聚合、不支持 `avg`/`min`/
  `max`、聚合结果不能再参与同层聚合（聚合边强制跨层）。
- 否定仅支持分层否定；非分层程序（如 `p :- not p`）报 `StratificationError`。
- 每条派生事实只记录一条最优推导 DAG，不保留备选证明。
- 预算超支时返回的是"已完成轮次"的事实（可能是仅输入事实），调用方必须
  检查 `complete` 后再使用结果。
- 随机对照测试覆盖小随机分层程序（120 个种子）；聚合以确定性小实例加
  普通集合/逐组求和参考核对；未做上限规模的性能压测。

## 代码结构

```
datalog/
  __init__.py   公共导出
  errors.py     错误类型体系
  terms.py      Var / Literal / Aggregate / Rule 与 pos / neg / agg_count / agg_sum、项校验
  engine.py     校验、分层、半朴素求值、聚合求值、推导记录、Result
tests/
  test_engine.py       单元测试 + 独立朴素不动点参考 + 随机对照
  test_aggregation.py  聚合测试：去重计数、空组取零、负整数求和、聚合后否定、
                       非法聚合递归、预算不发布部分总数（参考为普通集合与逐组求和）
demo.py         固定输入演示：正常结果 + 预算超支 + 分层拒绝 + 分组聚合 + 聚合环拒绝
```

测试中的期望值来自测试文件内独立编写的朴素不动点参考实现
（`naive_evaluate`，与被测核心不共享任何代码）及手工计算的小实例；
引擎与参考在 120 个确定性随机分层程序上结果一致。
