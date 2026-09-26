"""Deterministic offline demo of the well-founded ground evaluator.

Computes live, in well under 90 seconds, with no I/O beyond stdout:

  1. a mutual-negation cycle (a :- not b, b :- not a) that the stratified
     engine would reject, evaluated here to *undefined* -- printed with
     the per-round lower/upper bounds of the alternating fixpoint;
  2. the same program plus one fact, showing how the added information
     collapses the undefined atoms to definite true/false;
  3. a genuinely false atom next to the cycle (no proof AND outside the
     upper bound), contrasting "false" with "undefined";
  4. a deliberately too-small round budget, returning an explicitly
     incomplete result instead of a fake "undefined" answer.

Run:  python demo_iteration.py
"""

from datalog import Rule, evaluate_ground, neg, pos


def fmt(atoms):
    if not atoms:
        return "{}"
    body = ", ".join(
        f"{a[0]}({', '.join(repr(x) for x in a[1:])})" for a in sorted(atoms)
    )
    return "{ " + body + " }"


def show(result):
    print(f"  converged (complete) = {result.complete}"
          + ("" if result.complete else f"  reason: {result.reason}"))
    print("  alternating fixpoint, round by round:")
    for rd in result.rounds:
        print(f"    round {rd.index}: lower (certainly true) = {fmt(rd.lower)}")
        print(f"             upper (possibly true) = {fmt(rd.upper)}")
    if result.complete:
        print(f"  => true      = {fmt(result.true)}")
        print(f"  => false     = {fmt(result.false)}")
        print(f"  => undefined = {fmt(result.undefined)}")


def main():
    cycle_rules = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]

    print("=" * 70)
    print("1) Negation cycle:  a :- not b.   b :- not a.")
    print("=" * 70)
    print("  Neither atom has a positive proof, but neither is genuinely")
    print("  false: the well-founded semantics keeps both UNDEFINED.")
    show(evaluate_ground([], cycle_rules))

    print()
    print("=" * 70)
    print("2) Same cycle + one fact:  a.")
    print("=" * 70)
    print("  The added fact breaks the symmetry: a becomes TRUE, b FALSE,")
    print("  and the undefined group empties out.")
    show(evaluate_ground([("a",)], cycle_rules))

    print()
    print("=" * 70)
    print("3) False is not undefined:  c :- d.  (d has no rule, no fact)")
    print("=" * 70)
    print("  d (and hence c) can never be derived AND drops out of the")
    print("  upper bound, so both are genuinely FALSE -- unlike the cycle.")
    rules3 = cycle_rules + [Rule(("c",), [pos("d")])]
    show(evaluate_ground([], rules3))

    print()
    print("=" * 70)
    print("4) Triggered boundary: round budget exhausted (max_rounds=1)")
    print("=" * 70)
    tight = evaluate_ground([], cycle_rules, max_rounds=1)
    show(tight)
    print("  true/false/undefined are None here: an exhausted budget is")
    print("  reported as incomplete, never disguised as 'undefined'.")


if __name__ == "__main__":
    main()
