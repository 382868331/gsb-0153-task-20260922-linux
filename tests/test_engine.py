"""Tests for the stratified Datalog engine.

The expected values come from an *independent* naive fixpoint reference
implemented in this file (``naive_evaluate``) — it shares no code with the
engine under test — plus hand-computed sets for the small targeted cases.
"""

import random
import unittest

from datalog import (
    LimitError,
    StratificationError,
    UnsafeRuleError,
    ValidationError,
    Rule,
    Var,
    evaluate,
    neg,
    pos,
)

X, Y, Z, W = Var("X"), Var("Y"), Var("Z"), Var("W")


# ---------------------------------------------------------------------------
# Independent naive reference implementation (does NOT import the engine).
# Naive fixpoint per stratum: re-evaluate every rule over the full
# accumulated store until nothing changes.  Set semantics only; no
# provenance, no budget, no semi-naive optimisation.
# ---------------------------------------------------------------------------

def _naive_stratify(preds, rules):
    edges = []
    for r in rules:
        for lit in r.body:
            edges.append((r.head[0], lit.atom[0], not lit.positive))
    stratum = {p: 0 for p in preds}
    for _ in range(len(preds) + 1):
        changed = False
        for h, b, is_neg in edges:
            need = stratum[b] + (1 if is_neg else 0)
            if need > stratum[h]:
                stratum[h] = need
                changed = True
        if not changed:
            return stratum
    raise AssertionError("reference: unstratifiable program")


def _naive_fire(rule, store):
    """All head facts produced by one naive (non-semi-naive) rule firing."""
    pos_lits = [l for l in rule.body if l.positive]
    neg_lits = [l for l in rule.body if not l.positive]
    out = set()

    def subst(atom, env):
        return (atom[0],) + tuple(env[a] if isinstance(a, Var) else a for a in atom[1:])

    def bt(i, env):
        if i == len(pos_lits):
            for lit in neg_lits:
                g = subst(lit.atom, env)
                if g in store.get(g[0], ()):
                    return
            out.add(subst(rule.head, env))
            return
        atom = pos_lits[i].atom
        for fact in store.get(atom[0], ()):
            env2 = dict(env)
            ok = True
            for a, v in zip(atom[1:], fact[1:]):
                if isinstance(a, Var):
                    if a in env2:
                        if env2[a] != v:
                            ok = False
                            break
                    else:
                        env2[a] = v
                elif a != v:
                    ok = False
                    break
            if ok:
                bt(i + 1, env2)

    bt(0, {})
    return out


def naive_evaluate(facts, rules):
    preds = {f[0] for f in facts}
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = _naive_stratify(preds, rules)
    store = {}
    for f in facts:
        store.setdefault(f[0], set()).add(f)
    by_stratum = {}
    for r in rules:
        by_stratum.setdefault(stratum[r.head[0]], []).append(r)
    for s in sorted(by_stratum):
        while True:
            new = set()
            for r in by_stratum[s]:
                for f in _naive_fire(r, store):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            for f in new:
                store.setdefault(f[0], set()).add(f)
    return {f for fs in store.values() for f in fs}


# ---------------------------------------------------------------------------
# Targeted tests
# ---------------------------------------------------------------------------

class TestReachability(unittest.TestCase):
    """Positive recursion: transitive closure over a small graph."""

    def setUp(self):
        self.facts = [
            ("edge", "a", "b"),
            ("edge", "b", "c"),
            ("edge", "c", "d"),
            ("edge", "a", "e"),
        ]
        self.rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        ]

    def test_matches_naive_reference(self):
        res = evaluate(self.facts, self.rules)
        self.assertTrue(res.complete)
        self.assertIsNone(res.reason)
        self.assertEqual(set(res.facts), naive_evaluate(self.facts, self.rules))

    def test_expected_reach_set(self):
        res = evaluate(self.facts, self.rules)
        got = {(f[1], f[2]) for f in res.query("reach", X, Y)}
        self.assertEqual(
            got,
            {("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"),
             ("a", "c"), ("b", "d"), ("a", "d")},
        )

    def test_query_with_constants(self):
        res = evaluate(self.facts, self.rules)
        self.assertEqual(
            [f[2] for f in res.query("reach", "a", X)], ["b", "c", "d", "e"]
        )
        self.assertEqual(res.query("reach", "d", X), [])


