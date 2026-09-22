"""datalog —— 带否定分层的规则查询求值库（仅标准库）。

公开接口：

* :func:`evaluate` —— 求程序的分层最小模型；
* :class:`Atom` / :class:`Rule` —— 输入结构；
* :class:`Result` / :class:`QueryResult` /
  :class:`FactProof` / :class:`Derivation` —— 输出结构；
* :class:`InvalidProgramError` / :class:`StratificationError` /
  :class:`EvaluationIncomplete` —— 错误类型；
* ``MAX_PREDICATES`` / ``MAX_RULES`` / ``MAX_FACTS`` /
  ``DEFAULT_FACT_BUDGET`` —— 接口边界常量。
"""

from .engine import evaluate
from .errors import (
    DatalogError,
    EvaluationIncomplete,
    InvalidProgramError,
    StratificationError,
)
from .model import (
    DEFAULT_FACT_BUDGET,
    MAX_FACT_BUDGET,
    MAX_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
    Atom,
    Derivation,
    FactProof,
    QueryResult,
    Result,
    Rule,
    derivation_sort_key,
)
from .provenance import all_facts_with_depth, format_chain, walk_chosen

__all__ = [
    "evaluate",
    "Atom",
    "Rule",
    "Result",
    "QueryResult",
    "FactProof",
    "Derivation",
    "derivation_sort_key",
    "DatalogError",
    "InvalidProgramError",
    "StratificationError",
    "EvaluationIncomplete",
    "format_chain",
    "walk_chosen",
    "all_facts_with_depth",
    "MAX_PREDICATES",
    "MAX_RULES",
    "MAX_FACTS",
    "DEFAULT_FACT_BUDGET",
    "MAX_FACT_BUDGET",
]

__version__ = "1.0.0"
