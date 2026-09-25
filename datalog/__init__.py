"""Reusable stratified-Datalog rule-query evaluation module (offline, stdlib-only).

Public API:

    evaluate(facts, rules, *, max_facts=..., groups=...) -> Result
    Var(name)                      -- logic variable
    Rule(head, body, aggregate=None) -- rule; head is an atom tuple
    Aggregate("count") / Aggregate("sum", Var) -- group aggregate on a rule
    pos(*atom) / neg(*atom)        -- body literals
    Result.query / .derivation / .explain / .fact_id
    Result.complete / .facts / .derived

Errors: DatalogError, ValidationError, UnsafeRuleError, LimitError,
StratificationError, AggregateStratificationError, AggregateError.
"""

from .engine import (
    DEFAULT_FACT_BUDGET,
    MAX_INPUT_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
    AggregateDerivation,
    Derivation,
    Result,
    evaluate,
)
from .errors import (
    AggregateError,
    AggregateStratificationError,
    DatalogError,
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
)
from .terms import Aggregate, Literal, Rule, Var, neg, pos

__all__ = [
    "evaluate",
    "Result",
    "Derivation",
    "AggregateDerivation",
    "Var",
    "Rule",
    "Aggregate",
    "Literal",
    "pos",
    "neg",
    "DatalogError",
    "ValidationError",
    "UnsafeRuleError",
    "LimitError",
    "StratificationError",
    "AggregateStratificationError",
    "AggregateError",
    "MAX_PREDICATES",
    "MAX_RULES",
    "MAX_INPUT_FACTS",
    "DEFAULT_FACT_BUDGET",
]

__version__ = "1.1.0"
