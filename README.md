# datalog — 带否定分层与分组聚合的规则查询求值模块

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器：给定基事实与规则，按层求最小模型（least model），并为每条派生事实
记录字典序最小的最短推导 DAG（来源链）。规则体末尾可接一个**分组聚合**
（`count` / `sum`），聚合是严格的分层边：只能消费已完成的低层谓词，任何
经过聚合的递归环都会被拒绝。

- **Python 版本**：3.14.7（仅标准库，无需安装任何第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 运行测试（含独立朴素参考对照）
./.venv/bin/python demo.py                            # 运行演示（约 0.1 秒，上限 90 秒）
```

## 接口

```python
from datalog import evaluate, Rule, Aggregate, Var, pos, neg

X, Y, Z, V, N = Var("X"), Var("Y"), Var("Z"), Var("V"), Var("N")
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
res.fact_id(("reach", "a", "c"))     # 该事实的稳定整数 ID（相同输入重跑不变）
```

### 分组聚合

规则构造时传入 `aggregate=Aggregate(...)` 即在规则体末尾接一个分组聚合；
头的**最后一列**是聚合值占位变量，其余头参数是分组键：

```python
facts = [("sale", "east", 30), ("sale", "east", -5), ("sale", "west", 8)]
rules = [
    # net(地区, 净额) :- 对每组 sale 的整数金额求和（允许负整数）
    Rule(("net", X, N),  [pos("sale", X, V)], Aggregate("sum", V)),
    # rows(地区, 行数) :- 对每组去重后的贡献事实计数
    Rule(("rows", X, N), [pos("sale", X, V)], Aggregate("count")),
    # 聚合结果可被更高层普通规则引用（正向或否定）
    Rule(("hit", X),     [pos("net", X, 25)]),
]
res = evaluate(facts, rules, groups={0: [("south",)]})
res.query("net", X, N)    # [('net', 'east', 25), ('net', 'south', 0), ('net', 'west', 8)]
```

- **分组键**：头中除最后一列外的所有参数，必须是出现在正体中的变量
  （已绑定）；不允许常量或其他未绑定变量。
- **count**：每组贡献**去重后的连接行**数。同一事实即使由多条不同推导
  产生也只算一次（集合语义）。
- **sum**：对每组去重后连接行中值变量的**整数**值求和（负整数合法）。
  值不是整数（如 `1.5`、`"x"`）时抛 `AggregateError`。
- **空组**：只有调用者通过 `groups={规则序号: [组键元组, ...]}` 显式给出的
  组域中、且没有任何贡献行的组，才产生 0（`explicit=True`）；未列出的空组
  不产生任何事实。
- **分层**：聚合规则的所有体边都是严格跨层边（与负边同级）。聚合结果可被
  更高层的普通规则（含否定）引用；任何穿过聚合边的依赖环抛
  `AggregateStratificationError`（`StratificationError` 子类）。同层普通
  正递归不受影响。
- **来源记录**：`res.derivation(聚合事实)` 返回
  `AggregateDerivation(rule_index, contributors, contributor_ids, value,
  explicit)`——`contributors` 是每组去重后的贡献连接行（每行为按体序的
  贡献事实元组），`contributor_ids` 是这些事实的稳定 ID（与
  `res.fact_id(...)` 一致），`value` 是发布的聚合值。`res.explain(...)`
  会把贡献事实链一并缩进打印。
- **预算原子性**：一个层内所有聚合事实先算完再**整体提交**；若整批超出
  `max_facts`，一条都不发布（绝不发布算了一半的总数），返回
  `complete=False` 的结果。

### 数据契约（固定公开结构）

- **变量**：`Var("名字")`；**常量**：`int`（拒绝 `bool`）、有限 `float`、`str`。
- **原子**：普通元组 `(谓词: str, 参数...)`；事实是全常量的原子。
- **字面量**：`pos(...)` / `neg(...)`；**规则**：`Rule(头原子, [字面量...], aggregate=None)`。
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
  `max_facts`、事实中含变量、元数不一致、聚合形状非法（如 `Aggregate("avg")`、
  空体聚合规则）、显式组域非法（非基组键/元数不符/指向非聚合规则）等，
  消息含 `fact i` / `rule i, body literal j` / `groups[...]` 等位置。
- `UnsafeRuleError`（`ValidationError` 子类）：规则头变量或否定体变量
  未出现在正体中；聚合规则的分组键未绑定、sum 值变量未绑定、结果列不是
  全新变量。
- `LimitError`（`ValidationError` 子类）：超出上表前三项上限。
- `StratificationError`：谓词依赖图中存在穿过负边的环，消息列出涉及谓词。
- `AggregateStratificationError`（`StratificationError` 子类）：依赖环穿过
  聚合边（聚合只能消费严格低层），消息列出涉及谓词。
- `AggregateError`：聚合求值失败（如 sum 的值不是整数），消息含规则序号与组键。

### 预算耗尽 ≠ 无解

`max_facts` 是**事实总数预算**。若下一轮半朴素迭代或下一批聚合提交会使
总数超支，该轮/该批**整体不应用**（不留未说明的部分变更），返回
`complete=False` 且 `reason` 说明原因的结果——绝不会用部分答案（或算了一
半的聚合总数）冒充完整结果。反之，规则推不出任何新事实（"无解"/空派生）
是正常完成：`complete=True`、结果为空集。两者严格区分。

## 可复现路径与失败示例

```bash
# 1) 正常路径：聚合 + 来源链（现场计算，约 0.1 秒）
./.venv/bin/python demo.py
#    第 2 节打印 net/rows 分组结果、显式空组 net('south', 0)、
#    以及 net('east', 37) 的贡献事实链（含稳定 ID）。

