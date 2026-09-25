"""Deterministic offline demo of the stratified Datalog engine.

Computes, in well under 90 seconds and with no I/O beyond stdout:

  1. a normal result   -- recursive reachability + stratified negation on a
                          deterministically generated graph, with query
                          results and a provenance (source) chain;
  2. group aggregation -- count/sum per group over derived facts, an
                          explicit empty group producing zero, an aggregate
                          result consumed by a higher rule, and the recorded
                          contributing facts with their stable ids;
  3. a real rejection  -- the fact budget is deliberately set too small, so
                          the engine returns an explicitly *incomplete*
                          result instead of a partial answer (and no
                          half-finished aggregate totals);
  4. a real exception  -- a program with a dependency cycle through an
                          aggregate edge is rejected with
                          AggregateStratificationError.

Run:  python demo.py
"""

from datalog import (
    Aggregate,
    AggregateStratificationError,
    Rule,
    Var,
    evaluate,
    neg,
    pos,
)

X, Y, Z, V, N = Var("X"), Var("Y"), Var("Z"), Var("V"), Var("N")


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
    print("2) Group aggregation: count/sum per group + explicit empty group")
    print("=" * 68)
    # Sales ledger; 'east' has a negative adjustment row.
    ledger = [
        ("sale", "east", 30), ("sale", "east", -5), ("sale", "east", 12),
        ("sale", "west", 8), ("sale", "west", 9),
        ("region", "east"), ("region", "west"),
        ("region", "south"), ("region", "north"),
    ]
    agg_rules = [
        # 0: net amount per region (sum of integer amounts)
        Rule(("net", X, N), [pos("sale", X, V)], Aggregate("sum", V)),
        # 1: number of ledger rows per region (count of distinct facts)
        Rule(("rows", X, N), [pos("sale", X, V)], Aggregate("count")),
        # 2: regions whose net is exactly 37 (a higher rule over the aggregate)
        Rule(("target_hit", X), [pos("net", X, 37)]),
        # 3-4: which regions have no rows fact at all (negation over the
        # aggregate).  'south' is in the explicit group domain so it carries
        # a real rows(_, 0) fact (not absent); 'north' is outside the domain
        # and truly has no rows fact, so it is the one negation finds.
        Rule(("has_rows", X), [pos("rows", X, N)]),
        Rule(("no_rows_fact", X), [pos("region", X), neg("has_rows", X)]),
    ]
    # Explicit group domain: 'south' has no sales, so it appears only
    # because the caller listed it -- with a zero.
    domain = {0: [("south",)], 1: [("south",)]}
    ares = evaluate(ledger, agg_rules, groups=domain)
    print(f"complete={ares.complete}")
    for f in ares.query("net", X, N):
        print(f"  net({f[1]!r}) = {f[2]}")
    for f in ares.query("rows", X, N):
        print(f"  rows({f[1]!r}) = {f[2]}")
    print(f"  target_hit: {[f[1] for f in ares.query('target_hit', X)]}")
    print(f"  no rows fact (outside explicit domain): "
          f"{[f[1] for f in ares.query('no_rows_fact', X)]}")
    print("\nProvenance for net('east', 37) "
          "(contributing facts carry stable ids):")
    print(ares.explain(("net", "east", 37)))
    d = ares.derivation(("net", "east", 37))
    print(f"  aggregate value={d.value}  "
          f"contributor ids={sorted(i for row in d.contributor_ids for i in row)}")
    dsouth = ares.derivation(("net", "south", 0))
    print(f"  net('south', 0): explicit empty group = {dsouth.explicit}, "
          f"contributors = {dsouth.contributors}")

    print()
    print("=" * 68)
    print("3) Triggered boundary: fact budget exceeded -> incomplete result")
    print("=" * 68)
    tight = evaluate(facts, rules, max_facts=len(facts) + 2)
    print(f"complete={tight.complete}")
    print(f"reason={tight.reason}")
    print(f"facts returned={len(tight.facts)} (input only: "
          f"{len(tight.derived)} derived — the overflowing round was discarded)")
    # Same protection for aggregates: a batch that does not fit is not
    # published at all (no half-finished totals).
    tight_agg = evaluate(ledger, agg_rules, groups=domain,
                         max_facts=len(ledger) + 3)
    print(f"aggregate batch under tight budget: complete={tight_agg.complete}")
    print(f"reason={tight_agg.reason}")
    print(f"aggregate facts published: {len(tight_agg.query('net', X, N))} "
          f"(none — the whole batch was withheld)")

    print()
    print("=" * 68)
    print("4) Triggered rejection: recursive cycle through an aggregate edge")
    print("=" * 68)
    bad_rules = [
        Rule(("t", X, N), [pos("s", X, V)], Aggregate("sum", V)),  # t sums s
        Rule(("s", X, V), [pos("t", X, V)]),                       # s needs t
    ]
    try:
        evaluate([("s", "a", 1)], bad_rules)
        print("ERROR: expected AggregateStratificationError was not raised")
    except AggregateStratificationError as exc:
        print(f"AggregateStratificationError raised as expected:\n  {exc}")


if __name__ == "__main__":
    main()
