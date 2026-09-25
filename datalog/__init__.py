"""Reusable stratified-Datalog rule-query evaluation module (offline, stdlib-only).

Public API:

    evaluate(facts, rules, *, max_facts=...) -> Result
    Var(name)                      -- logic variable
    Rule(head, body)               -- rule; head is an atom tuple
    pos(*atom) / neg(*atom)        -- body literals
    agg_count(*atom, group=, into) -- trailing count aggregate
    agg_sum(*atom, group=, value=, into) -- trailing sum aggregate
    Result.query / .derivation / .explain / .complete / .facts / .derived

Errors: DatalogError, ValidationError, UnsafeRuleError, LimitError,
StratificationError.
"""

from .engine import (
    DEFAULT_FACT_BUDGET,
    MAX_INPUT_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
    AggDerivation,
    Derivation,
    Result,
    evaluate,
)
from .errors import (
    DatalogError,
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
)
from .terms import (
    Aggregate,
    Literal,
    Rule,
    Var,
    agg_count,
    agg_sum,
    neg,
    pos,
)

__all__ = [
    "evaluate",
    "Result",
    "Derivation",
    "AggDerivation",
    "Var",
    "Rule",
    "Literal",
    "Aggregate",
    "pos",
    "neg",
    "agg_count",
    "agg_sum",
    "DatalogError",
    "ValidationError",
    "UnsafeRuleError",
    "LimitError",
    "StratificationError",
    "MAX_PREDICATES",
    "MAX_RULES",
    "MAX_INPUT_FACTS",
    "DEFAULT_FACT_BUDGET",
]

__version__ = "1.1.0"