# 2) 失败示例一：预算不足，整批聚合不发布
./.venv/bin/python -c "
from datalog import *
X, V, N = Var('X'), Var('V'), Var('N')
res = evaluate([('sale','a',1),('sale','b',2),('sale','c',3)],
               [Rule(('total', X, N), [pos('sale', X, V)], Aggregate('sum', V))],
               max_facts=5)
print(res.complete, res.reason)   # False ...budget...；total 一条都没有
"

# 3) 失败示例二：穿过聚合边的递归环被拒绝
./.venv/bin/python -c "
from datalog import *
X, V, N = Var('X'), Var('V'), Var('N')
try:
    evaluate([('s','a',1)],
             [Rule(('t', X, N), [pos('s', X, V)], Aggregate('sum', V)),
              Rule(('s', X, V), [pos('t', X, V)])])
except AggregateStratificationError as e:
    print('rejected:', e)
"

# 4) 失败示例三：sum 的值不是整数
./.venv/bin/python -c "
from datalog import *
X, V, N = Var('X'), Var('V'), Var('N')
try:
    evaluate([('txn','a',1.5)],
             [Rule(('t', X, N), [pos('txn', X, V)], Aggregate('sum', V))])
except AggregateError as e:
    print('rejected:', e)
"
```

## 语义与设计取舍

- **分层否定**：按依赖图计算层号（正边同层、负边跨层），逐层求最小模型；
  含负边的环直接拒绝。这是分层 Datalog 的标准语义，不宣称支持
  well-founded / answer-set 等更广义语义。
- **聚合即分层边**：聚合规则的所有体边按严格跨层处理（与负边同级），
  因此聚合总是作用在**已完成的**低层最小模型上，语义确定、无需半朴素
  增量维护；代价是不支持任何经过聚合的递归（包括间接环），这类程序
  直接拒绝。聚合在层内一次性点火、整批原子提交，然后才运行同层普通规则。
- **正递归**：层内用半朴素（semi-naive）delta 迭代，每轮只用上一轮的新增
  事实驱动，避免重复推导；非递归规则只在首轮触发。
- **聚合去重口径**：聚合的输入是"去重后的连接行集合"——每行是正体各
  字面量匹配到的事实元组。同一事实无论被多少条推导推出，在行集合里只
  出现一次；但不同的连接行（如多字面体中不同的匹配组合）各自计数。
  sum 对每行的值变量求和，值必须是 `int`（`bool` 拒绝）。
- **推导 DAG**：每条派生事实记录一条**最短**推导（规则应用深度最小；半朴素
  的首个派生轮即最小深度），同深度时取字典序最小的
  `(规则序号, 正体子事实按体序)`。子事实指规则正体匹配到的事实；否定字面量
  是缺席检查，不计入子节点。聚合事实的推导记录**全部**去重贡献行（不止
  一条），并附贡献事实的稳定 ID 与聚合值。各事实的推导共享子推导，整体
  构成 DAG。每条普通事实只保留一条最优推导，不枚举全部证明。
- **稳定事实 ID**：输入事实按排序后的键依次编号，派生事实按提交顺序编号；
  相同输入重跑编号完全一致，可跨运行引用（`Result.fact_id`）。
- **字典序比较**：混合类型常量按 `(类型秩, 值)` 比较（数值 < 字符串），
  避免 Python 3 中 `int` 与 `str` 不可比的问题。
- **浮点口径**：常量中的 `float` 必须是有限值；相等性遵循 Python 语义，
  即 `2 == 2.0` 视为同一常量（哈希一致）。聚合 sum 只接受整数，count
  产出整数，因此本模块仍不产生浮点计算结果，无浮点误差区间问题。
- **连接求值**：正体按书写顺序回溯连接，未做连接顺序优化/索引——上限规模
  （20 谓词 / 100 规则 / 2000 事实）下足够，换取实现的简单与可审计。

## 已知限制

- 仅集合语义：无包（bag）语义、无算术表达式、无析取头、无函数符号。
- 聚合仅 `count` / `sum`；sum 只接受整数值；无 min/max/avg。
- 聚合不可递归：任何穿过聚合边的依赖环（直接或间接）都被拒绝；
  否定仅支持分层否定；非分层程序（如 `p :- not p`）报 `StratificationError`。
- 空组只来自显式组域 `groups=`；不做基于常量类型的域推断。
- 每条普通派生事实只记录一条最优推导 DAG，不保留备选证明。
- 预算超支时返回的是"已完成批次"的事实（可能是仅输入事实），调用方必须
  检查 `complete` 后再使用结果。
- 随机对照测试覆盖小随机分层程序（120 个种子，无聚合）；聚合由手工小实例
  与测试内独立朴素参考对照覆盖；未做上限规模的性能压测。

## 代码结构

```
datalog/
  __init__.py   公共导出
  errors.py     错误类型体系（含 AggregateError / AggregateStratificationError）
  terms.py      Var / Literal / Rule / Aggregate / pos / neg 与项校验
  engine.py     校验、分层（负边+聚合边）、半朴素求值、聚合提交、推导记录、Result
tests/
  test_engine.py     原有单元测试 + 独立朴素不动点参考 + 随机对照
  test_aggregate.py  聚合测试 + 独立朴素集合/逐组求和参考
demo.py         固定输入演示：正常结果 + 聚合 + 预算超支 + 聚合递归拒绝
```

测试中的期望值来自测试文件内独立编写的朴素参考实现（`naive_evaluate` /
`naive_aggregate`，与被测核心不共享任何代码）及手工计算的小实例；引擎与
朴素参考在 120 个确定性随机分层程序（无聚合）及全部聚合小实例上结果一致。