class TestStratifiedNegation(unittest.TestCase):
    """Negation over a lower stratum (unreachable nodes)."""

    def setUp(self):
        self.facts = [
            ("node", "a"), ("node", "b"), ("node", "c"), ("node", "z"),
            ("edge", "a", "b"), ("edge", "b", "c"),
        ]
        self.rules = [
            Rule(("reach", X, X), [pos("node", X)]),
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
            Rule(("unreach", X), [pos("node", X), neg("reach", "a", X)]),
        ]

    def test_unreach_set(self):
        res = evaluate(self.facts, self.rules)
        self.assertTrue(res.complete)
        self.assertEqual([f[1] for f in res.query("unreach", X)], ["z"])

    def test_matches_naive_reference(self):
        res = evaluate(self.facts, self.rules)
        self.assertEqual(set(res.facts), naive_evaluate(self.facts, self.rules))

    def test_two_layer_negation(self):
        # odd(X): node not directly ok; even(X): node not odd  (2 neg layers)
        facts = [("node", 1), ("node", 2), ("ok", 1)]
        rules = [
            Rule(("odd", X), [pos("node", X), neg("ok", X)]),
            Rule(("even", X), [pos("node", X), neg("odd", X)]),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual([f[1] for f in res.query("odd", X)], [2])
        self.assertEqual([f[1] for f in res.query("even", X)], [1])
        self.assertEqual(set(res.facts), naive_evaluate(facts, rules))


class TestDuplicateDerivationAndProvenance(unittest.TestCase):
    def test_duplicate_derivation_collapses_to_min(self):
        # p(a) derivable by rule 0 and rule 1 at the same depth:
        # the lexicographically smaller (rule_index, children) wins.
        facts = [("e", "a")]
        rules = [
            Rule(("p", X), [pos("e", X)]),
            Rule(("p", X), [pos("e", X), pos("e", X)]),
        ]
        res = evaluate(facts, rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("p", X), [("p", "a")])  # set semantics
        d = res.derivation(("p", "a"))
        self.assertEqual(d.rule_index, 0)
        self.assertEqual(d.children, (("e", "a"),))
        self.assertEqual(set(res.facts), naive_evaluate(facts, rules))

    def test_shortest_derivation_wins(self):
        # reach(a,c) has a depth-1 proof (rule 0 via big-edge) and a depth-2
        # proof (rule 2 transitivity); the shortest must be recorded.
        facts = [("edge", "a", "b"), ("edge", "b", "c"), ("bigedge", "a", "c")]
        rules = [
            Rule(("reach", X, Y), [pos("bigedge", X, Y)]),
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("reach", Y, Z)]),
        ]
        res = evaluate(facts, rules)
        d = res.derivation(("reach", "a", "c"))
        self.assertEqual(d.rule_index, 0)
        self.assertEqual(d.children, (("bigedge", "a", "c"),))

    def test_lexicographic_children_tiebreak(self):
        # Same rule, same head fact, two bindings: children (e(a,1)) < (e(a,2)).
        facts = [("e", "a", 1), ("e", "a", 2)]
        rules = [Rule(("p", X), [pos("e", X, Y)])]
        res = evaluate(facts, rules)
        d = res.derivation(("p", "a"))
        self.assertEqual(d.children, (("e", "a", 1),))

    def test_provenance_chain_is_sound_and_acyclic(self):
        facts = [("edge", "a", "b"), ("edge", "b", "c"), ("edge", "c", "d")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        ]
        res = evaluate(facts, rules)
        for fact in res.derived:
            seen = set()
            stack = [fact]
            while stack:
                f = stack.pop()
                self.assertNotIn(f, seen, "derivation DAG must be acyclic")
                seen.add(f)
                d = res.derivation(f)
                if d is None:
                    self.assertIn(f, facts)
                    continue
                rule = rules[d.rule_index]
                self.assertEqual(rule.head[0], f[0])
                pos_lits = [l for l in rule.body if l.positive]
                self.assertEqual(len(pos_lits), len(d.children))
                # Replay: match children to positive literals, derive head.
                env = {}
                for lit, child in zip(pos_lits, d.children):
                    self.assertEqual(lit.atom[0], child[0])
                    for a, v in zip(lit.atom[1:], child[1:]):
                        if isinstance(a, Var):
                            if a in env:
                                self.assertEqual(env[a], v)
                            else:
                                env[a] = v
                        else:
                            self.assertEqual(a, v)
                head = (rule.head[0],) + tuple(
                    env[a] if isinstance(a, Var) else a for a in rule.head[1:]
                )
                self.assertEqual(head, f)
                for lit in rule.body:
                    if not lit.positive:
                        g = (lit.atom[0],) + tuple(
                            env[a] if isinstance(a, Var) else a for a in lit.atom[1:]
                        )
                        self.assertNotIn(g, res.facts)
                stack.extend(d.children)

    def test_explain_renders_source_chain(self):
        facts = [("edge", "a", "b"), ("edge", "b", "c")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        ]
        res = evaluate(facts, rules)
        text = res.explain(("reach", "a", "c"))
        self.assertIn("reach('a', 'c')  <= rule 1", text)
        self.assertIn("reach('a', 'b')  <= rule 0", text)
        self.assertIn("edge('a', 'b')  [input fact]", text)
        self.assertIn("edge('b', 'c')  [input fact]", text)

    def test_input_fact_has_no_derivation(self):
        res = evaluate([("e", 1)], [])
        self.assertIsNone(res.derivation(("e", 1)))
        with self.assertRaises(KeyError):
            res.derivation(("e", 2))


