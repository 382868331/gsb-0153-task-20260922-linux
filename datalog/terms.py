"""Public data structures: variables, literals, rules, and term validation.

Representation contract (fixed, public):

* A **variable** is a ``Var`` instance.
* A **constant** is an ``int`` (but never ``bool``), a finite ``float``,
  or a ``str``.
* An **atom** is a plain tuple ``(predicate, arg1, ..., argN)`` where
  ``predicate`` is a non-empty ``str`` and each arg is a term (constant or
  variable).  Facts are atoms whose args are all constants.
* A **literal** is a ``Literal`` wrapping an atom plus a polarity flag;
  build them with the ``pos(...)`` / ``neg(...)`` helpers.
* A **rule** is ``Rule(head_atom, [literal, ...], aggregate=None)``; the
  optional :class:`Aggregate` turns the rule into a group aggregate
  (``count`` / ``sum``) evaluated as a stratum boundary.

There are no function symbols: nesting atoms inside atoms is rejected.
"""

from __future__ import annotations

import math

from .errors import ValidationError


class Var:
    """A logic variable identified by a non-empty string name."""

    __slots__ = ("name",)

    def __init__(self, name):
        if isinstance(name, bool) or not isinstance(name, str) or not name:
            raise ValidationError(
                f"variable name must be a non-empty str, got {name!r}"
            )
        self.name = name

    def __repr__(self):
        return f"Var({self.name!r})"

    def __eq__(self, other):
        return isinstance(other, Var) and self.name == other.name

    def __hash__(self):
        return hash(("Var", self.name))


class Literal:
    """An atom with a polarity.  ``positive=True`` is a plain body atom,
    ``positive=False`` is a negated (``not``) body atom."""

    __slots__ = ("atom", "positive")

    def __init__(self, atom, positive=True):
        self.atom = tuple(atom)
        self.positive = positive

    def __repr__(self):
        sign = "" if self.positive else "not "
        return f"Literal({sign}{self.atom!r})"


def pos(*atom):
    """Build a positive literal: ``pos("edge", Var("X"), Var("Y"))``."""
    return Literal(atom, True)


def neg(*atom):
    """Build a negated literal: ``neg("reach", Var("X"), Var("Y"))``."""
    return Literal(atom, False)


class Aggregate:
    """A group aggregate appended to a rule body: ``Aggregate(func, value)``.

    ``func`` is ``"count"`` or ``"sum"``.  For ``count`` the ``value`` is
    ignored (conventionally ``None``) and the head's last column is the
    number of distinct fact tuples contributed per group.  For ``sum`` the
    ``value`` must be a :class:`Var` bound in a positive body literal; the
    head's last column is the sum of the distinct bound integer values per
    group (negative integers allowed).

    The group key is every other head argument; each must be a variable
    bound in a positive body literal.  Empty groups are only produced for
    keys the caller lists in ``evaluate(..., groups=...)``.
    """

    __slots__ = ("func", "value")

    def __init__(self, func, value=None):
        if func not in ("count", "sum"):
            raise ValidationError(
                f"aggregate function must be 'count' or 'sum', got {func!r}"
            )
        if func == "sum" and not isinstance(value, Var):
            raise ValidationError(
                f"sum aggregate value must be a Var, got {value!r}"
            )
        self.func = func
        self.value = value

    def __repr__(self):
        if self.func == "count":
            return "Aggregate('count')"
        return f"Aggregate('sum', {self.value!r})"

    def __eq__(self, other):
        return (
            isinstance(other, Aggregate)
            and self.func == other.func
            and self.value == other.value
        )

    def __hash__(self):
        return hash(("Aggregate", self.func, self.value))


class Rule:
    """``Rule(head, body, aggregate=None)`` with ``head`` an atom tuple,
    ``body`` an iterable of ``Literal`` objects (possibly empty), and an
    optional :class:`Aggregate`."""

    __slots__ = ("head", "body", "aggregate")

    def __init__(self, head, body=(), aggregate=None):
        self.head = tuple(head)
        self.body = tuple(body)
        self.aggregate = aggregate

    def __repr__(self):
        if self.body:
            body = ", ".join(
                ("" if lit.positive else "not ") + repr(lit.atom) for lit in self.body
            )
        if self.aggregate is None:
            if self.body:
                return f"Rule({self.head!r} :- {body})"
            return f"Rule({self.head!r})"
        if self.body:
            return f"Rule({self.head!r} :- {body} >> {self.aggregate!r})"
        return f"Rule({self.head!r} :- {self.aggregate!r})"


def check_predicate(pred, where):
    """Validate a predicate name; raise a located ValidationError."""
    if isinstance(pred, bool) or not isinstance(pred, str) or not pred:
        raise ValidationError(
            f"{where}: predicate must be a non-empty str, got {pred!r}"
        )


def check_constant(value, where):
    """Validate a constant term; raise a located ValidationError.

    Rejects bool (even though it subclasses int), NaN and +/-Infinity,
    and any type other than int/float/str.
    """
    if isinstance(value, bool):
        raise ValidationError(
            f"{where}: bool is not a valid constant (got {value!r})"
        )
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(
                f"{where}: non-finite float constant {value!r} (NaN/Infinity rejected)"
            )
        return
    if isinstance(value, str):
        return
    raise ValidationError(
        f"{where}: constant must be int, finite float, or str; got "
        f"{type(value).__name__} {value!r}"
    )


def check_term(value, where):
    """Validate a rule term (constant or Var)."""
    if isinstance(value, Var):
        return
    check_constant(value, where)


def atom_vars(atom):
    """Yield the Var objects occurring in an atom's arguments."""
    return {a for a in atom[1:] if isinstance(a, Var)}
