"""Demo of the well-founded grounded-rule evaluator (datalog.wellfounded).

Runs live in well under 90 seconds (actual runtime is milliseconds) and
prints the per-round lower/upper bounds so convergence can be checked by
eye.  Nothing is precomputed or replayed.
"""

from datalog import Rule, evaluate_wf, neg, pos


def show(title, facts, rules, **kw):
    print("=" * 68)
    print(title)
    print("facts:", sorted(facts) or "(none)")
    for r in rules:
        print("   ", r)
    res = evaluate_wf(facts, rules, **kw)
    for i, (lower, upper) in enumerate(res.rounds):
        print(f"  round {i}: lower={sorted(lower)} upper={sorted(upper)}")
    if not res.complete:
        print("  INCOMPLETE:", res.reason)
        print("  (partial bounds above are inspectable, not a final answer)")
    else:
        print("  true     :", sorted(res.true))
        print("  false    :", sorted(res.false))
        print("  undefined:", sorted(res.undefined))
    print()
    return res


def main():
    cycle = [Rule(("a",), [neg("b")]), Rule(("b",), [neg("a")])]

    show("1) a depends on not b, b depends on not a -> both undefined",
         [], cycle)

    show("2) adding fact a eliminates the uncertainty",
         [("a",)], cycle)

    show("3) stratified program: everything decided",
         [("edge", 1, 2), ("node", 2)],
         [
             Rule(("reach", 1, 2), [pos("edge", 1, 2)]),
             Rule(("dead", 2), [pos("node", 2), neg("reach", 1, 2)]),
         ])

    show("4) round budget exhausted -> honest incomplete, not fake undefined",
         [("q",)], [Rule(("p",), [neg("q")])], max_rounds=2)


if __name__ == "__main__":
    main()
