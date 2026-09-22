# datalog — 带否定分层的规则查询求值库

Python 3.14.7 标准库实现，**无需安装任何第三方依赖**，离线运行。
实现“带否定分层的 Datalog 规则查询求值”可复用模块：项只有常量与变量、
无函数符，谓词固定元数，仅集合语义；支持正递归（半朴素 delta 迭代）、
带否定的分层最小模型，并为每条派生事实记录**字典序最小的最短推导 DAG**。

## 运行要求与命令

* Python：**3.14.7**（仅用 3.10+ 的语法特性；以 3.14.7 实测）。
* 依赖：仅标准库，不需要网络、账号、密钥、数据库、中间件、云 API 或 Docker。
* 执行测试：

  ```bash
  python -m unittest discover -s tests -v
  ```

* 运行固定输入演示（现场真实计算，无 sleep、无预录结果，实测 < 0.1 秒）：

  ```bash
  python demo.py
  ```

终端命令仅用于启动测试与演示。所有数据由仓库内确定性代码/常量生成。

## 接口

### 输入结构

```python
from datalog import Atom, Rule, evaluate

facts = [("edge", ("a", "b")), Atom("edge", ["b", "c"])]   # ground 事实
rules = [
    Rule(Atom("reach", ["X", "Y"]), [Atom("edge", ["X", "Y"])]),
    Rule(Atom("reach", ["X", "Y"]),
         [Atom("edge", ["X", "Z"]), Atom("reach", ["Z", "Y"])]),
    # 否定文字放第三个参数 neg_body：可达但无直飞
    Rule(Atom("indirect", ["X", "Y"]),
         [Atom("reach", ["X", "Y"])],
         [Atom("edge", ["X", "Y"])]),
]
res = evaluate(facts, rules, fact_budget=1_000_000)
```

* **项**：变量为大写字母开头的 `str`（如 `"X"`）；常量为 `int`、有限 `float`
  或非大写开头的 `str`（如 `"alice"`、`"c0"`）。**无函数符**（不接受复合项）。
* **事实**：ground `Atom` 或 `(谓词名, (常量, ...))` 的列表，**集合语义**自动去重。
* **规则**：`Rule(head, body, neg_body=())`。安全性强制：
  头变量与否定体变量必须在**普通正体原子**（非内置项）中出现；
  空体规则只允许派生基事实。
* **内置比较**（安全过滤，不做产生式绑定）：

  ```python
  Atom("<", [4, "X"], builtin="<")            # 4 < X
  Atom("!=", ["X", "Y"], builtin="!=")        # X != Y
  Atom("=", ["X", 3], builtin="=")            # X = 3
  ```

  `=`/`!=` 两侧为变量或任意常量；`<` 两侧为变量或 int/float。
  内置项不进入依赖图、不可被规则定义、不能否定。

### 输出结构

* `res.status`：`"complete"` 或 `"incomplete"`；
* `res.facts`：`{谓词: frozenset[(常量, ...)]}`，含 EDB 与已提交各层 IDB；
* `res.proof`：`{(谓词, 行): FactProof}`；
  `FactProof.depth`（最短推导深度，EDB=0）、
  `.derivations`（全部最短推导）、`.chosen`（字典序最小者）；
  `Derivation.rule_index`（EDB 为 `None`）、`.parents`
  （正前提 `(谓词, 行, True)`；否定见证 `(谓词, 行, False)`）；
* `res.strata`：谓词 → 层号（0 起）；
* `res.budget` / `res.fact_count` / `res.stopped_at_stratum`；
* 查询与解释：

  ```python
  q = res.query(Atom("reach", ["X", "c"]))   # 含变量原子
  q.rows                                     # 按变量出现顺序的答案元组
  q.provenance(row)                          # 该答案行的 FactProof
  ("reach", ("a", "c")) in res               # 具体事实成员判定
  res.explain(("reach", ("a", "c")))         # FactProof
  ```

  另提供 `datalog.format_chain(res, "reach", ("a", "c"))` 把 `chosen`
  推导渲染成缩进树（demo 有实例）。

### 错误语义（固定契约）

| 情形 | 行为 |
|---|---|
| 谓词元数冲突、事实/规则结构非法、非安全规则、EDB/IDB 同谓词 | `InvalidProgramError`（`ValueError` 子类），信息含规则/参数定位 |
| 依赖图存在**含负边的环** | `StratificationError`（`ValueError` 子类），信息给出环路径 |
| EDB 事实数本身已超过 `fact_budget` | `InvalidProgramError`：完整模型不可能容纳，属可定位配置错误 |
| 推导过程中事实总数超 `fact_budget` | **不抛异常**：返回 `status="incomplete"` |
| 在 incomplete 结果上查询/判定未求完谓词 | `EvaluationIncomplete`（**未知**，不能冒充完整） |
| 完整模型（或已完整层）中查无此事实 | `KeyError`（**确证不存在**，即“证明无解”） |

**预算耗尽（未知）与证明无解（确证不存在）严格分开。**

### 数值与类型契约