class TestSafetyRejection(unittest.TestCase):
    def test_unsafe_head_variable(self):
        with self.assertRaises(UnsafeRuleError) as cm:
            evaluate([("e", 1)], [Rule(("p", X), [pos("e", Y)])])
        self.assertIn("rule 0", str(cm.exception))
        self.assertIn("'X'", str(cm.exception))

    def test_unsafe_negated_variable(self):
        with self.assertRaises(UnsafeRuleError) as cm:
            evaluate([("e", 1)], [Rule(("p", X), [pos("e", X), neg("q", Y)])])
        self.assertIn("body literal 1", str(cm.exception))

    def test_ground_head_empty_body_is_safe(self):
        res = evaluate([], [Rule(("p", 1), [])])
        self.assertTrue(res.complete)
        self.assertEqual(res.query("p", X), [("p", 1)])


class TestStratificationRejection(unittest.TestCase):
    def test_negative_self_cycle(self):
        rules = [Rule(("p", X), [pos("e", X), neg("p", X)])]
        with self.assertRaises(StratificationError) as cm:
            evaluate([("e", 1)], rules)
        self.assertIn("'p'", str(cm.exception))

    def test_indirect_negative_cycle(self):
        rules = [
            Rule(("p", X), [pos("q", X)]),
            Rule(("q", X), [pos("e", X), neg("r", X)]),
            Rule(("r", X), [pos("p", X)]),
        ]
        with self.assertRaises(StratificationError):
            evaluate([("e", 1)], rules)

    def test_positive_cycle_is_allowed(self):
        # Positive (non-negated) recursion must stratify and evaluate; the
        # least model of p(X) :- p(X), e(X) alone is empty (no base case),
        # and with a base rule p(X) :- e(X) it is {p(1)}.
        rules = [Rule(("p", X), [pos("p", X), pos("e", X)])]
        res = evaluate([("e", 1)], rules)
        self.assertTrue(res.complete)
        self.assertEqual(res.query("p", X), [])
        rules2 = [Rule(("p", X), [pos("e", X)])] + rules
        res2 = evaluate([("e", 1)], rules2)
        self.assertEqual(res2.query("p", X), [("p", 1)])


class TestValidation(unittest.TestCase):
    def test_nan_and_infinity_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError, msg=bad):
                evaluate([("e", bad)], [])
            with self.assertRaises(ValidationError, msg=bad):
                evaluate([], [Rule(("p", bad), [])])

    def test_bool_constant_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate([("e", True)], [])
        with self.assertRaises(ValidationError):
            evaluate([], [Rule(("p", False), [])])

    def test_bool_max_facts_rejected(self):
        with self.assertRaises(ValidationError):
            evaluate([("e", 1)], [], max_facts=True)

    def test_bad_max_facts(self):
        with self.assertRaises(ValidationError):
            evaluate([], [], max_facts=-1)
        with self.assertRaises(ValidationError):
            evaluate([], [], max_facts="10")

    def test_variable_in_fact_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            evaluate([("e", X)], [])
        self.assertIn("fact 0", str(cm.exception))

    def test_arity_mismatch_rejected(self):
        with self.assertRaises(ValidationError) as cm:
            evaluate([("e", 1)], [Rule(("p", X), [pos("e", X, Y)])])
        self.assertIn("arity", str(cm.exception))

    def test_limits(self):
        with self.assertRaises(LimitError):
            evaluate([("e", i) for i in range(2001)], [])
        with self.assertRaises(LimitError):
            evaluate([], [Rule(("p", 1), [])] * 101)
        facts = [(f"p{i}", 1) for i in range(21)]
        with self.assertRaises(LimitError):
            evaluate(facts, [])

    def test_finite_float_and_mixed_constants_ok(self):
        res = evaluate([("e", 1.5), ("e", "s"), ("e", 2)], [])
        self.assertTrue(res.complete)
        self.assertEqual(len(res.facts), 3)
        # Python numeric equality: 2 and 2.0 are the same constant.
        res2 = evaluate([("e", 2), ("e", 2.0)], [])
        self.assertEqual(len(res2.facts), 1)


