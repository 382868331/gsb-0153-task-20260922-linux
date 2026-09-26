"""Well-founded evaluation of finite ground rule sets (negation cycles OK).

This is an *independent* entry point next to the stratified engine in
`engine.py`.  It accepts an already-ground, finite program (facts plus
rules whose head and body atoms contain no variables) and evaluates it
under the **well-founded semantics** via the **alternating fixpoint**
construction.  Unlike the stratified engine, programs whose dependency
graph contains cycles through negation (e.g. `a :- not b` together with
`b :- not a`) are *not* rejected; atoms whose truth cannot be decided
come back as `undefined` instead of being silently treated as false.

Definitions
-----------
* **Atom universe** `U`: every ground atom occurring in the input facts,
  in a rule head, or in a rule body literal.  It is finite by construction
  and limited to `MAX_ATOMS` (64) atoms.
* **Immediate consequence operator** `T(J)`: the least set of atoms
  closed under the rules when every negated literal `not a` is read as
  "`a` is not in `J`".  Computed by plain bottom-up iteration.
* **Alternating fixpoint**: `A_0 = {}`, `A_{k+1} = T(A_k)`.  The even
  members form an ascending chain of *lower* bounds (atoms certainly
  true), the odd members a descending chain of *upper* bounds (atoms
  still possibly true).  The sequence is monotone in pairs, so a repeated
  value means both bounds are final.
* **Result**: `true` = final lower bound, `false` = `U` minus the
  final upper bound, `undefined` = upper minus lower.  An atom without
  any positive proof is therefore *not* automatically false: it is false
  only when it also falls out of the upper bound.

Budget
------
`max_rounds` bounds the number of alternating iterations.  Exhaustion
is reported as `complete=False` with a reason, and `true`/`false`/
`undefined` are `None` -- an exhausted budget is never disguised as
a genuine "undefined" answer.
"""

from __future__ import annotations

from collections import namedtuple

from .errors import LimitError, ValidationError
from .terms import Literal, Rule, Var, check_constant, check_predicate

# --- Declared interface limits -------------------------------------------

MAX_ATOMS = 64
DEFAULT_MAX_ROUNDS = 128


#: One recorded alternating-fixpoint iteration: `lower`/`upper` are
#: the bounds on certainly-true / possibly-true atoms after that round.
Round = namedtuple("Round", ["index", "lower", "upper"])


class WFResult:
    """Outcome of :func:`evaluate_ground`.

    Attributes:
        complete: `True` iff the alternating fixpoint converged within
            the round budget.  When `False`, `reason` explains why and
            `true`/`false`/`undefined` are `None` (a budget
            exhaustion is not a valid three-valued answer).
        reason: `None` when complete, else a human-readable cause.
        true / false / undefined: frozensets of atoms partitioning the
            atom universe, or `None` when incomplete.
        atoms: the full atom universe (frozenset of ground atom tuples).
        rounds: tuple of :class:`Round` snapshots, one per alternating
            iteration, for inspecting convergence.
    """

    def __init__(self, *, complete, reason, true, false, undefined, atoms, rounds):
        self.complete = complete
        self.reason = reason
        self.true = true
        self.false = false
        self.undefined = undefined
        self.atoms = frozenset(atoms)
        self.rounds = tuple(rounds)

    def status(self, atom):
        """`"true"` / `"false"` / `undefined` for `atom`.

        Raises `KeyError` for an atom outside the universe and
        `RuntimeError` when the result is incomplete.
        """
        atom = tuple(atom)
        if atom not in self.atoms:
            raise KeyError(f"atom not in the universe: {atom!r}")
        if not self.complete:
            raise RuntimeError(
                f"incomplete result ({self.reason}); no truth values available"
            )
        if atom in self.true:
            return "true"
        if atom in self.undefined:
            return "undefined"
        return "false"


# --- Validation -----------------------------------------------------------

def _check_ground_atom(atom, where):
    if not isinstance(atom, (tuple, list)) or not atom:
        raise ValidationError(f"{where}: atom must be a non-empty tuple, got {atom!r}")
    atom = tuple(atom)
    check_predicate(atom[0], where)
    for j, arg in enumerate(atom[1:]):
        if isinstance(arg, Var):
            raise ValidationError(
                f"{where}, argument {j}: variable {arg!r} not allowed; "
                f"this entry point takes already-ground rules only"
            )
        check_constant(arg, f"{where}, argument {j}")
    return atom


