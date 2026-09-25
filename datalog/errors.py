"""Error taxonomy for the stratified Datalog engine.

All errors raised by the public API derive from ``DatalogError`` so callers
can catch one base type.  Every message carries a locatable position (fact
index, rule index, body-literal index, or predicate name) — invalid input is
never silently corrected.
"""


class DatalogError(Exception):
    """Base class for all errors raised by this package."""


class ValidationError(DatalogError):
    """Malformed input: bad types, non-finite numbers, arity mismatch, etc."""


class UnsafeRuleError(ValidationError):
    """A head variable or negated-body variable is not bound in a positive
    body literal (rule-safety violation)."""


class LimitError(ValidationError):
    """A declared interface limit (predicates / rules / facts) was exceeded."""


class StratificationError(DatalogError):
    """The predicate dependency graph contains a cycle through a negative
    edge, so no stratification (and hence no least-model semantics under
    this engine) exists."""


class AggregateStratificationError(StratificationError):
    """A dependency cycle passes through an aggregate edge (an aggregate
    may only consume strictly lower strata, never its own output)."""


class AggregateError(DatalogError):
    """An aggregate could not be evaluated over the contributed facts
    (for example a ``sum`` value that is not an integer)."""
