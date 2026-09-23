"""Deterministic offline demo of the incremental stratified-Datalog engine.

Shows, really computed in well under 90 seconds with no I/O beyond stdout:

  1. end-to-end normal results using all three new capability groups at
     once: multi-source atomic fact batches, DRed invalidation + re-
     derivation work sets on the positive recursive layer, negation-stratum
     flips, and an atomic rule-set replacement with an old-version snapshot;
  2. a tricky boundary: a pure recursive cycle must collapse when its last
     base support is removed (no self-support through support counting);
  3. actually triggered rejections: an unsafe rule replacement (rejected,
     old rules stay) and a budget-exceeding batch (rolled back atomically);
  4. the main non-toy scale: 100 rules / 10 000 base facts / 100 batches.

Run:  python demo.py
"""

import time

from datalog import (
    Rule,
    Session,
    StratificationError,
    UnsafeRuleError,
    Var,
    neg,
    pos,
)

X, Y, Z = Var("X"), Var("Y"), Var("Z")


def section(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def demo_end_to_end():
    section("1) End-to-end: batches + DRed work sets + negation flip + rule swap")
    rules = [
        Rule(("reach", X, Y), [pos("edge", X, Y)]),                       # 0
        Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),   # 1
        Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),     # 2
    ]
    s = Session(
        rules,
        facts=[
            (("node", n), "seed") for n in ("a", "b", "c", "d")
        ]
        + [
            (("edge", "a", "b"), "sensor-1"),
            (("edge", "a", "b"), "sensor-2"),   # same fact, two sources
            (("edge", "b", "c"), "sensor-1"),
        ],
    )
    snap0 = s.snapshot()
    print(f"initial: {s.stats()}")
    print(f"unreachable from 'a': {[f[1] for f in snap0.query('unreach', X)]}")

    # batch 1: remove ONE of two sources -> nothing may change
    rep = s.apply_batch(remove=[(("edge", "a", "b"), "sensor-1")])
    print(f"\nbatch {rep.batch_id}: remove edge(a,b)@sensor-1 "
          f"(sensor-2 still supports it)")
    print(f"  added={len(rep.added_facts)} removed={len(rep.removed_facts)} "
          f"strata_touched={len(rep.strata)}")

    # batch 2: remove the LAST source -> cascade through the recursive layer
    rep = s.apply_batch(remove=[(("edge", "a", "b"), "sensor-2")])
    print(f"\nbatch {rep.batch_id}: remove edge(a,b)@sensor-2 (last source)")
    print(f"  removed: {sorted(rep.removed_facts)}")
    for f, reason in sorted(rep.retraction_reasons.items()):
        print(f"  retraction {f}: {reason}")
        assert s.verify_retraction(f)
    print("  all retractions independently verified: OK")

    # batch 3: a new path flips the negation stratum
    rep = s.apply_batch(add=[(("edge", "a", "d"), "sensor-3"), (("edge", "d", "c"), "sensor-3")])
    print(f"\nbatch {rep.batch_id}: add path a->d->c")
    print(f"  unreach now: {[f[1] for f in s.snapshot().query('unreach', X)]}")
    for st in rep.strata:
        print(f"  stratum {st.stratum} ({st.kind}): recomputed={st.recomputed} "
              f"deleted={len(st.deleted)} rederived={len(st.rederived)} "
              f"inserted={len(st.inserted)} rules_fired={sorted(st.rules_fired)}")

    # batch 4: atomic rule replacement; the old snapshot keeps its version
    old_snap = s.snapshot()
    rep = s.replace_rules(rules + [Rule(("bonus", X), [pos("node", X), neg("unreach", X)])])
    print(f"\nbatch {rep.batch_id}: rule set replaced (version "
          f"{old_snap.rule_version} -> {rep.rule_version}), "
          f"added={sorted(rep.added_facts)}")
    print(f"  old snapshot (v{old_snap.rule_version}) still has no 'bonus': "
          f"{old_snap.query('bonus', X) == []}")
    print(f"  old snapshot evidence still valid: {old_snap.verify_all()[0]}")
    print(f"  new snapshot evidence valid: {s.snapshot().verify_all()[0]}")
    print("\nsource chain for reach('a','c') in the new version:")
    print(s.snapshot().explain(("reach", "a", "c")))