def _validate_ground_program(facts, rules):
    if isinstance(facts, (str, bytes)) or not hasattr(facts, "__iter__"):
        raise ValidationError(
            f"facts must be an iterable of ground atom tuples, got {type(facts).__name__}"
        )
    norm_facts = []
    seen_facts = set()
    for i, fact in enumerate(facts):
        fact = _check_ground_atom(fact, f"fact {i}")
        if fact not in seen_facts:  # set semantics: duplicates collapse
            seen_facts.add(fact)
            norm_facts.append(fact)

    if isinstance(rules, (str, bytes)) or not hasattr(rules, "__iter__"):
        raise ValidationError(
            f"rules must be an iterable of Rule objects, got {type(rules).__name__}"
        )
    norm_rules = []
    for ri, rule in enumerate(rules):
        where = f"rule {ri}"
        if not isinstance(rule, Rule):
            raise ValidationError(f"{where}: expected a Rule, got {rule!r}")
        head = _check_ground_atom(rule.head, f"{where}, head")
        body = []
        for bi, lit in enumerate(rule.body):
            lwhere = f"{where}, body literal {bi}"
            if not isinstance(lit, Literal):
                raise ValidationError(
                    f"{lwhere}: expected a Literal (use pos()/neg()), got {lit!r}"
                )
            if not isinstance(lit.positive, bool):
                raise ValidationError(
                    f"{lwhere}: polarity must be a bool, got {lit.positive!r}"
                )
            atom = _check_ground_atom(lit.atom, lwhere)
            body.append(Literal(atom, lit.positive))
        norm_rules.append(Rule(head, body))

    # Fixed arity per predicate, across facts, heads and body literals.
    arity = {}

    def note(atom, where):
        pred, n = atom[0], len(atom) - 1
        if pred in arity:
            if arity[pred] != n:
                raise ValidationError(
                    f"{where}: predicate {pred!r} used with arity {n}, "
                    f"but already declared with arity {arity[pred]} (fixed arity)"
                )
        else:
            arity[pred] = n

    for i, fact in enumerate(norm_facts):
        note(fact, f"fact {i}")
    for ri, rule in enumerate(norm_rules):
        note(rule.head, f"rule {ri}, head")
        for bi, lit in enumerate(rule.body):
            note(lit.atom, f"rule {ri}, body literal {bi}")

    # Atom universe: facts + heads + body atoms (all ground, hence finite).
    universe = set(norm_facts)
    for rule in norm_rules:
        universe.add(rule.head)
        for lit in rule.body:
            universe.add(lit.atom)
    if len(universe) > MAX_ATOMS:
        raise LimitError(
            f"atom universe too large: {len(universe)} > limit {MAX_ATOMS}"
        )
    return norm_facts, norm_rules, frozenset(universe)


# --- Alternating fixpoint -------------------------------------------------

def _consequences(rules, neg_interpretation):
    """Least fixpoint of the positive part, reading `not a` as
    `a not in neg_interpretation` (the operator T of the alternating
    construction)."""
    derived = set()
    changed = True
    while changed:
        changed = False
        for rule in rules:
            if rule.head in derived:
                continue
            ok = True
            for lit in rule.body:
                if lit.positive:
                    if lit.atom not in derived:
                        ok = False
                        break
                elif lit.atom in neg_interpretation:
                    ok = False
                    break
            if ok:
                derived.add(rule.head)
                changed = True
    return frozenset(derived)


def evaluate_ground(facts, rules, *, max_rounds=DEFAULT_MAX_ROUNDS):
    """Evaluate a finite ground rule set under the well-founded semantics.

    Args:
        facts: iterable of ground atom tuples `(pred, const, ...)`.
        rules: iterable of :class:`Rule` objects with fully ground head
            and body atoms (no variables, no function terms, no
            aggregation).
        max_rounds: alternating-iteration budget, an `int` >= 1
            (`bool` rejected).  Exhaustion yields an incomplete
            :class:`WFResult`, never a fake "undefined" answer.

    Returns:
        A :class:`WFResult`.  When `complete` is `True`,
        `true`/`false`/`undefined` partition the atom universe.

    Raises:
        ValidationError: malformed input, variables in the program, arity
            mismatch, or an invalid `max_rounds` (located message).
        LimitError: atom universe larger than 64 atoms.
    """
    if isinstance(max_rounds, bool) or not isinstance(max_rounds, int):
        raise ValidationError(
            f"max_rounds must be an int (bool rejected), got {max_rounds!r}"
        )
    if max_rounds < 1:
        raise ValidationError(f"max_rounds must be >= 1, got {max_rounds}")

    norm_facts, norm_rules, universe = _validate_ground_program(facts, rules)
    program = list(norm_rules) + [Rule(f, ()) for f in norm_facts]

    lower = frozenset()          # best certainly-true bound so far (even A_k)
    upper = frozenset(universe)  # best possibly-true bound so far (odd A_k)
    rounds = []
    seen = {frozenset()}         # values of A_k already visited; A_0 = {}
    current = frozenset()
    converged = False
    for k in range(max_rounds):
        current = _consequences(program, current)
        if k % 2 == 0:           # A_1, A_3, ... are upper bounds
            upper = current
        else:                    # A_2, A_4, ... are lower bounds
            lower = current
        rounds.append(Round(k + 1, lower, upper))
        if current in seen:
            # The sequence alternates monotonically in pairs; a repeated
            # value means both chains have stabilised.
            converged = True
            break
        seen.add(current)

    if not converged:
        return WFResult(
            complete=False,
            reason=f"round budget exhausted: {max_rounds} alternating "
            f"iterations without convergence",
            true=None,
            false=None,
            undefined=None,
            atoms=universe,
            rounds=rounds,
        )

    return WFResult(
        complete=True,
        reason=None,
        true=lower,
        false=frozenset(universe - upper),
        undefined=frozenset(upper - lower),
        atoms=universe,
        rounds=rounds,
    )
