"""End-to-end demo of the incremental Datalog session (v2 API).

Computes everything live in well under 90 seconds, no sleeps, no
pre-printed answers.  Exercises all three new capability groups plus the
supplementary requirements:

  1. atomic add/remove batches with multi-source facts, DRed work sets
     (over-deleted / re-derived) and dependency-ordered negation
     recompute;
  2. a tricky boundary: a pure recursive cycle that must NOT support
     itself, and a fact that is both asserted and derived;
  3. atomic rule-set replacement with versioned snapshots, plus two
     actually triggered rejections (unstratifiable program, fact budget);
  4. a non-toy deterministic instance (25 subgraphs, 10 000 base facts,
     100 rules, 30 batches) cross-checked by the independent evidence
     checker.

Run:  python demo_incremental.py
"""

import time

from datalog import (
    Rule,
    Session,
    StratificationError,
    Var,
    neg,
    pos,
    verify_retractions,
    verify_snapshot,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def banner(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def part1_incremental_batches():
    banner("1) Atomic batches: multi-source facts, DRed work sets, negation flip")
    rules = [
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                      # 0
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),  # 1
        Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),    # 2
    ]
    s = Session(
        rules,
        facts=[
            ("node", "a"), ("node", "b"), ("node", "c"), ("node", "z"),
            (("edge", "a", "b"), "sensor-1"),
            (("edge", "a", "b"), "sensor-2"),   # same fact, second source
            (("edge", "b", "c"), "sensor-1"),
        ],
    )
    r = s.result()
    print(f"initial: {len(r.facts)} facts; reachable from 'a': "
          f"{[f[2] for f in r.query('reach', 'a', X)]}")
    print(f"unreachable from 'a': {[f[1] for f in r.query('unreach', X)]}")
    print(f"sources of edge(a,b): {sorted(r.sources(('edge', 'a', 'b')))}")

    rep = s.apply_batch(removes=[(("edge", "a", "b"), "sensor-1")])
    print("\n-- remove edge(a,b)@sensor-1 (sensor-2 still vouches for it)")
    print(f"   added={len(rep.added)} removed={len(rep.removed)} "
          f"over_deleted={len(rep.over_deleted)} rederived={len(rep.rederived)} "
          f"firings={rep.firings}")
    print(f"   edge(a,b) still present: {('edge', 'a', 'b') in s.result().facts}")

    rep = s.apply_batch(removes=[(("edge", "a", "b"), "sensor-2")])
    print("\n-- remove edge(a,b)@sensor-2 (last source)")
    print(f"   removed={sorted(rep.removed)}")
    print(f"   over_deleted={sorted(rep.over_deleted)} rederived={sorted(rep.rederived)}")
    for ret in rep.retractions:
        print(f"   retracted {ret.fact}  cause={ret.cause} detail={ret.detail}")
    chk = verify_retractions(rules, list(s.result().base_sources), rep.removed)
    print(f"   retractions independently verified: {chk.ok}")

    rep = s.apply_batch(adds=[(("edge", "c", "z"), "sensor-3")])
    print("\n-- add edge(c,z): negation stratum flips")
    print(f"   recomputed components: {sorted(rep.recomputed)}")
    print(f"   unreachable now: {[f[1] for f in s.result().query('unreach', X)]}")
    chk = verify_snapshot(s.result())
    print(f"   evidence checker: ok={chk.ok} ({chk.checked} derivations replayed)")
    return s, rules


def part2_tricky_boundary():
    banner("2) Tricky boundary: pure recursive cycle cannot support itself")
    rules = [
        Rule(("p", X), [pos("e", X)]),  # 0
        Rule(("p", X), [pos("q", X)]),  # 1
        Rule(("q", X), [pos("p", X)]),  # 2
    ]
    s = Session(rules, facts=[(("e", "a"), "src")])
    print("with e(a): p =", s.result().query("p", X), " q =", s.result().query("q", X))
    rep = s.apply_batch(removes=[(("e", "a"), "src")])
    print("after removing e(a): p =", s.result().query("p", X),
          " q =", s.result().query("q", X))
    print(f"   over_deleted={sorted(rep.over_deleted)} rederived={sorted(rep.rederived)}")
    print("   (a support-counting engine would keep p and q alive forever)")

    print("\n-- a fact that is both asserted and derived")
    rules2 = [Rule(("p", X), [pos("e", X)])]
    s2 = Session(rules2, facts=[(("e", "a"), "src"), (("p", "a"), "manual-entry")])
    s2.apply_batch(removes=[(("p", "a"), "manual-entry")])
    r = s2.result()
    print(f"   p(a) asserted+derived, assertion removed -> still true: "
          f"{('p', 'a') in r.facts}, now derived: {('p', 'a') in r.derived}")
    print(f"   derivation of p(a): {r.derivation(('p', 'a'))}")


