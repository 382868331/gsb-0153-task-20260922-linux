# datalog — 带否定分层的规则查询求值模块

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器：给定基事实与规则，按层求最小模型（least model），并为每条派生事实
记录字典序最小的最短推导 DAG（来源链）。

- **Python 版本**：3.14.7（仅标准库，无需安装任何第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始

```bash
python -m unittest discover -s tests -v   # 运行测试（含独立朴素参考对照）
python demo.py                            # 运行演示（约 0.1 秒，远小于 8 秒上限）
```

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
res.stats                   # EvalStats(firings, derived, rounds)：推导工作量计数
```

### 按需求值（只展开与查询相关的部分）

调用方指定要查的关系和已知条件后，`evaluate_query` 先做**相关性剪枝**：
从查询谓词出发沿依赖图（正边**和负边**都跟随）取传递闭包，闭包外的规则
与输入事实整批剔除，再用与完整求值完全相同的分层半朴素算法求值。负边
必须跟随——决定否定是否成立的事实一个都不能少，因此递归规则与分层否定
的语义与完整求值**逐事实一致**（答案、推导 DAG、规则编号均相同）。

```python
from datalog import evaluate_query

res = evaluate_query(facts, rules, "reach", "a", X, measure_baseline=True)
res.answers                 # 已按模式过滤好的答案（== 完整求值的 query 结果）
res.focus                   # FocusStats：相关性报告
res.focus.relevant_predicates   # 实际参与展开的谓词
res.focus.pruned_rules          # 被剔除规则的原序号
res.focus.pruned_facts          # 被剔除的输入事实数
res.focus.derivations_avoided   # 实际减少的推导次数（规则触发数之差）
```

- `derivations_avoided` 需要一次完整求值作基线，故仅在
  `measure_baseline=True` 时填充（默认 `None`，聚焦求值本身不做无用功）。
- `max_facts` 预算作用于聚焦后的运行（相关输入事实 + 派生事实）。
- 结果 `res.facts` 是完整模型在相关谓词上的切片；查询无关谓词得到空集。
- 完整求值 `evaluate` 原样保留，两者可互换使用。

### 数据契约（固定公开结构）

- **变量**：`Var("名字")`；**常量**：`int`（拒绝 `bool`）、有限 `float`、`str`。
- **原子**：普通元组 `(谓词: str, 参数...)`；事实是全常量的原子。
- **字面量**：`pos(...)` / `neg(...)`；**规则**：`Rule(头原子, [字面量...])`。
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
  `max_facts`、事实中含变量、元数不一致等，消息含 `fact i` /
  `rule i, body literal j` 等位置。
- `UnsafeRuleError`（`ValidationError` 子类）：规则头变量或否定体变量
  未出现在正体中（非安全规则）。
- `LimitError`（`ValidationError` 子类）：超出上表前三项上限。
- `StratificationError`：谓词依赖图中存在穿过负边的环，消息列出涉及谓词。

### 预算耗尽 ≠ 无解

`max_facts` 是**事实总数预算**。若下一轮半朴素迭代产生的事实会使总数超支，
该轮**整体不应用**（不留未说明的部分变更），返回 `complete=False` 且
`reason` 说明原因的结果——绝不会用部分答案冒充完整结果。反之，规则推不出
任何新事实（"无解"/空派生）是正常完成：`complete=True`、结果为空集。两者
严格区分。

## 语义与设计取舍

- **分层否定**：按依赖图计算层号（正边同层、负边跨层），逐层求最小模型；
  含负边的环直接拒绝。这是分层 Datalog 的标准语义，不宣称支持
  well-founded / answer-set 等更广义语义。
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
- **按需求值**：剪枝在谓词依赖图层面进行（不做常量级 magic-set 变换），
  因此永远可靠：相关闭包内的求值与完整求值使用同一份核心代码
  （`_evaluate_core`），规则保持原始编号，推导记录逐条一致。
  `evaluate_query` 的 `pattern` 常量只用于过滤最终答案，不参与剪枝。

## 已知限制

- 仅集合语义：无包（bag）语义、无聚合、无算术、无析取头、无函数符号。
- 否定仅支持分层否定；非分层程序（如 `p :- not p`）报 `StratificationError`。
- 每条派生事实只记录一条最优推导 DAG，不保留备选证明。
- 预算超支时返回的是"已完成轮次"的事实（可能是仅输入事实），调用方必须
  检查 `complete` 后再使用结果。
- `evaluate_query` 只对查询谓词的依赖闭包分层；若不可分层部分与查询
  无关，聚焦查询可以成功而完整求值会报 `StratificationError`（这是
  按需求值的自然性质，不是静默纠错）。
- 随机对照测试覆盖小随机分层程序（120 个种子）；未做上限规模的性能压测。

## 代码结构

```
datalog/
  __init__.py   公共导出
  errors.py     错误类型体系
  terms.py      Var / Literal / Rule / pos / neg 与项校验
  engine.py     校验、分层、半朴素求值、推导记录、Result、按需求值
tests/
  test_engine.py  单元测试 + 独立朴素不动点参考 + 随机对照
  test_query.py   按需求值：与完整求值的一致性、剪枝与减少量记录
demo.py         固定输入演示：正常结果 + 预算超支 + 分层拒绝 + 按需求值
```

测试中的期望值来自测试文件内独立编写的朴素不动点参考实现
（`naive_evaluate`，与被测核心不共享任何代码）及手工计算的小实例；
引擎与参考在 120 个确定性随机分层程序上结果一致；按需求值在同一批
随机程序上逐谓词与完整求值对照（答案、推导记录、相关切片完全一致，
且规则触发数不多于完整求值）。
