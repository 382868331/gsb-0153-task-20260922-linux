# datalog — 带否定分层的规则查询求值模块

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器：给定基事实与规则，按层求最小模型（least model），并为每条派生事实
记录字典序最小的最短推导 DAG（来源链）。

`evaluate_query` 在此之上做查询定向（按需）求值：调用方指定要查的关系与已知
条件后，只展开可能影响答案的规则与事实，递归规则与分层否定保持原语义，
答案与推导依据同完整求值一致，并通过 `QueryStats` 如实记录减少的无关推导量；
完整求值 `evaluate` 原样保留。

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
```

#### 查询定向求值：`evaluate_query`

完整求值 `evaluate` 保留原语义，总是计算整个程序的最小模型。当调用方只关心
某个关系在给定已知条件下的答案时，用 `evaluate_query` 只展开可能影响答案的
部分（magic-set 式的按需传播）：

```python
from datalog import evaluate_query
# 查询模式：常量=已知条件，Var=开放位置；可传单个模式、谓词名或模式列表
q = evaluate_query(facts, rules, ("reach", "a", X))
q.answer()     # 排序去重后的答案事实；与 evaluate 全量结果按模式筛选一致
q.stats        # QueryStats：用了多少输入事实/规则、派生出多少事实、
               #   pruned_derivations：连接已产生但因与查询无关而丢弃的候选推导
q.stats.summary()  # 形如 "input facts used 11/20 (skipped 9), rules used
               #   3/4 (skipped 1), derived 9 facts, pruned 9 irrelevant derivations"
q.derivation(fact)  # 与完整求值相同的最短字典序推导 DAG
```

- **需求传播**：查询模式中的常量形成初始需求（部分绑定），沿规则反向传播；
  没有任何需求到达的谓词规则不会被触发，不满足任何需求的输入事实不加载、
  连接产生的无关候选事实不计入模型。
- **否定与递归的保真性**：否定字面量与正字面量一样传播需求——决定否定成立
  与否的事实恰好匹配该否定字面量的需求，绝不会为了少算而漏掉；正递归仍用
  同一套半朴素 delta 迭代、同一分层与最短推导选择。对被物化事实，
  `evaluate_query` 的答案与推导依据与 `evaluate` 完整求值逐事实一致；
  `evaluate_query(facts, rules, [(p,)+(Var,...)])` 等价于求 p 的完整模型。
- **预算语义**：`max_facts` 只约束实际物化的事实（实际使用的输入事实 +
  保留的派生）；超支同样返回 `complete=False` 且整轮不应用，不冒充完整。

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
- **查询定向求值**：`evaluate_query` 把查询模式的常量绑定作为需求，沿规则
  体（含否定体）反向求需求不动点；无需求的谓词规则不触发、无需求的输入事实
  不加载、连接产出但不满足需求的候选事实丢弃并计入 `pruned_derivations`。
  变量只有在已被更靠近头的位置绑成常量时才能收缩需求，因此这是对真实所需
  事实的可靠超近似，不会漏答案；分层、半朴素迭代与最短推导选择完全复用
  完整求值路径，保证结果一致。

## 已知限制

- 仅集合语义：无包（bag）语义、无聚合、无算术、无析取头、无函数符号。
- 否定仅支持分层否定；非分层程序（如 `p :- not p`）报 `StratificationError`。
- 每条派生事实只记录一条最优推导 DAG，不保留备选证明。
- 预算超支时返回的是"已完成轮次"的事实（可能是仅输入事实），调用方必须
  检查 `complete` 后再使用结果。
- 随机对照测试覆盖小随机分层程序（完整求值 120 个种子，查询定向 150 个
  种子，后者逐事实对照完整求值的答案与推导）；未做上限规模的性能压测。
- 查询定向求值的需求是位置常量绑定，不做跨字面量的变量重排序/连接顺序
  优化；当查询完全开放（无任何常量条件）时，不会比完整求值少算。

## 代码结构

```
datalog/
  __init__.py   公共导出
  errors.py     错误类型体系
  terms.py      Var / Literal / Rule / pos / neg 与项校验
  engine.py     校验、分层、半朴素求值、推导记录、Result、
                查询定向需求传播与 evaluate_query/QueryStats
tests/
  test_engine.py  单元测试 + 独立朴素不动点参考 + 随机对照
  test_query.py   查询定向求值：减算统计、否定保真、与完整求值随机对照
demo.py         固定输入演示：正常结果 + 预算超支 + 分层拒绝
```

测试中的期望值来自测试文件内独立编写的朴素不动点参考实现
（`naive_evaluate`，与被测核心不共享任何代码）及手工计算的小实例；
引擎与参考在 120 个确定性随机分层程序上结果一致。
