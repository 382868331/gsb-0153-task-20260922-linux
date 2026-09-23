"""Reusable stratified-Datalog rule-query evaluation module (offline, stdlib-only).

Public API (one-shot, unchanged since v1.0):

    evaluate(facts, rules, *, max_facts=...) -> Result
    Var(name)                      -- logic variable
    Rule(head, body)               -- rule; head is an atom tuple
    pos(*atom) / neg(*atom)        -- body literals
    Result.query / .derivation / .explain / .complete / .facts / .derived

Public API (incremental, added in v1.1):

    Session(rules, facts, *, max_facts=...)  -- stateful incremental session
    session.apply_batch(add=..., remove=..., rules=...) -> UpdateReport
    session.replace_rules(rules) -> UpdateReport
    session.snapshot() -> Snapshot   (immutable, versioned, evidence-checkable)
    session.verify_retraction(fact)  (independent from-scratch check)
    UpdateReport: affected_facts / retraction_reasons / strata work sets

Errors: DatalogError, ValidationError, UnsafeRuleError, LimitError,
StratificationError.
"""

from .engine import (
    DEFAULT_FACT_BUDGET,
    MAX_INPUT_FACTS,
    MAX_PREDICATES,
    MAX_RULES,
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
from .incremental import (
    DEFAULT_SOURCE,
    SESSION_DEFAULT_FACT_BUDGET,
    SESSION_MAX_BASE_FACTS,
    SESSION_MAX_PREDICATES,
    SESSION_MAX_RULES,
    Session,
    Snapshot,
    StratumReport,
    UpdateReport,
)
from .terms import Literal, Rule, Var, neg, pos

__all__ = [
    "evaluate",
    "Result",
    "Derivation",
    "Var",
    "Rule",
    "Literal",
    "pos",
    "neg",
    "Session",
    "Snapshot",
    "UpdateReport",
    "StratumReport",
    "DEFAULT_SOURCE",
    "DatalogError",
    "ValidationError",
    "UnsafeRuleError",
    "LimitError",
    "StratificationError",
    "MAX_PREDICATES",
    "MAX_RULES",
    "MAX_INPUT_FACTS",
    "DEFAULT_FACT_BUDGET",
    "SESSION_MAX_PREDICATES",
    "SESSION_MAX_RULES",
    "SESSION_MAX_BASE_FACTS",
    "SESSION_DEFAULT_FACT_BUDGET",
]

__version__ = "1.1.0"
