# datalog — 带否定分层的规则查询求值模块

离线、纯 Python 标准库实现的分层 Datalog（stratified Datalog with negation）
求值器：给定基事实与规则，按层求最小模型（least model），并为每条派生事实
记录字典序最小的最短推导 DAG（来源链）。

- **Python 版本**：3.14.7（仅标准库，无需安装任何第三方依赖）
- **运行环境**：Linux 原生，完全离线；无网络、账号、数据库、Docker 需求

## 快速开始

```bash
python3.14 -m venv .venv                  # 准备虚拟环境（可选，标准库即可运行）
source .venv/bin/activate
python -m unittest discover -s tests -v   # 运行全部测试（含独立参考对照）
python demo.py                            # 分层引擎演示（约 0.1 秒）
python demo_iteration.py                  # 良基语义演示：逐轮上下界（约 0.1 秒）
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

## 独立入口：良基语义（否定环）

`evaluate_ground(facts, rules, *, max_rounds=128)` 是独立于分层引擎的
第二入口，面向**已经落地的有限规则**（规则头、体全部无变量；无函数项、
无聚合、不做变量展开）。它不拒绝穿过负边的依赖环，而是用**交替不动点**
（alternating fixpoint）按**良基语义**（well-founded semantics）把原子全集
划分为三组：

```python
from datalog import evaluate_ground, Rule, neg

rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]
res = evaluate_ground([], rules)
res.complete            # True：在轮次预算内收敛
res.true / res.false / res.undefined   # 三组原子，划分原子全集
res.status(("a",))       # "undefined"
res.rounds              # 每轮 (lower, upper) 上下界快照，便于检查收敛
```

- **原子全集**：输入事实、规则头、规则体出现过的全部原子（落地，故有限），
  上限 **64** 个（超出报 `LimitError`）。
- **交替不动点**：`A_0 = {}`，`A_{k+1} = T(A_k)`，其中 `T(J)` 是把
  `not a` 读作 "a 不在 J 中" 后的最小闭包。偶数项构成下界（确定为真）
  递增链，奇数项构成上界（可能为真）递减链；`true` = 最终下界，
  `false` = 全集减最终上界，`undefined` = 上界减下界。**找不到正向证明
  的原子不会直接当假**——只有同时跌出上界才是 false。
- **预算耗尽单独返回**：`max_rounds` 耗尽时 `complete=False`、`reason`
  说明原因，`true/false/undefined` 均为 `None`——绝不把预算耗尽伪装成
  undefined 的正式答案；`rounds` 轨迹仍可检查。
- 变量出现在事实或规则中会以带位置信息的 `ValidationError` 拒绝
  （本入口不做变量展开）。

```bash
python demo_iteration.py   # 否定环 undefined 示例、加事实消除不确定、预算边界
```


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

## 已知限制

- 仅集合语义：无包（bag）语义、无聚合、无算术、无析取头、无函数符号。
- 否定仅支持分层否定；非分层程序（如 `p :- not p`）报 `StratificationError`。
  需要否定环时改用独立入口 `evaluate_ground`（良基语义，仅落地规则，
  原子全集上限 64，无变量展开/函数项/聚合）。
- 每条派生事实只记录一条最优推导 DAG，不保留备选证明。
- 预算超支时返回的是"已完成轮次"的事实（可能是仅输入事实），调用方必须
  检查 `complete` 后再使用结果。
- 随机对照测试覆盖小随机分层程序（120 个种子）；未做上限规模的性能压测。

## 代码结构

```
datalog/
  __init__.py   公共导出
  errors.py     错误类型体系
  terms.py      Var / Literal / Rule / pos / neg 与项校验
  engine.py     校验、分层、半朴素求值、推导记录、Result
  wellfounded.py 独立入口：落地规则的良基语义（交替不动点）、WFResult
tests/
  test_engine.py      分层引擎单元测试 + 独立朴素不动点参考 + 随机对照
  test_wellfounded.py 良基入口测试：枚举候选上下界的独立参考、不动点定义核对、
                      否定环/加事实/预算耗尽/异常路径
demo.py         固定输入演示：正常结果 + 预算超支 + 分层拒绝
demo_iteration.py 良基语义演示：逐轮上下界、否定环、加事实、预算边界
```

测试中的期望值来自测试文件内独立编写的朴素不动点参考实现
（`naive_evaluate`，与被测核心不共享任何代码）及手工计算的小实例；
引擎与参考在 120 个确定性随机分层程序上结果一致。
良基入口的期望值来自测试文件内独立编写的**枚举参考**：对小原子全集枚举
所有候选下界、按定义核对 `T(T(S)) == S` 取最小不动点，与 `evaluate_ground`
在 60 个确定性随机落地程序上结果一致。