def demo_cycle_boundary():
    section("2) Tricky boundary: a pure recursive cycle cannot support itself")
    rules = [
        Rule(("p", X), [pos("e", X)]),
        Rule(("p", X), [pos("q", X)]),
        Rule(("q", X), [pos("p", X)]),
    ]
    s = Session(rules, facts=[(("e", 1), "only-source")])
    print(f"with e(1): derived {sorted(f for f in s.current_facts() if f[0] != 'e')}")
    rep = s.apply_batch(remove=[(("e", 1), "only-source")])
    print(f"after removing the only base support: removed={sorted(rep.removed_facts)}")
    print(f"p(1) independently verified retracted: {s.verify_retraction(('p', 1))}")
    print(f"q(1) independently verified retracted: {s.verify_retraction(('q', 1))}")


def demo_rejections():
    section("3) Actually triggered rejections (state stays untouched)")
    s = Session([Rule(("p", X), [pos("e", X)])], facts=[(("e", 1), "s")])
    try:
        s.replace_rules([Rule(("p", X), [pos("e", Y)])])
        print("ERROR: unsafe rule was accepted")
    except UnsafeRuleError as exc:
        print(f"unsafe rule replacement rejected: {exc}")
    try:
        s.replace_rules([Rule(("p", X), [pos("e", X), neg("p", X)])])
        print("ERROR: unstratifiable rules were accepted")
    except StratificationError as exc:
        print(f"unstratifiable replacement rejected: {exc}")
    print(f"session unchanged: facts={sorted(s.current_facts())}, "
          f"rule_version={s.rule_version}")

    tight = Session(
        [Rule(("reach", X, Y), [pos("edge", X, Y)]),
         Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)])],
        facts=[(("edge", i, i + 1), "s") for i in range(3)],
        max_facts=14,
    )
    before = tight.current_facts()
    rep = tight.apply_batch(add=[(("edge", i, i + 1), "s") for i in range(3, 6)])
    print(f"\nbudget-limited batch: complete={rep.complete}")
    print(f"  reason: {rep.reason}")
    print(f"  state rolled back: {tight.current_facts() == before}")


def demo_scale():
    section("4) Main scale: 100 rules / 10 000 base facts / 100 batches")
    from tests_extended.test_scale import (
        NODES,
        build_program,
        initial_facts,
        make_batches,
    )

    rules = build_program()
    facts = initial_facts()
    t0 = time.monotonic()
    s = Session(rules, facts=facts)
    print(f"init: {len(rules)} rules, {len(facts)} base facts, "
          f"{s.stats()['total_facts']} total facts in {time.monotonic() - t0:.2f}s")
    t0 = time.monotonic()
    worked = 0
    for adds, rems in make_batches():
        rep = s.apply_batch(add=adds, remove=rems)
        assert rep.complete, rep.reason
        worked += sum(len(st.deleted) + len(st.rederived) + len(st.inserted)
                      for st in rep.strata)
    dt = time.monotonic() - t0
    stats = s.stats()
    print(f"100 atomic batches in {dt:.2f}s "
          f"({worked} facts touched across all work sets)")
    print(f"final: {stats}")
    ok, problems = s.snapshot().verify_all()
    print(f"full evidence check over {stats['derived_facts']} derived facts: "
          f"{'OK' if ok else problems[:3]}")
    assert NODES == 2500


def main():
    demo_end_to_end()
    demo_cycle_boundary()
    demo_rejections()
    demo_scale()
    print("\nAll demo sections completed (everything above was computed live).")


if __name__ == "__main__":
    main()