* `float` 常量拒绝 `NaN` / `+∞` / `-∞`（`math.isfinite`）；
  int/float 常量限定在 int64 范围内；接受正常有限浮点（如 `1.5`）。
* 因 `bool` 是 `int` 子类，**整数参数明确拒绝 `bool`**（事实常量、
  `fact_budget` 等）；`negated` 等布尔参数同样不接受 int 伪装。
* 本库不做浮点聚合/近似比较：`<` 按 IEEE-754 双精度的严格全序执行，
  无容差、无 epsilon，边界用例不使用宽容差掩盖。**没有任何计算产生
  新的浮点值**（浮点只作常量参与过滤），因此不存在结果舍入误差口径；
  若未来引入算术内置项，需在 README 单独定义误差口径。

## 设计取舍

1. **分层**：在“体谓词→头谓词”依赖图上用 Tarjan SCC 找环；SCC 内含
   否定边即拒绝。凝聚 DAG 上按约束 `stratum(头) ≥ stratum(体)`（正边）、
   `stratum(头) ≥ stratum(体)+1`（负边）取最长路定级。
2. **半朴素 eval**：每个层内首轮全量触发，此后每个“正体普通原子位置”
   一个 delta 变体（该位置扫上一轮改进集、其余扫全集），避免重复全量推导。
3. **最短深度的 delta 包含“深度松弛”**：不同低层事实推导深度差异大，
   事实首次出现的轮次未必给最短深度。delta 因此有两类新信息：
   新事实、最短深度变小的事实——按超图上 Bellman-Ford 式松弛迭代至不动点。
4. **最短推导 DAG**：每个候选推导的深度为 `1 + max(正前提深度,
   否定见证深度)`；同一事实保留达到最小深度的**全部**推导（DAG），
   `chosen` 取规范化键上的字典序最小。等深度新推导不再向下传播
   （不改变成员身份与他者深度）。
5. **否定见证深度**：取被否定关系最终模型中的最大推导深度（空关系为 0），
   使有否定父节点的推导深度是良定义的有限值；该深度只影响深度编号，
   不影响成员判定（成员判定严格按闭世界缺席）。
6. **预算的原子性**：事实计数在每层开始前检查；超限时**整层不提交**
   （不留下未说明的部分层结果），返回已完整的低层前缀与
   `stopped_at_stratum`。`fact_budget` 是常驻事实总数（EDB+IDB），
   默认 1,000,000，合法范围 1..10,000,000。
7. **集合语义**：同一事实多重推导只存一份，多条最短推导在
   `FactProof.derivations` 中保留，成员关系无重复。
8. **独立参考**：`tests/reference_naive.py` 是独立的**朴素全量触发**
   不动点实现（分层用 Kosaraju，与核心的 Tarjan 不同算法），**不导入
   核心求值代码**（只复用题面固定的 `Atom`/`Rule` 输入结构），用于
   交叉校验最小模型；半朴素实现与朴素参考在 25 组随机图 + 12 组
   随机否定/递归组合实例上逐一比对。

## 输入上限（接口边界）

| 参数 | 上限 | 超限行为 |
|---|---:|---|
| 不同谓词数 | 20 | `InvalidProgramError` |
| 规则数 | 100 | `InvalidProgramError` |
| 输入事实数 | 2000 | `InvalidProgramError` |
| `fact_budget`（事实总数） | 默认 1,000,000；最大 10,000,000 | 推导中超限 → incomplete |

谓词元数没有单独写死上限（受事实/规则表达本身限制）。短演示不穷举上限规模。

## 文件结构

```
datalog/
  __init__.py     公开接口
  model.py        Atom / Rule / Result / FactProof / Derivation 与输入校验
  engine.py       分层、半朴素 delta 迭代、最短推导松弛、预算控制
  provenance.py   来源链遍历与格式化
  errors.py       错误类型
tests/
  test_inputs.py  输入契约与错误语义
  test_engine.py  递归/否定/重复推导/预算/查询 与朴素参考的随机交叉验证
  reference_naive.py  独立朴素不动点参考（不调用核心算法）
demo.py           固定输入演示
README.md
```

## 已知限制（按本题契约的有限子集，不宣称完整行业标准兼容）

* 仅实现**安全、可分层的纯 Datalog 子集**：无函数符、无条件算术产生式
  绑定（内置比较只作安全过滤）、无聚合、无存在量词、无析取体、无更新。
* 仅支持带**分层否定**（stratified negation）；含负边环的程序直接拒绝，
  不实现局部 stratified / well-founded / answer-set 语义。
* 内置比较仅 `=`、`!=`、`<`（后者限数值侧），不支持 `<=`、`>`、`>=`
  以及字符串序比较。
* 常量序中 `int` 与 `float` 按 Python `==`/`<` 语义；`1 == 1.0` 会被
  视为同一常量（Python 语义），这是已知的类型合并特性而非误差。
* 不实现外部数据接入、持久化、并发增量维护或解释计划优化；
  半朴素是标准做法但没有索引/选择度优化，极大关系的笛卡尔积体原子
  受预算与 Python 性能约束。
* 不实现零元谓词查询的特殊输出格式（零元关系若存在，其行是 `()`）。
