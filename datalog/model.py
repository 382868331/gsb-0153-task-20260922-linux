"""公开数据结构：项 (Term)、原子 (Atom)、规则 (Rule)、结果对象。

输入契约（违反时抛 ``InvalidProgramError``，信息中带定位）：

* 谓词名：非空 ``str``，不以 ``#`` 开头；
* 常量项：允许 ``int``（拒绝 ``bool``，因为 ``bool`` 是 ``int`` 子类）、
  ``str``、``float``（拒绝 NaN / +-Infinity），数值取值受限在 int64 范围内；
* 变量项：``str``，首字符必须大写；常量串不得大写开头；
* 无函数符：不接受元组/列表作为复合项；
* 谓词固定元数：同名谓词在所有事实与规则中参数个数必须一致；
* 规则安全：规则头变量与否定体变量必须在普通正体原子中出现。

内置项 ``Atom(name, args, builtin=...)`` 支持三种比较：``"="``、``"!="``、
``"<"``（变量须在普通正体中已出现，故只作过滤，不做产生式绑定）：

* ``"="`` / ``"!="``：两侧为变量或任意常量，按 Python ``==`` 语义过滤；
* ``"<"``：两侧为变量或 int/float 常量；变量在运行时绑定到非数值
  （int/float，bool 已在输入侧拒绝）时该内置项为假。

内置项不进入谓词依赖图，不可被规则定义、不能被否定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import EvaluationIncomplete, InvalidProgramError

# 接口规模边界（题面给定）。
MAX_PREDICATES = 20
MAX_RULES = 100
MAX_FACTS = 2000

# 默认/最大事实总预算（EDB + 各层 IDB 合计的常驻事实数）。
DEFAULT_FACT_BUDGET = 1_000_000
MAX_FACT_BUDGET = 10_000_000

_BUILTINS = frozenset({"=", "!=", "<"})
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)


def is_variable(term: Any) -> bool:
    """``term`` 是否为变量（大写开头的字符串）。"""
    return isinstance(term, str) and bool(term) and term[0].isupper()


def _check_scalar(value: Any, where: str) -> None:
    """校验单个常量值；``where`` 用于错误定位。"""
    if isinstance(value, bool):
        # bool 是 int 子类，按契约整数参数拒绝 bool，必须先判。
        raise InvalidProgramError(f"{where}: bool 不是合法常量（整数参数拒绝 bool）")
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise InvalidProgramError(f"{where}: 整数 {value!r} 超出 int64 范围")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidProgramError(f"{where}: 浮点常量拒绝 NaN/Infinity")
        if not (_INT64_MIN <= value <= _INT64_MAX):
            raise InvalidProgramError(f"{where}: 浮点 {value!r} 超出允许范围")
        return
    if isinstance(value, str):
        if not value:
            raise InvalidProgramError(f"{where}: 常量串不能为空（空串无法与变量区分）")
        if value[0].isupper():
            raise InvalidProgramError(
                f"{where}: 常量串 {value!r} 以大写字母开头，会与变量混淆；"
                "请改写为小写/数字开头"
            )
        if value[0] == "#":
            raise InvalidProgramError(f"{where}: 常量串不得以 '#' 开头: {value!r}")
        return
    raise InvalidProgramError(
        f"{where}: 不支持的常量类型 {type(value).__name__}（仅 int/float/str，无函数符）"
    )


def _check_predicate_name(name: Any, where: str) -> None:
    if not isinstance(name, str) or not name:
        raise InvalidProgramError(f"{where}: 谓词名必须是非空 str，得到 {name!r}")
    if name[0] == "#":
        raise InvalidProgramError(f"{where}: 谓词名 {name!r} 以 '#' 开头（保留）")


def _check_args(args: Any, where: str) -> tuple[Any, ...]:
    if not isinstance(args, (list, tuple)):
        raise InvalidProgramError(
            f"{where}: 参数序列必须是 list/tuple，得到 {type(args).__name__}"
        )
    checked: list[Any] = []
    for i, term in enumerate(args):
        sub = f"{where} 的第 {i + 1} 个参数"
        if is_variable(term):
            checked.append(term)
        else:
            _check_scalar(term, sub)
            checked.append(term)
    return tuple(checked)


@dataclass(frozen=True, slots=True)
class Atom:
    """一个原子：``p(a, X, 3)`` 或内置比较（``builtin`` 非 None）。

    ``negated`` 仅对普通原子有意义，且普通规则中不直接使用——
    否定文字通过 :class:`Rule` 的 ``neg_body`` 表达。
    """

    name: str
    args: tuple[Any, ...]
    negated: bool = False
    builtin: str | None = None

    def __init__(self, name: str, args: list[Any] | tuple[Any, ...],
                 negated: bool = False, builtin: str | None = None):
        where = f"原子 {name!r}"
        _check_predicate_name(name, where)
        if not isinstance(negated, bool):
            raise InvalidProgramError(f"{where}: negated 必须是 bool（拒绝 int 伪装）")
        if builtin is not None and builtin not in _BUILTINS:
            raise InvalidProgramError(
                f"{where}: 未知内置比较 {builtin!r}（允许 {sorted(_BUILTINS)}）"
            )
        checked_args = _check_args(args, where)
        if builtin is not None:
            if negated:
                raise InvalidProgramError(f"{where}: 内置比较不得取反（请改用互补比较）")
            if len(checked_args) != 2:
                raise InvalidProgramError(f"{where}: 内置比较必须恰有 2 个参数")
        if builtin == "<":
            for i, t in enumerate(checked_args):
                if is_variable(t):
                    continue
                if not isinstance(t, (int, float)):
                    raise InvalidProgramError(
                        f"{where}: '<' 两侧参数必须是变量或 int/float 常量，"
                        f"第 {i + 1} 个为 {t!r}"
                    )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "args", checked_args)
        object.__setattr__(self, "negated", negated)
        object.__setattr__(self, "builtin", builtin)

    @property
    def is_builtin(self) -> bool:
        return self.builtin is not None

    @property
    def variables(self) -> frozenset[str]:
        return frozenset(t for t in self.args if is_variable(t))

    def format(self) -> str:
        if self.is_builtin:
            a, b = self.args
            return f"{repr_term(a)} {self.builtin} {repr_term(b)}"
        return ("!" if self.negated else "") + \
            f"{self.name}({', '.join(repr_term(t) for t in self.args)})"


def repr_term(term: Any) -> str:
    return term if is_variable(term) else repr(term)


@dataclass(frozen=True, slots=True)
class Rule:
    """一条规则：``head :- body..., !neg...``。

    * ``body``：普通正原子与内置比较原子（均不得带 ``negated``）；
    * ``neg_body``：被否定的普通原子（其 ``negated`` 标志必须为 False，
      “位于 neg_body”本身即表达否定，避免双重标记）；
    * 空体规则表示无条件派生（其头因安全检查而必须为基原子）。
    """

    head: Atom
    body: tuple[Atom, ...]
    neg_body: tuple[Atom, ...]

    def __init__(self, head: Atom, body: list[Atom] | tuple[Atom, ...] = (),
                 neg_body: list[Atom] | tuple[Atom, ...] = ()):
        if not isinstance(head, Atom) or head.negated or head.is_builtin:
            raise InvalidProgramError("规则头必须是普通的、非否定的原子")
        body_t = tuple(body)
        neg_t = tuple(neg_body)
        for label, seq in (("正体", body_t), ("否定体", neg_t)):
            for j, atom in enumerate(seq):
                if not isinstance(atom, Atom):
                    raise InvalidProgramError(
                        f"规则 {head.format()} 的{label}第 {j + 1} 项不是 Atom"
                    )
                if atom.negated:
                    raise InvalidProgramError(
                        f"规则 {head.format()} 的{label}中不得出现 negated=True 的原子 "
                        f"{atom.format()}（否定只由 neg_body 表达）"
                    )
                if atom.is_builtin and label == "否定体":
                    raise InvalidProgramError(
                        f"规则 {head.format()}: 内置比较不得出现在否定体: {atom.format()}"
                    )
        pos_vars: set[str] = set()
        for atom in body_t:
            if not atom.is_builtin:
                pos_vars |= atom.variables
        head_txt = head.format()
        for v in sorted(head.variables):
            if v not in pos_vars:
                raise InvalidProgramError(
                    f"规则 {head_txt} :- ... 非安全：头变量 {v} "
                    "未在任何正普通体原子中出现"
                )
        for atom in neg_t:
            for v in sorted(atom.variables):
                if v not in pos_vars:
                    raise InvalidProgramError(
                        f"规则 {head_txt} :- ... 非安全：否定体 {atom.format()} 的变量 "
                        f"{v} 未在任何正普通体原子中出现"
                    )
        for atom in body_t:
            if atom.is_builtin:
                for v in sorted(atom.variables):
                    if v not in pos_vars:
                        raise InvalidProgramError(
                            f"规则 {head_txt} :- ... 非安全：内置项 {atom.format()} 的变量 "
                            f"{v} 未在任何正普通体原子中出现"
                        )
        object.__setattr__(self, "head", head)
        object.__setattr__(self, "body", body_t)
        object.__setattr__(self, "neg_body", neg_t)

    def format(self) -> str:
        parts = [a.format() for a in self.body]
        parts += ["!" + a.format() for a in self.neg_body]
        return f"{self.head.format()} :- {', '.join(parts) if parts else 'true'}"


# ---------------------------------------------------------------- 推导 DAG ----

# 来源标签：EDB 事实的 rule_index 为 None。
EDB = "edb"

# 父节点键：(谓词名, 参数元组, 成立标志)。
#   正体前提：标志 True，事实在模型中存在；
#   否定体前提：标志 False，该基原子在被依赖层的模型中不存在（闭世界见证）。
ParentKey = tuple[str, tuple[Any, ...], bool]


@dataclass(frozen=True, slots=True)
class Derivation:
    """一条事实的一个直接推导步骤。

    ``rule_index`` 为 None 表示 EDB 事实（``parents`` 为空）；
    否则指向入参 ``rules`` 中的规则下标，``parents`` 按
    ``规则.body 中普通正原子 + 规则.neg_body`` 的顺序排列
    （内置比较不产生父节点，只作过滤）。
    """

    rule_index: int | None
    parents: tuple[ParentKey, ...]


@dataclass(frozen=True, slots=True)
class FactProof:
    """事实的最短推导 DAG 信息。

    * ``depth``：最短推导深度（EDB=0；规则推导 = 1 + max(正前提深度,
      否定见证深度)，否定见证深度取被否定关系模型中的最大推导深度）；
    * ``derivations``：达到该最短深度的全部推导；
    * ``chosen``：其中字典序最小者（按规则下标、父节点序列的规范化键比较）。
    """

    depth: int
    derivations: tuple[Derivation, ...]
    chosen: Derivation


class Result:
    """``evaluate`` 的返回值（集合语义）。

    属性：
      * ``status``：``"complete"`` 或 ``"incomplete"``；
      * ``facts``：``predicate -> frozenset[参数元组]``，含 EDB 与已求层 IDB；
      * ``proof``：``(谓词, 参数元组) -> FactProof``；
      * ``strata``：谓词 -> 层号（从 0 起）；
      * ``edb_predicates``：输入事实提供的谓词；
      * ``budget`` / ``fact_count``：事实总预算与当前常驻事实总数；
      * ``stopped_at_stratum``：incomplete 时第一个未能整轮求完的层号。

    incomplete 时，对未求完层中的 IDB 谓词调用 ``__contains__`` / ``query``
    会抛 :class:`EvaluationIncomplete`；EDB 谓词与更低层谓词仍可正常查询。
    """

    __slots__ = ("status", "facts", "proof", "strata", "edb_predicates",
                 "budget", "fact_count", "stopped_at_stratum", "rules",
                 "witness_depth", "arities")

    def __init__(self, status, facts, proof, strata, edb_predicates, budget,
                 fact_count, stopped_at_stratum, rules, witness_depth, arities):
        self.status = status
        self.facts = facts
        self.proof = proof
        self.strata = strata
        self.edb_predicates = edb_predicates
        self.budget = budget
        self.fact_count = fact_count
        self.stopped_at_stratum = stopped_at_stratum
        self.rules = rules
        self.witness_depth = witness_depth
        self.arities = arities

    def _accessible(self, predicate: str) -> bool:
        if self.status == "complete":
            return True
        if predicate in self.edb_predicates:
            return True
        return self.strata.get(predicate, 0) < self.stopped_at_stratum

    def _check_predicate(self, name: str, n_args: int, where: str):
        if name not in self.arities:
            raise InvalidProgramError(
                f"{where}: 谓词 {name!r} 未在任何事实或规则中声明"
            )
        expect = self.arities[name]
        if n_args != expect:
            raise InvalidProgramError(
                f"{where}: 谓词 {name!r} 固定元数为 {expect}，给出 {n_args} 个参数"
            )

    def __contains__(self, atom) -> bool:
        """具体事实是否在模型中；未求完层的 IDB 抛 EvaluationIncomplete。"""
        name, args = _atom_key(atom)
        self._check_predicate(name, len(args), "成员判定")
        if not self._accessible(name):
            raise EvaluationIncomplete(
                f"事实 {name}({', '.join(repr(a) for a in args)}) 无法判定："
                f"求值在第 {self.stopped_at_stratum} 层因预算耗尽中止"
            )
        return args in self.facts.get(name, frozenset())

    def query(self, atom: "Atom") -> "QueryResult":
        """对原子求值（可含变量）；目标谓词所在层未求完时抛 EvaluationIncomplete。"""
        if not isinstance(atom, Atom) or atom.negated or atom.is_builtin:
            raise InvalidProgramError("query 只接受普通的非否定原子")
        self._check_predicate(atom.name, len(atom.args), f"查询 {atom.format()}")
        if not self._accessible(atom.name):
            raise EvaluationIncomplete(
                f"查询 {atom.format()} 位于第 {self.strata.get(atom.name, 0)} 层，"
                f"该层未求完（止于第 {self.stopped_at_stratum} 层）：预算耗尽，不能冒充完整"
            )
        rows = _match(atom, self.facts.get(atom.name, frozenset()))
        return QueryResult(atom, rows, self)

    def explain(self, atom) -> FactProof:
        """返回具体事实的 :class:`FactProof`；不存在且可判定时抛 KeyError。"""
        name, args = _atom_key(atom)
        self._check_predicate(name, len(args), "explain")
        p = self.proof.get((name, args))
        if p is not None:
            return p
        if args in self.facts.get(name, frozenset()):
            raise InvalidProgramError(f"内部错误：{(name, args)} 在模型中但无推导记录")
        if not self._accessible(name):
            raise EvaluationIncomplete(
                f"事实 {(name, args)} 所在层未求完（止于第 "
                f"{self.stopped_at_stratum} 层），无法判定"
            )
        raise KeyError(f"模型中确证不存在：{name}({', '.join(repr(a) for a in args)})")


@dataclass(frozen=True, slots=True)
class QueryResult:
    atom: Atom
    rows: tuple[tuple[Any, ...], ...]
    result: Result

    @property
    def variables(self) -> tuple[str, ...]:
        """不同变量（按首次出现顺序）；与答案行列一一对应。"""
        seen: set[str] = set()
        out: list[str] = []
        for t in self.atom.args:
            if is_variable(t) and t not in seen:
                seen.add(t)
                out.append(t)
        return tuple(out)

    @property
    def complete(self) -> bool:
        return self.result._accessible(self.atom.name)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def provenance(self, row: tuple[Any, ...]) -> FactProof:
        """取某一答案行的推导。"""
        if len(row) != len(self.variables):
            raise InvalidProgramError(
                f"答案行 {row!r} 元数 {len(row)} 与查询变量数 {len(self.variables)} 不符"
            )
        subst = dict(zip(self.variables, row, strict=True))
        args = tuple(subst[t] if is_variable(t) else t for t in self.atom.args)
        return self.result.explain((self.atom.name, args))


def _atom_key(atom) -> tuple[str, tuple[Any, ...]]:
    if isinstance(atom, Atom):
        if atom.is_builtin:
            raise InvalidProgramError("内置比较不是可查询谓词")
        if any(is_variable(t) for t in atom.args):
            raise InvalidProgramError(f"此处要求具体事实，但 {atom.format()} 含变量")
        return atom.name, atom.args
    if (isinstance(atom, tuple) and len(atom) == 2 and isinstance(atom[0], str)
            and isinstance(atom[1], (list, tuple))):
        args = tuple(atom[1])
        for t in args:
            _check_scalar(t, f"事实 {atom[0]}")
        return atom[0], args
    raise InvalidProgramError("需要 Atom 或 (谓词名, (常量, ...))")


def _match(atom: Atom, rows: frozenset[tuple]) -> tuple[tuple, ...]:
    n = len(atom.args)
    # 每个不同变量取其首次出现位置；重复变量额外要求各位置相等。
    first_pos: dict[str, int] = {}
    equal_groups: list[tuple[int, int]] = []
    for i, v in enumerate(atom.args):
        if is_variable(v):
            if v in first_pos:
                equal_groups.append((first_pos[v], i))
            else:
                first_pos[v] = i
    const_pos = [(i, t) for i, t in enumerate(atom.args) if not is_variable(t)]
    out_pos = list(first_pos.values())
    out: set[tuple] = set()
    for row in rows:
        if len(row) != n:
            continue
        if any(row[i] != c for i, c in const_pos):
            continue
        if any(row[a] != row[b] for a, b in equal_groups):
            continue
        out.add(tuple(row[i] for i in out_pos))
    return tuple(sorted(out, key=_row_sort_key))


def _row_sort_key(row: tuple):
    # 混合类型常量的确定性全序键：(类型序, 数值或串)。
    return tuple((_const_type_order(v),
                  v if isinstance(v, (int, float)) else str(v)) for v in row)


def _const_type_order(v) -> int:
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return 1
    if isinstance(v, float):
        return 2
    return 3


def derivation_sort_key(deriv: Derivation):
    """:class:`Derivation` 的字典序全序键（供“字典序最小最短推导”使用）。"""
    return (deriv.rule_index if deriv.rule_index is not None else -1,
            tuple((p[0], tuple((_const_type_order(c),
                                c if isinstance(c, (int, float)) else str(c))
                               for c in p[1]), p[2]) for p in deriv.parents))
