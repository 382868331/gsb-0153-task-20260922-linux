"""Well-founded evaluation of grounded finite rules via alternating fixpoint.

This is an *independent* entry point next to the stratified engine in
engine.py.  It accepts already-grounded rules (no variables, no function
terms, no aggregates) that may contain **negation cycles** and evaluates them
under the well-founded semantics, returning three disjoint atom sets:
true, false and undefined.  An atom that merely lacks a positive proof is
*not* reported as false — it is false only when the alternating fixpoint
proves its negation.

Alternating fixpoint (Van Gelder): for a set I of atoms assumed false, let
T(I) be the least model of the positive reduct in which "not a" holds iff
a is not in I.  Starting from A0 = {} the sequence A(i+1) = T(A(i))
alternates between a growing lower bound A0 <= A2 <= ... (atoms certainly
true) and a shrinking upper bound A1 >= A3 >= ... (atoms possibly true).
When A(n+2) == A(n) the iteration has converged: true = A_even,
false = atoms - A_odd and undefined = A_odd - A_even.

Budgets are honest: exceeding the declared atom limit raises LimitError,
and exhausting the round budget returns WFResult(complete=False) with a
reason — the partial bounds are exposed for inspection but never presented
as a finished true/false/undefined answer.
"""

from __future__ import annotations

from .errors import LimitError, ValidationError
from .terms import Literal, Rule, Var, check_constant, check_predicate

# --- Declared interface limits -------------------------------------------

MAX_ATOMS = 64
MAX_RULES = 200
DEFAULT_MAX_ROUNDS = 128


class WFResult:
    """Outcome of evaluate_wf.

    complete is True iff the alternating fixpoint converged within the
    round budget.  Only then are true / false / undefined a finished
    answer.  rounds records one (lower, upper) snapshot per iteration
    round (frozensets of atoms) so convergence can be inspected; on
    budget exhaustion the partial bounds remain visible here.
    """

    __slots__ = ("complete", "reason", "true", "false", "undefined", "atoms", "rounds")

    def __init__(self, complete, reason, true, false, undefined, atoms, rounds):
        self.complete = complete
        self.reason = reason
        self.true = frozenset(true)
        self.false = frozenset(false)
        self.undefined = frozenset(undefined)
        self.atoms = frozenset(atoms)
        self.rounds = tuple(rounds)

    def __repr__(self):
        if not self.complete:
            return f"WFResult(complete=False, reason={self.reason!r})"
        return (
            f"WFResult(true={sorted(self.true)!r}, false={sorted(self.false)!r}, "
            f"undefined={sorted(self.undefined)!r})"
        )


# --- Validation -----------------------------------------------------------


def _check_ground_atom(atom, where):
    if not isinstance(atom, tuple) or not atom:
        raise ValidationError(f"{where}: atom must be a non-empty tuple, got {atom!r}")
    check_predicate(atom[0], where)
    for arg in atom[1:]:
        if isinstance(arg, Var):
            raise ValidationError(
                f"{where}: variable {arg!r} in grounded rule input; "
                "this entry point accepts only fully grounded rules"
            )
        check_constant(arg, where)


def _validate(facts, rules, max_rounds):
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
        raise ValidationError(f"max_rounds must be a positive int, got {max_rounds!r}")
    facts = [tuple(f) for f in facts]
    for i, f in enumerate(facts):
        _check_ground_atom(f, f"fact {i}")
    rules = list(rules)
    if len(rules) > MAX_RULES:
        raise LimitError(f"rule count {len(rules)} exceeds limit {MAX_RULES}")
    for i, r in enumerate(rules):
        if not isinstance(r, Rule):
            raise ValidationError(f"rule {i}: expected Rule, got {type(r).__name__}")
        _check_ground_atom(r.head, f"rule {i}, head")
        for j, lit in enumerate(r.body):
            if not isinstance(lit, Literal):
                raise ValidationError(
                    f"rule {i}, body literal {j}: expected Literal, got "
                    f"{type(lit).__name__}"
                )
            _check_ground_atom(lit.atom, f"rule {i}, body literal {j}")
    atoms = set(facts)
    for r in rules:
        atoms.add(r.head)
        for lit in r.body:
            atoms.add(lit.atom)
    if len(atoms) > MAX_ATOMS:
        raise LimitError(
            f"atom universe has {len(atoms)} atoms, exceeds limit {MAX_ATOMS}"
        )
    return frozenset(facts), rules, atoms


# --- Immediate consequence under a false-assumption set -------------------


def _consequence(rules, facts, true_set):
    """Least model of the positive reduct over the atom universe: "not a"
    holds iff a is NOT in true_set; iterate until no new head is derived."""
    derived = set(facts)
    changed = True
    while changed:
        changed = False
        for r in rules:
            if r.head in derived:
                continue
            ok = all(
                (lit.atom in derived) if lit.positive else (lit.atom not in true_set)
                for lit in r.body
            )
            if ok:
                derived.add(r.head)
                changed = True
    return frozenset(derived)


# --- Public entry point ---------------------------------------------------


def evaluate_wf(facts, rules, *, max_rounds=DEFAULT_MAX_ROUNDS):
    """Evaluate grounded rules over base facts under well-founded semantics
    and return a WFResult.

    Negation cycles are supported; atoms whose truth cannot be decided are
    reported in undefined rather than defaulted to false.  If the
    alternating fixpoint does not converge within max_rounds rounds, the
    result has complete=False and a reason — the partial bounds in rounds
    are *not* a finished answer.
    """
    facts, rules, atoms = _validate(facts, rules, max_rounds)

    # A(0) = {} ; A(n+1) = T(A(n)).  Even terms form the growing lower
    # bound, odd terms the shrinking upper bound.
    seq = [frozenset(), _consequence(rules, facts, frozenset())]
    rounds = []
    for _ in range(max_rounds):
        a_next = _consequence(rules, facts, seq[-1])
        seq.append(a_next)
        lower = seq[-3] if (len(seq) - 3) % 2 == 0 else seq[-2]
        upper = seq[-2] if (len(seq) - 3) % 2 == 0 else seq[-3]
        rounds.append((lower, upper))
        if seq[-1] == seq[-3]:
            # A(n+2) == A(n): converged.  The even-indexed bound is "true",
            # the odd-indexed bound is "possibly true".
            even, odd = (seq[-1], seq[-2]) if (len(seq) - 1) % 2 == 0 else (seq[-2], seq[-1])
            true = even
            false = atoms - odd
            undefined = odd - even
            return WFResult(True, None, true, false, undefined, atoms, rounds)

    return WFResult(
        False,
        f"round budget exhausted: no convergence within {max_rounds} rounds",
        frozenset(),
        frozenset(),
        frozenset(),
        atoms,
        rounds,
    )
