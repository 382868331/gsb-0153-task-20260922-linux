"""Reusable stratified-Datalog rule-query evaluation module (offline, stdlib-only).

Public API (v1, unchanged):

    evaluate(facts, rules, *, max_facts=...) -> Result
    Var(name)                      -- logic variable
    Rule(head, body)               -- rule; head is an atom tuple
    pos(*atom) / neg(*atom)        -- body literals
    Result.query / .derivation / .explain / .complete / .facts / .derived

Public API (v2, incremental sessions — new entry points, same data model):

    Session(rules, facts=(), *, max_facts=...)   -- persistent program
    Session.apply_batch(adds=..., removes=...)   -> BatchReport
    Session.replace_rules(new_rules)             -> ReplaceReport
    Session.result()                             -> SessionResult
    verify_snapshot(snapshot) / verify_evidence(...) / verify_retractions(...)
    recompute_least_model(facts, rules)          -- independent reference

Errors: DatalogError, ValidationError, UnsafeRuleError, LimitError,
StratificationError.
"""

from .checker import (
    EvidenceReport,
    recompute_least_model,
    verify_evidence,
    verify_retractions,
    verify_snapshot,
)
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
    SESSION_MAX_BASE_FACTS,
    SESSION_MAX_PREDICATES,
    SESSION_MAX_RULES,
    BatchReport,
    ReplaceReport,
    Retraction,
    Session,
    SessionResult,
)
from .terms import Literal, Rule, Var, neg, pos

__all__ = [
    # v1 one-shot evaluation (unchanged)
    "evaluate",
    "Result",
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
    # v2 incremental sessions
    "Session",
    "SessionResult",
    "BatchReport",
    "ReplaceReport",
    "Retraction",
    "DEFAULT_SOURCE",
    "SESSION_MAX_PREDICATES",
    "SESSION_MAX_RULES",
    "SESSION_MAX_BASE_FACTS",
    # independent evidence checker / reference
    "EvidenceReport",
    "verify_evidence",
    "verify_snapshot",
    "verify_retractions",
    "recompute_least_model",
]

__version__ = "2.0.0"