class TestBudget(unittest.TestCase):
    def test_budget_overflow_returns_incomplete_without_partial_round(self):
        # Chain a->b->c->d: round 1 derives 3 reach facts at once.
        facts = [("edge", "a", "b"), ("edge", "b", "c"), ("edge", "c", "d")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        ]
        res = evaluate(facts, rules, max_facts=len(facts) + 1)
        self.assertFalse(res.complete)
        self.assertIsNotNone(res.reason)
        self.assertIn("budget", res.reason)
        # The overflowing round was discarded wholesale: zero derived facts.
        self.assertEqual(res.derived, frozenset())
        self.assertEqual(set(res.facts), set(facts))

    def test_budget_exactly_sufficient_is_complete(self):
        facts = [("edge", "a", "b"), ("edge", "b", "c")]
        rules = [
            Rule(("reach", X, Y), [pos("edge", X, Y)]),
            Rule(("reach", X, Z), [pos("reach", X, Y), pos("edge", Y, Z)]),
        ]
        full = evaluate(facts, rules)
        res = evaluate(facts, rules, max_facts=len(full.facts))
        self.assertTrue(res.complete)
        self.assertEqual(set(res.facts), set(full.facts))

    def test_input_alone_exceeding_budget(self):
        res = evaluate([("e", 1), ("e", 2)], [], max_facts=1)
        self.assertFalse(res.complete)
        self.assertIn("budget", res.reason)

    def test_budget_exhaustion_is_not_conflated_with_unsat(self):
        # A complete empty derivation (no rule fires) is complete=True;
        # a budget stop is complete=False.  Distinct outcomes.
        res_unsat = evaluate([("e", 1)], [Rule(("p", X), [pos("q", X)])])
        self.assertTrue(res_unsat.complete)
        self.assertEqual(res_unsat.query("p", X), [])


class TestRandomizedCrossCheck(unittest.TestCase):
    """Deterministic random stratified programs: engine vs naive reference."""

    def _random_program(self, seed):
        rng = random.Random(seed)
        consts = [0, 1, 2, "a", "b"]
        preds = [(f"p{i}", rng.randint(1, 2)) for i in range(rng.randint(2, 5))]
        facts = []
        for name, ar in preds:
            for _ in range(rng.randint(0, 4)):
                facts.append((name, *[rng.choice(consts) for _ in range(ar)]))
        rules = []
        for _ in range(rng.randint(1, 7)):
            hi = rng.randrange(len(preds))
            hname, har = preds[hi]
            body = []
            pos_vars = []
            for _ in range(rng.randint(1, 2)):
                bi = rng.randint(0, hi)  # positive deps: level <= head level
                bname, bar = preds[bi]
                args = []
                for _ in range(bar):
                    if rng.random() < 0.6:
                        v = Var(rng.choice("XYZW"))
                        args.append(v)
                        pos_vars.append(v)
                    else:
                        args.append(rng.choice(consts))
                body.append(pos(bname, *args))
            if hi > 0 and rng.random() < 0.5 and pos_vars:
                bi = rng.randint(0, hi - 1)  # negated deps: strictly lower
                bname, bar = preds[bi]
                args = [
                    rng.choice(pos_vars) if rng.random() < 0.7 else rng.choice(consts)
                    for _ in range(bar)
                ]
                body.append(neg(bname, *args))
            head_args = [
                rng.choice(pos_vars) if pos_vars and rng.random() < 0.7
                else rng.choice(consts)
                for _ in range(har)
            ]
            rules.append(Rule((hname, *head_args), body))
        return facts, rules

    def test_engine_matches_naive_on_random_programs(self):
        for seed in range(120):
            facts, rules = self._random_program(seed)
            res = evaluate(facts, rules)
            self.assertTrue(res.complete, f"seed {seed}: {res.reason}")
            self.assertEqual(
                set(res.facts),
                naive_evaluate(facts, rules),
                f"seed {seed} mismatch",
            )


if __name__ == "__main__":
    unittest.main()
