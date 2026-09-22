"""Deterministic offline demo of the stratified Datalog engine.

Computes, in well under 8 seconds and with no I/O beyond stdout:

  1. a normal result   -- recursive reachability + stratified negation on a
                          deterministically generated graph, with query
                          results and a provenance (source) chain;
  2. a real rejection  -- the fact budget is deliberately set too small, so
                          the engine returns an explicitly *incomplete*
                          result instead of a partial answer;
  3. a real exception  -- a program with a dependency cycle through
                          negation is rejected with StratificationError.

Run:  python demo.py
"""

from datalog import (
    Rule,
    StratificationError,
    Var,
    evaluate,
    neg,
    pos,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def build_graph():
    """Deterministic small graph: a chain 0..7 plus a branch and an
    isolated node 9 (no edges in or out)."""
    nodes = list(range(10))
    edges = [(i, i + 1) for i in range(8)]  # 0->1->...->8
    edges += [(2, 5), (3, 7)]               # deterministic extra branches
    return nodes, edges


def main():
    nodes, edges = build_graph()
    facts = [("node", n) for n in nodes] + [("edge", a, b) for a, b in edges]
    rules = [
        Rule(("reach", X, X), [pos("node", X)]),                           # 0
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                        # 1
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),    # 2
        Rule(("unreach", X), [pos("node", X), neg("reach", 0, X)]),        # 3
    ]

    print("=" * 68)
    print("1) Normal evaluation: reachability + stratified negation")
    print("=" * 68)
    res = evaluate(facts, rules)
    print(f"complete={res.complete}  total facts={len(res.facts)}  "
          f"derived={len(res.derived)}")
    targets = [f[2] for f in res.query("reach", 0, X)]
    print(f"nodes reachable from 0 : {targets}")
    missing = [f[1] for f in res.query("unreach", X)]
    print(f"nodes unreachable from 0 (via negation): {missing}")
    print("\nProvenance chain for reach(0, 4):")
    print(res.explain(("reach", 0, 4)))

    print()
    print("=" * 68)
    print("2) Triggered boundary: fact budget exceeded -> incomplete result")
    print("=" * 68)
    tight = evaluate(facts, rules, max_facts=len(facts) + 2)
    print(f"complete={tight.complete}")
    print(f"reason={tight.reason}")
    print(f"facts returned={len(tight.facts)} (input only: "
          f"{len(tight.derived)} derived — the overflowing round was discarded)")

    print()
    print("=" * 68)
    print("3) Triggered rejection: dependency cycle through negation")
    print("=" * 68)
    bad_rules = [
        Rule(("p", X), [pos("q", X)]),
        Rule(("q", X), [pos("node", X), neg("p", X)]),
    ]
    try:
        evaluate([("node", 1)], bad_rules)
        print("ERROR: expected StratificationError was not raised")
    except StratificationError as exc:
        print(f"StratificationError raised as expected:\n  {exc}")


if __name__ == "__main__":
    main()
