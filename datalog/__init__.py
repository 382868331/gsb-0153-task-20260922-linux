"""Reusable stratified-Datalog rule-query evaluation module (offline, stdlib-only).

Public API:

    evaluate(facts, rules, *, max_facts=...) -> Result
    evaluate_query(facts, rules, query, *, max_facts=...) -> QueryResult
    Var(name)                      -- logic variable
    Rule(head, body)               -- rule; head is an atom tuple
    pos(*atom) / neg(*atom)        -- body literals
    Result.query / .derivation / .explain / .complete / .facts / .derived

Errors: DatalogError, ValidationError, UnsafeRuleError, LimitError,
StratificationError.
"""

from .engine import (
    DEFAULT_FACT_BUDGET,
    MAX_INPUT_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
    Derivation,
    QueryResult,
    QueryStats,
    Result,
    evaluate,
    evaluate_query,
)
from .errors import (
    DatalogError,
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
)
from .terms import Literal, Rule, Var, neg, pos

__all__ = [
    "evaluate",
    "evaluate_query",
    "Result",
    "QueryResult",
    "QueryStats",
    "Derivation",
    "Var",
    "Rule",
    "Literal",
    "pos",
    "neg",
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

__version__ = "1.0.0"