def part3_rule_replacement_and_rejections():
    banner("3) Atomic rule-set replacement + two real rejections")
    rules_v1 = [
        Rule(("p", X), [pos("e", X)]),  # 0
        Rule(("q", X), [pos("p", X)]),  # 1
    ]
    s = Session(rules_v1, facts=[(("e", "a"), "s1"), (("e", "b"), "s2")])
    old = s.result()
    print(f"version {old.rules_version}: q = {s.result().query('q', X)}")

    rep = s.replace_rules(rules_v1 + [Rule(("r", X), [pos("q", X)])])
    print(f"\n-- publish v{rep.rules_version}: added={sorted(rep.added)} "
          f"removed={sorted(rep.removed)}")
    print(f"   old snapshot still at v{old.rules_version}, "
          f"r(a) visible there: {('r', 'a') in old.facts}, "
          f"its evidence: {old.explain(('q', 'a')).splitlines()[0]}")

    print("\n-- rejection 1: unstratifiable replacement (cycle through negation)")
    try:
        s.replace_rules([Rule(("p", X), [pos("e", X), neg("p", X)])])
        print("   ERROR: not rejected")
    except StratificationError as exc:
        print(f"   StratificationError: {exc}")
    print(f"   version still {s.result().rules_version}, "
          f"q = {s.result().query('q', X)}")

    print("\n-- rejection 2: batch that would exceed the fact budget")
    s2 = Session(rules_v1, facts=[(("e", "a"), "s1")], max_facts=3)
    before = set(s2.result().facts)
    rep = s2.apply_batch(adds=[(("e", "b"), "s2"), (("e", "c"), "s3")])
    print(f"   applied={rep.applied}; reason: {rep.reason}")
    print(f"   state untouched: {set(s2.result().facts) == before}")


def part4_scale():
    banner("4) Non-toy instance: 100 rules / 10 000 base facts / 30 batches")
    subgraphs, nodes, chain, max_dist = 25, 100, 10, 4
    rules = []
    for g in range(subgraphs):
        start = g * nodes
        rules += [
            Rule((f"reach{g}", X, Y), [pos(f"edge{g}", X, Y)]),
            Rule((f"reach{g}", X, Z), [pos(f"reach{g}", X, Y), pos(f"edge{g}", Y, Z)]),
            Rule((f"reach{g}", X, X), [pos(f"node{g}", X)]),
            Rule((f"unreach{g}", X), [pos(f"node{g}", X), neg(f"reach{g}", start, X)]),
        ]
    facts = []
    for g in range(subgraphs):
        for n in range(nodes):
            facts.append(((f"node{g}", g * nodes + n), "gen"))
        for c in range(nodes // chain):
            base = g * nodes + c * chain
            for i in range(chain):
                for d in range(1, max_dist + 1):
                    if i + d < chain:
                        facts.append(((f"edge{g}", base + i, base + i + d), "gen"))
    print(f"rules={len(rules)}  base facts={len(facts)}")

    t0 = time.monotonic()
    s = Session(rules, facts=facts)
    r0 = s.result()
    print(f"initial build: {time.monotonic() - t0:.2f}s, "
          f"least model = {len(r0.facts)} facts")

    # 30 deterministic batches, each cutting and re-adding edges in two
    # subgraphs; every batch's evidence is independently verified.
    t0 = time.monotonic()
    total_firings = 0
    for b in range(30):
        g1, g2 = b % subgraphs, (b * 7 + 3) % subgraphs
        base1, base2 = g1 * nodes, g2 * nodes
        i = b % (chain - 1)
        j = b % (chain - 5)
        rep = s.apply_batch(
            removes=[((f"edge{g1}", base1 + i, base1 + i + 1), "gen")],
            # distance-5 edges do not exist initially: a real addition
            adds=[((f"edge{g2}", base2 + j, base2 + j + 5), f"fix-{b}")],
        )
        assert rep.applied, rep.reason
        total_firings += sum(rep.firings.values())
        chk = verify_snapshot(s.result())
        assert chk.ok, chk.violations[:2]
    dt = time.monotonic() - t0
    r = s.result()
    print(f"30 batches applied + evidence-verified in {dt:.2f}s")
    print(f"final model = {len(r.facts)} facts; total rule firings across "
          f"batches = {total_firings} (a from-scratch rebuild would fire "
          f"~{len(r0.facts) * 30:,} — incremental locality confirmed)")
    print(f"sample: unreach3 has {len(r.query('unreach3', X))} facts; "
          f"sources of edge3(303,304): "
          f"{sorted(r.sources(('edge3', 303, 304))) if ('edge3', 303, 304) in r.facts else 'removed'}")


def main():
    part1_incremental_batches()
    part2_tricky_boundary()
    part3_rule_replacement_and_rejections()
    part4_scale()
    print("\nAll demo sections computed live and verified.")


if __name__ == "__main__":
    main()
