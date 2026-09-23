"""Incremental maintenance of a stratified Datalog least model.

This module adds a stateful :class:`Session` on top of the same data model
as :mod:`datalog.engine` (atoms, literals, rules, errors).  A session holds

* a **versioned rule set** (validated and stratified before publication;
  ``replace_rules`` swaps it atomically), and
* a **source-aware fact base**: every base fact carries a set of source
  ids; a fact is present iff at least one source supports it.

Update batches (``apply_batch``) are atomic: the whole batch is validated
before anything is applied, and if the fact budget would be exceeded the
entire batch is rolled back and reported as not applied — no partial,
unexplained state is ever left behind.

Maintenance strategy per batch:

* Predicates are partitioned into components (SCCs of the positive
  dependency graph), processed in topological (dependency) order.
* Components whose inputs did not change are **not touched at all**
  (zero rule firings — measurable via ``BatchReport.firings``).
* Purely positive components (no changed negated dependency) are
  maintained by DRed: over-delete everything reachable from the removed
  facts through the shared support DAG, then re-derive what still has
  support from the remaining facts.  Recursion therefore cannot keep
  itself alive through support counts — a pure cycle with no base
  support collapses.
* Components with a changed negated dependency are recomputed from
  scratch, in dependency order (stratified negation is non-monotonic,
  so incremental delta maintenance is not sound there).

Every derived fact keeps the full set of its currently valid immediate
derivations (the *support DAG*, shared between facts).  Public snapshots
(:meth:`Session.result`) expose, per derived fact, the single best
derivation — shortest (minimal rule-application depth), ties broken by
the lexicographically smallest ``(rule_index, children)`` — exactly the
same policy as :func:`datalog.evaluate`.
"""

from __future__ import annotations

from collections import defaultdict, namedtuple

from .engine import (
    DEFAULT_FACT_BUDGET,
    Derivation,
    Result,
    _check_fact,
    _check_safety,
    _fact_key,
    _join,
    _negated_ok,
    _stratify,
    _substitute,
    _validate_rules,
)
from .errors import LimitError, StratificationError, ValidationError
from .terms import Rule, check_constant

# --- Session interface limits (deliberately larger than evaluate()'s; ----
# --- see README "两版接口的差异" for the rationale) ----------------------

SESSION_MAX_PREDICATES = 100
SESSION_MAX_RULES = 500
SESSION_MAX_BASE_FACTS = 50_000

#: Source id used when a batch operation gives a bare fact without one.
DEFAULT_SOURCE = "default"


# --- Reports --------------------------------------------------------------

#: Why a fact ceased to hold.  ``cause`` is one of ``"source_removed"``
#: (the last source of a base fact was withdrawn; ``detail`` = the source
#: ids this batch removed), ``"support_lost"`` (a derived fact lost every
#: derivation), or ``"rules_replaced"`` (the fact is not in the least
#: model under the new rule set).  Every retraction is verifiable with
#: ``datalog.checker.verify_retractions``.
Retraction = namedtuple("Retraction", ["fact", "cause", "detail"])


class BatchReport:
    """Outcome of :meth:`Session.apply_batch`.

    Attributes:
        applied: ``False`` iff the batch was rejected (fact budget); the
            session state is then byte-identical to before the call.
        reason: human-readable cause when ``applied`` is False, else None.
        added / removed: frozensets of facts whose truth *changed* in this
            batch (net, base + derived).
        over_deleted: facts tentatively retracted by the DRed delete phase
            (the actual invalidation-propagation work set).
        rederived: subset of ``over_deleted`` that was re-derived from
            remaining support and therefore still holds.
        recomputed: frozenset of predicate names whose component was
            recomputed from scratch (changed negated dependency).
        firings: dict ``rule_index -> number of rule firings`` this batch
            (a firing = one complete positive-body match).  Components
            with no relevant change do not appear at all — this is how
            callers can verify that unrelated rules were not recomputed.
        retractions: tuple of :class:`Retraction` for every removed fact.
        rules_version: the rule-set version in effect after the batch.
    """

    __slots__ = (
        "applied", "reason", "added", "removed", "over_deleted",
        "rederived", "recomputed", "firings", "retractions",
        "rules_version",
    )

    def __init__(self, *, applied, reason=None, added=frozenset(),
                 removed=frozenset(), over_deleted=frozenset(),
                 rederived=frozenset(), recomputed=frozenset(),
                 firings=None, retractions=(), rules_version=0):
        self.applied = applied
        self.reason = reason
        self.added = frozenset(added)
        self.removed = frozenset(removed)
        self.over_deleted = frozenset(over_deleted)
        self.rederived = frozenset(rederived)
        self.recomputed = frozenset(recomputed)
        self.firings = dict(firings or {})
        self.retractions = tuple(retractions)
        self.rules_version = rules_version

    def __repr__(self):
        return (
            f"BatchReport(applied={self.applied}, added={len(self.added)}, "
            f"removed={len(self.removed)}, over_deleted={len(self.over_deleted)}, "
            f"rederived={len(self.rederived)}, recomputed={sorted(self.recomputed)})"
        )


class ReplaceReport:
    """Outcome of :meth:`Session.replace_rules`.

    Attributes:
        applied: False iff the new rule set was validated but publishing
            it would exceed the fact budget (old version kept).
        reason: cause when not applied, else None.
        added / removed: frozensets — the affected fact set (symmetric
            difference of the old and new least models).
        retractions: tuple of :class:`Retraction` (cause
            ``"rules_replaced"``) for every removed fact — verifiable with
            ``datalog.checker.verify_retractions`` against the new rules.
        rules_version: the version now in effect (old version + 1 when
            applied, else the unchanged current version).
    """

    __slots__ = ("applied", "reason", "added", "removed", "retractions",
                 "rules_version")

    def __init__(self, *, applied, reason=None, added=frozenset(),
                 removed=frozenset(), retractions=(), rules_version=0):
        self.applied = applied
        self.reason = reason
        self.added = frozenset(added)
        self.removed = frozenset(removed)
        self.retractions = tuple(retractions)
        self.rules_version = rules_version

    def __repr__(self):
        return (
            f"ReplaceReport(applied={self.applied}, added={len(self.added)}, "
            f"removed={len(self.removed)}, rules_version={self.rules_version})"
        )


# --- Components (SCCs of the positive predicate dependency graph) ---------

class _Component:
    __slots__ = ("cid", "preds", "rules", "recursive", "pos_deps",
                 "neg_deps", "relevant", "pos_relevant")

    def __init__(self, cid, preds):
        self.cid = cid
        self.preds = frozenset(preds)
        self.rules = []          # (rule_index, pos_lits, neg_lits, recursive_positions)
        self.recursive = False
        self.pos_deps = frozenset()   # predicates in positive body lits, outside this component
        self.neg_deps = frozenset()   # predicates in negated body lits (always lower components)
        self.relevant = frozenset()   # preds | pos_deps | neg_deps
        self.pos_relevant = frozenset()  # preds | pos_deps


def _build_components(predicates, rules):
    """Partition predicates into SCCs of the positive dependency graph and
    return them as a list of ``_Component`` in topological (dependency
    first) order.  Must only be called after ``_stratify`` succeeded."""
    adj = {p: set() for p in predicates}
    for rule in rules:
        h = rule.head[0]
        for lit in rule.body:
            if lit.positive:
                adj[lit.atom[0]].add(h)  # body pred -> head pred

    # Iterative Tarjan SCC.
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    sccs = []
    counter = [0]

    def strongconnect(root):
        work = [(root, iter(sorted(adj[root])))]
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index_of:
                    index_of[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(adj[w]))))
                    advanced = True
                    break
                elif w in on_stack:
                    low[v] = min(low[v], index_of[w])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
            if low[v] == index_of[v]:
                scc = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == v:
                        break
                sccs.append(scc)

    for p in sorted(predicates):
        if p not in index_of:
            strongconnect(p)

    pred_to_comp = {}
    comps = []
    for cid, scc in enumerate(sccs):
        comp = _Component(cid, scc)
        comps.append(comp)
        for p in scc:
            pred_to_comp[p] = cid

    # Fill rule/deps info.
    for ri, rule in enumerate(rules):
        h = rule.head[0]
        comp = comps[pred_to_comp[h]]
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        rec_positions = tuple(
            i for i, l in enumerate(pos_lits) if l.atom[0] in comp.preds
        )
        comp.rules.append((ri, rule, pos_lits, neg_lits, rec_positions))
    for comp in comps:
        pos_deps = set()
        neg_deps = set()
        self_loop = False
        for ri, rule, pos_lits, neg_lits, _rec in comp.rules:
            for l in pos_lits:
                p = l.atom[0]
                if p in comp.preds:
                    self_loop = True
                else:
                    pos_deps.add(p)
            for l in neg_lits:
                p = l.atom[0]
                if p in comp.preds:
                    # _stratify already rejected negative cycles; be defensive.
                    raise StratificationError(
                        f"negative dependency of predicate {p!r} on itself "
                        f"within one recursive component"
                    )
                neg_deps.add(p)
        comp.recursive = len(comp.preds) > 1 or self_loop
        comp.pos_deps = frozenset(pos_deps)
        comp.neg_deps = frozenset(neg_deps)
        comp.relevant = comp.preds | comp.pos_deps | comp.neg_deps
        comp.pos_relevant = comp.preds | comp.pos_deps

    # Topological order of the condensation (dependencies first), with a
    # deterministic tie-break on the smallest predicate name.
    order = []
    done = set()

    def deps_of(comp):
        d = {pred_to_comp[p] for p in comp.pos_deps | comp.neg_deps}
        d.discard(comp.cid)
        return d

    remaining = {c.cid: deps_of(c) for c in comps}
    by_name = {c.cid: min(c.preds) for c in comps}
    while remaining:
        ready = sorted(
            (cid for cid, deps in remaining.items() if deps <= done),
            key=lambda c: by_name[c],
        )
        if not ready:  # cannot happen: condensation is a DAG
            raise AssertionError("component graph has a cycle")
        for cid in ready:
            order.append(comps[cid])
            done.add(cid)
            del remaining[cid]
    return order


# --- Journaled mutations (for atomic batch rollback) ----------------------

def _dset_add(d, key, item, journal):
    """Add ``item`` to the set ``d[key]`` (creating it), journaled."""
    s = d.get(key)
    if s is None:
        d[key] = {item}
        journal.append(("dict_del_key", d, key))
        return True
    if item in s:
        return False
    s.add(item)
    journal.append(("set_discard", s, item))
    return True


def _dset_discard(d, key, item, journal):
    """Remove ``item`` from the set ``d[key]`` (dropping empty sets), journaled."""
    s = d.get(key)
    if s is None or item not in s:
        return False
    if len(s) == 1:
        del d[key]
        journal.append(("dict_set_key", d, key, {item}))
    else:
        s.discard(item)
        journal.append(("set_add", s, item))
    return True


def _set_add(s, item, journal):
    if item in s:
        return False
    s.add(item)
    journal.append(("set_discard", s, item))
    return True


def _set_discard(s, item, journal):
    if item not in s:
        return False
    s.discard(item)
    journal.append(("set_add", s, item))
    return True


def _rollback(journal):
    for entry in reversed(journal):
        op = entry[0]
        if op == "dict_del_key":
            entry[1].pop(entry[2], None)
        elif op == "dict_set_key":
            entry[1][entry[2]] = entry[3]
        elif op == "set_discard":
            entry[1].discard(entry[2])
        elif op == "set_add":
            entry[1].add(entry[2])
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unknown journal op {op!r}")


# --- Snapshot -------------------------------------------------------------

class SessionResult(Result):
    """Immutable snapshot of a :class:`Session`, returned by
    :meth:`Session.result`.  Behaves exactly like the :class:`Result` of
    :func:`datalog.evaluate` (same ``query`` / ``derivation`` / ``explain``
    / ``complete`` / ``facts`` / ``derived``), and additionally carries:

    * ``rules`` — the rule list of this snapshot's version (tuple),
    * ``rules_version`` — the version number,
    * ``max_facts`` — the session's fact budget,
    * ``sources(fact)`` — frozenset of source ids currently supporting a
      base fact (empty frozenset for derived facts; ``KeyError`` for facts
      not in the snapshot),
    * ``base_sources`` — a ``{base_fact: frozenset(sources)}`` mapping.

    A session snapshot is always a complete least model: batches that
    would exceed the budget are rejected instead, so ``complete`` is True.
    Snapshots are deep enough to be immutable: later batches and rule
    replacements never change a previously returned snapshot, and old
    snapshots never reference sources or rules of a newer version.
    """

    def __init__(self, *, facts, input_facts, derivations, rules,
                 sources, rules_version, max_facts):
        super().__init__(
            complete=True,
            reason=None,
            facts=facts,
            input_facts=input_facts,
            derivations=derivations,
            rules=rules,
        )
        self.rules = tuple(rules)
        self.rules_version = rules_version
        self.max_facts = max_facts
        self._sources = dict(sources)

    @property
    def base_sources(self):
        """A copy of the ``{base_fact: frozenset(source_ids)}`` mapping."""
        return dict(self._sources)

    def sources(self, fact):
        """Source ids supporting a base fact (empty for derived facts)."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")
        return self._sources.get(fact, frozenset())


# --- Session ---------------------------------------------------------------

class Session:
    """A persistent, incrementally maintained stratified Datalog program.

    Args:
        rules: iterable of :class:`Rule` (validated and stratified up
            front; invalid programs raise before any state exists).
        facts: optional iterable of initial base facts.  Each entry is
            either a bare fact tuple (assigned ``DEFAULT_SOURCE``) or a
            ``(fact, source_id)`` pair.
        max_facts: total-fact budget (base + derived), int >= 0 (bool
            rejected).  A batch or rule replacement that would exceed it
            is rejected wholesale and reported, never applied partially.

    Source ids must be non-empty ``str`` or non-bool ``int``.
    """

    def __init__(self, rules, facts=(), *, max_facts=DEFAULT_FACT_BUDGET):
        if isinstance(max_facts, bool) or not isinstance(max_facts, int):
            raise ValidationError(
                f"max_facts must be an int (bool rejected), got {max_facts!r}"
            )
        if max_facts < 0:
            raise ValidationError(f"max_facts must be >= 0, got {max_facts}")
        self._max_facts = max_facts

        norm_rules = _validate_rules(rules, max_rules=SESSION_MAX_RULES)
        _check_safety(norm_rules)

        self._edb = {}            # base fact -> set(source_id)
        self._store = {}          # pred -> set(fact)  (base + derived)
        self._derived = set()     # derived facts currently not base
        self._support = {}        # fact -> set((rule_index, children))
        self._consequences = {}   # child fact -> set((dependent_fact, derivation))
        self._arity = {}          # predicate -> arity
        self._journal = []

        # Arity from rules first, then initial facts (which may declare
        # additional base-only predicates).
        for ri, rule in enumerate(norm_rules):
            self._note_arity(rule.head[0], len(rule.head) - 1, f"rule {ri}, head")
            for bi, lit in enumerate(rule.body):
                self._note_arity(
                    lit.atom[0], len(lit.atom) - 1, f"rule {ri}, body literal {bi}"
                )
        init_ops = self._normalize_ops(facts, "facts")
        for fact, _src in init_ops:
            self._note_arity(fact[0], len(fact) - 1, "initial facts")
        if len(self._arity) > SESSION_MAX_PREDICATES:
            raise LimitError(
                f"too many distinct predicates: {len(self._arity)} > "
                f"limit {SESSION_MAX_PREDICATES}"
            )
        _stratify(self._arity.keys(), norm_rules)

        self._rules = norm_rules
        self._rules_version = 0
        self._components = _build_components(self._arity.keys(), norm_rules)

        for fact, src in init_ops:
            _dset_add(self._edb, fact, src, self._journal)
        if len(self._edb) > SESSION_MAX_BASE_FACTS:
            raise LimitError(
                f"too many distinct base facts: {len(self._edb)} > "
                f"limit {SESSION_MAX_BASE_FACTS}"
            )
        for fact in self._edb:
            self._store.setdefault(fact[0], set()).add(fact)
        self._journal = []

        firings = {}
        for comp in self._components:
            if comp.rules:
                self._eval_component_full(comp, firings)
        self._journal = []

        if len(self._edb) + len(self._derived) > max_facts:
            raise LimitError(
                f"fact budget exceeded by initial load: "
                f"{len(self._edb) + len(self._derived)} facts > budget {max_facts}"
            )

    # -- small helpers ----------------------------------------------------

    @property
    def rules_version(self):
        return self._rules_version

    @property
    def rules(self):
        return tuple(self._rules)

    @property
    def max_facts(self):
        return self._max_facts

    def _note_arity(self, pred, n, where):
        prev = self._arity.get(pred)
        if prev is not None and prev != n:
            raise ValidationError(
                f"{where}: predicate {pred!r} used with arity {n}, but "
                f"already declared with arity {prev} (fixed arity)"
            )
        self._arity[pred] = n

    def _in_store(self, fact):
        return fact in self._store.get(fact[0], ())

    def _all_facts(self):
        return {f for fs in self._store.values() for f in fs}

    def _total(self):
        return len(self._edb) + len(self._derived)

    # -- input normalization ------------------------------------------------

    @staticmethod
    def _check_source(src, where):
        if isinstance(src, bool) or not isinstance(src, (int, str)):
            raise ValidationError(
                f"{where}: source id must be a non-empty str or an int "
                f"(bool rejected), got {src!r}"
            )
        if isinstance(src, str) and not src:
            raise ValidationError(f"{where}: source id must be a non-empty str")
        return src

    def _normalize_ops(self, ops, kind):
        """Normalize a batch operation iterable to ``[(fact, source), ...]``.

        Each entry is either a bare fact tuple (default source) or a
        ``(fact, source)`` pair.  Raises ``ValidationError`` with a
        located message; nothing is mutated.
        """
        if isinstance(ops, (str, bytes)) or not hasattr(ops, "__iter__"):
            raise ValidationError(
                f"{kind} must be an iterable of facts or (fact, source) "
                f"pairs, got {type(ops).__name__}"
            )
        out = []
        for i, op in enumerate(ops):
            where = f"{kind} entry {i}"
            if (
                isinstance(op, (tuple, list))
                and len(op) == 2
                and isinstance(op[0], (tuple, list))
            ):
                fact, src = op
            else:
                fact, src = op, DEFAULT_SOURCE
            fact = _check_fact(fact, where)
            src = self._check_source(src, where)
            out.append((fact, src))
        return out

    def _check_batch_arity(self, ops):
        """Check batch facts against declared arities; return new predicates."""
        new_preds = {}
        for fact, _src in ops:
            pred, n = fact[0], len(fact) - 1
            if pred in self._arity:
                if self._arity[pred] != n:
                    raise ValidationError(
                        f"batch fact {fact!r}: predicate {pred!r} used with "
                        f"arity {n}, but declared with arity {self._arity[pred]}"
                    )
            elif pred in new_preds:
                if new_preds[pred] != n:
                    raise ValidationError(
                        f"batch fact {fact!r}: predicate {pred!r} used with "
                        f"inconsistent arity"
                    )
            else:
                new_preds[pred] = n
        if len(self._arity) + len(new_preds) > SESSION_MAX_PREDICATES:
            raise LimitError(
                f"too many distinct predicates: "
                f"{len(self._arity) + len(new_preds)} > limit "
                f"{SESSION_MAX_PREDICATES}"
            )
        return new_preds

    # -- journaled state mutations -------------------------------------------

    def _store_add(self, fact):
        _dset_add(self._store, fact[0], fact, self._journal)

    def _store_discard(self, fact):
        _dset_discard(self._store, fact[0], fact, self._journal)

    def _derived_add(self, fact):
        _set_add(self._derived, fact, self._journal)

    def _derived_discard(self, fact):
        _set_discard(self._derived, fact, self._journal)

    def _record_support(self, head, rule_index, children):
        deriv = (rule_index, children)
        if _dset_add(self._support, head, deriv, self._journal):
            for c in children:
                _dset_add(self._consequences, c, (head, deriv), self._journal)

    def _support_remove(self, fact):
        """Remove all support entries of ``fact`` and their consequence
        edges (journaled).  Returns the removed derivations."""
        derivs = self._support.get(fact)
        if not derivs:
            return ()
        derivs = frozenset(derivs)
        for deriv in derivs:
            _dset_discard(self._support, fact, deriv, self._journal)
            for c in deriv[1]:
                _dset_discard(self._consequences, c, (fact, deriv), self._journal)
        return derivs

    def _support_restore(self, fact, derivs):
        for deriv in derivs:
            self._record_support(fact, deriv[0], deriv[1])

    def _remove_derived_fact(self, fact):
        """Fully remove a derived fact: store, derived set, support and
        consequence edges (journaled)."""
        self._support_remove(fact)
        self._store_discard(fact)
        self._derived_discard(fact)

    # -- evaluation -----------------------------------------------------------

    def _fire_rules(self, comp, delta, firings, round_no, full):
        """One semi-naive round.  Returns the set of new head facts."""
        round_new = set()
        for ri, rule, pos_lits, neg_lits, rec_positions in comp.rules:
            if full:
                if not rec_positions:
                    if round_no > 0:
                        continue
                    distinguished = (None,)
                else:
                    distinguished = rec_positions
            else:
                distinguished = tuple(
                    i for i, l in enumerate(pos_lits) if delta.get(l.atom[0])
                )
                if not distinguished:
                    continue
            for d in distinguished:
                for env, children in _join(pos_lits, self._store, delta, d):
                    firings[ri] = firings.get(ri, 0) + 1
                    if neg_lits and not _negated_ok(neg_lits, env, self._store):
                        continue
                    head = _substitute(rule.head, env)
                    self._record_support(head, ri, children)
                    if head not in self._store.get(head[0], ()) and head not in round_new:
                        round_new.add(head)
        return round_new

    def _eval_component_full(self, comp, firings):
        """From-scratch semi-naive least-model computation of one component
        over the current store, recording every firing as support."""
        delta = {p: set(self._store.get(p, ())) for p in comp.preds}
        round_no = 0
        while True:
            if round_no > 0 and not any(delta[p] for p in comp.preds):
                return
            round_new = self._fire_rules(comp, delta, firings, round_no, full=True)
            if not round_new:
                return
            for f in round_new:
                self._store_add(f)
                self._derived_add(f)
            delta = {p: set() for p in comp.preds}
            for f in round_new:
                delta[f[0]].add(f)
            round_no += 1

    def _eval_component_add(self, comp, seed_added, firings):
        """Semi-naive addition: only derivations involving at least one
        seed (or newly derived) fact are fired.  Returns new facts."""
        delta = {}
        for f in seed_added:
            delta.setdefault(f[0], set()).add(f)
        new_total = set()
        while any(delta.values()):
            round_new = self._fire_rules(comp, delta, firings, 0, full=False)
            if not round_new:
                break
            for f in round_new:
                self._store_add(f)
                self._derived_add(f)
            new_total |= round_new
            delta = {}
            for f in round_new:
                delta.setdefault(f[0], set()).add(f)
        return new_total

    # -- DRed: delete / re-derive ---------------------------------------------

    def _dred(self, comp, seed_removed, extra_tentative):
        """Over-delete everything in ``comp`` reachable from
        ``seed_removed`` (facts already out of the store) plus
        ``extra_tentative`` (facts whose base support vanished), then
        re-derive what still has support.  Returns ``(tentative,
        rederived)``.

        The closure is restricted to this component's own predicates:
        consequence edges only point upward in the topological component
        order, so dependent facts in higher components are reached when
        those components run their own delete phase with the updated
        global ``removed`` set.  This keeps each component's work set
        attributable and never re-derives facts of a component whose
        negated dependencies changed (those are recomputed wholesale).
        """
        tentative = set(extra_tentative)
        stack = [f for f in seed_removed if f[0] in comp.pos_relevant]
        stack += list(extra_tentative)
        while stack:
            f = stack.pop()
            for g, _deriv in self._consequences.get(f, ()):
                if g[0] in comp.preds and g not in tentative:
                    tentative.add(g)
                    stack.append(g)
        if not tentative:
            return set(), set()

        # A tentative fact that is still a base fact (it has live sources)
        # never leaves the store; only its recorded derivations are rebuilt.
        still_base = {f for f in tentative if f in self._edb}

        old_support = {}
        for f in tentative:
            old_support[f] = self._support_remove(f)
            if f not in still_base:
                self._store_discard(f)
                self._derived_discard(f)

        # Re-derive: a tentative fact is confirmed as soon as one of its
        # recorded derivations has all children back in the store.
        waiting = defaultdict(list)   # missing child -> [(fact, derivation)]
        pending = {}                  # (fact, derivation) -> missing count
        queue = []
        for f in tentative:
            for deriv in old_support[f]:
                missing = [c for c in deriv[1] if not self._in_store(c)]
                if not missing:
                    queue.append(f)
                    break
                pending[(f, deriv)] = len(missing)
                for c in missing:
                    waiting[c].append((f, deriv))
        confirmed = set(still_base)  # base facts are trivially still true
        while queue:
            f = queue.pop()
            if f in confirmed:
                continue
            confirmed.add(f)
            for g, deriv in waiting.get(f, ()):
                pending[(g, deriv)] -= 1
                if pending[(g, deriv)] == 0:
                    queue.append(g)

        for f in confirmed:
            valid = {
                d for d in old_support[f]
                if all(self._in_store(c) or c in confirmed for c in d[1])
            }
            self._support_restore(f, valid)
            if f not in still_base:
                self._store_add(f)
                self._derived_add(f)
        return tentative - still_base, confirmed - still_base

    # -- public API -----------------------------------------------------------

    def apply_batch(self, adds=(), removes=()):
        """Apply one atomic batch of fact additions/removals.

        ``adds`` / ``removes`` are iterables of facts or ``(fact,
        source_id)`` pairs.  Removing a source that is not present is a
        no-op; a base fact disappears only when its last source is
        removed.  Returns a :class:`BatchReport`.

        Raises ``ValidationError`` (located) for malformed input — before
        anything is mutated.  If the result would exceed the fact budget,
        the batch is rolled back entirely and ``report.applied`` is False.
        """
        add_ops = self._normalize_ops(adds, "adds")
        remove_ops = self._normalize_ops(removes, "removes")
        new_preds = self._check_batch_arity(add_ops + remove_ops)

        journal = []
        self._journal = journal
        firings = {}
        added, removed = set(), set()
        over_deleted_all, rederived_all = set(), set()
        recomputed = set()
        pending_tentative = defaultdict(set)  # component id -> facts

        try:
            for pred, n in new_preds.items():
                self._arity[pred] = n
                journal.append(("dict_del_key", self._arity, pred))

            touched = {f for f, _s in add_ops} | {f for f, _s in remove_ops}
            was_present = {f for f in touched if f in self._edb}
            for fact, src in remove_ops:
                _dset_discard(self._edb, fact, src, journal)
            for fact, src in add_ops:
                _dset_add(self._edb, fact, src, journal)

            if len(self._edb) > SESSION_MAX_BASE_FACTS:
                _rollback(journal)
                self._journal = []
                return BatchReport(
                    applied=False,
                    reason=f"base fact limit exceeded: more than "
                    f"{SESSION_MAX_BASE_FACTS} distinct base facts",
                    rules_version=self._rules_version,
                )

            pred_to_comp = {}
            for comp in self._components:
                for p in comp.preds:
                    pred_to_comp[p] = comp.cid

            for f in touched:
                now = f in self._edb
                was = f in was_present
                if now and not was:
                    if self._in_store(f):
                        # Was derived, now also base: keep it, keep support.
                        self._derived_discard(f)
                    else:
                        self._store_add(f)
                        added.add(f)
                elif was and not now:
                    if self._support.get(f):
                        # Base support gone but derivations recorded: DRed
                        # decides whether it survives as a derived fact.
                        pending_tentative[pred_to_comp[f[0]]].add(f)
                        self._store_discard(f)
                    else:
                        self._store_discard(f)
                        removed.add(f)

            for comp in self._components:
                if not comp.rules:
                    continue
                changed_preds = {f[0] for f in added} | {f[0] for f in removed}
                if not (changed_preds & comp.relevant) and comp.cid not in pending_tentative:
                    continue
                if changed_preds & comp.neg_deps:
                    # Negated dependency changed: non-monotonic — recompute
                    # the whole component from scratch.
                    pend = pending_tentative.pop(comp.cid, set())
                    old = {f for f in self._derived if f[0] in comp.preds} | pend
                    for f in pend:
                        self._support_remove(f)
                    for f in set(self._derived):
                        if f[0] in comp.preds:
                            self._remove_derived_fact(f)
                    # Base facts of this component keep their sources but
                    # their recorded derivations are rebuilt from scratch.
                    for f in list(self._edb):
                        if f[0] in comp.preds and self._support.get(f):
                            self._support_remove(f)
                    self._eval_component_full(comp, firings)
                    new = {f for f in self._derived if f[0] in comp.preds}
                    removed |= old - new
                    added |= new - old
                    recomputed |= comp.preds
                else:
                    extra = pending_tentative.pop(comp.cid, set())
                    seed_removed = {
                        f for f in removed if f[0] in comp.pos_relevant
                    }
                    if seed_removed or extra:
                        tentative, rederived = self._dred(
                            comp, seed_removed, extra
                        )
                        over_deleted_all |= tentative
                        rederived_all |= rederived
                        removed |= tentative - rederived
                    seed_added = {
                        f for f in added if f[0] in comp.pos_relevant
                    }
                    if seed_added:
                        added |= self._eval_component_add(comp, seed_added, firings)

            # A fact retracted and re-derived within the same batch has no
            # net truth change; keep it only in the work sets.
            both = added & removed
            added -= both
            removed -= both

            if self._total() > self._max_facts:
                overflow_total = self._total()
                _rollback(journal)
                self._journal = []
                return BatchReport(
                    applied=False,
                    reason=f"fact budget exceeded: batch would reach "
                    f"{overflow_total} facts > budget {self._max_facts}; "
                    f"the whole batch was rolled back",
                    rules_version=self._rules_version,
                )

            retractions = []
            for f in sorted(removed, key=_fact_key):
                cause, detail = self._retraction_cause(f, remove_ops)
                retractions.append(Retraction(f, cause, detail))
            self._journal = []
            return BatchReport(
                applied=True,
                added=added,
                removed=removed,
                over_deleted=over_deleted_all,
                rederived=rederived_all,
                recomputed=recomputed,
                firings=firings,
                retractions=retractions,
                rules_version=self._rules_version,
            )
        except BaseException:
            _rollback(journal)
            self._journal = []
            raise

    def _retraction_cause(self, fact, remove_ops):
        removed_sources = frozenset(
            s for f, s in remove_ops if f == fact
        )
        if removed_sources:
            return "source_removed", removed_sources
        return "support_lost", None

    def replace_rules(self, new_rules):
        """Atomically replace the entire rule set.

        The new rule set is fully validated (types, limits, safety,
        stratification) *before* anything is published; any violation
        raises the usual exception and the current version stays in
        effect.  On success the whole model is recomputed and a
        :class:`ReplaceReport` with the affected fact set and verifiable
        retraction reasons is returned.  If the new model would exceed
        the fact budget, the old version is kept (``applied=False``).

        Previously returned snapshots are unaffected: they keep the old
        version's rules, facts, derivations and sources.
        """
        norm_rules = _validate_rules(new_rules, max_rules=SESSION_MAX_RULES)
        _check_safety(norm_rules)
        # Arity: rule predicates must agree with existing fact predicates;
        # rule-only predicates may be introduced within the limit.
        arity = {}
        for fact in self._edb:
            arity[fact[0]] = len(fact) - 1
        for ri, rule in enumerate(norm_rules):
            for pred, n, where in [
                (rule.head[0], len(rule.head) - 1, f"rule {ri}, head")
            ] + [
                (lit.atom[0], len(lit.atom) - 1, f"rule {ri}, body literal {bi}")
                for bi, lit in enumerate(rule.body)
            ]:
                if pred in arity and arity[pred] != n:
                    raise ValidationError(
                        f"{where}: predicate {pred!r} used with arity {n}, "
                        f"but declared with arity {arity[pred]} (fixed arity)"
                    )
                arity[pred] = n
        if len(arity) > SESSION_MAX_PREDICATES:
            raise LimitError(
                f"too many distinct predicates: {len(arity)} > limit "
                f"{SESSION_MAX_PREDICATES}"
            )
        _stratify(arity.keys(), norm_rules)

        old_facts = self._all_facts()
        saved = (
            self._rules, self._components, self._store, self._derived,
            self._support, self._consequences, self._arity,
        )

        self._rules = norm_rules
        self._arity = arity
        self._components = _build_components(arity.keys(), norm_rules)
        self._store = {}
        for fact in self._edb:
            self._store.setdefault(fact[0], set()).add(fact)
        self._derived = set()
        self._support = {}
        self._consequences = {}
        self._journal = []

        firings = {}
        for comp in self._components:
            if comp.rules:
                self._eval_component_full(comp, firings)

        if self._total() > self._max_facts:
            overflow_total = self._total()
            (self._rules, self._components, self._store, self._derived,
             self._support, self._consequences, self._arity) = saved
            self._journal = []
            return ReplaceReport(
                applied=False,
                reason=f"fact budget exceeded: new rule set would reach "
                f"{overflow_total} facts > budget {self._max_facts}; "
                f"the previous rule set (version {self._rules_version}) "
                f"stays in effect",
                rules_version=self._rules_version,
            )

        self._journal = []
        new_facts = self._all_facts()
        added = new_facts - old_facts
        removed = old_facts - new_facts
        retractions = tuple(
            Retraction(f, "rules_replaced", None)
            for f in sorted(removed, key=_fact_key)
        )
        self._rules_version += 1
        return ReplaceReport(
            applied=True,
            added=added,
            removed=removed,
            retractions=retractions,
            rules_version=self._rules_version,
        )

    def result(self):
        """Return an immutable :class:`SessionResult` snapshot of the
        current least model (see :class:`SessionResult`)."""
        derivations = self._best_derivations()
        return SessionResult(
            facts=self._all_facts(),
            input_facts=set(self._edb),
            derivations=derivations,
            rules=self._rules,
            sources={f: frozenset(s) for f, s in self._edb.items()},
            rules_version=self._rules_version,
            max_facts=self._max_facts,
        )

    # -- best-derivation view over the support DAG -----------------------------

    def _best_derivations(self):
        """For every derived fact pick the shortest well-founded
        derivation (minimal rule-application depth; ties broken by the
        lexicographically smallest ``(rule_index, children)``) — the same
        policy as :func:`datalog.evaluate`.  Computed over the shared
        support DAG with a Dijkstra-style pass, so cycles in the support
        graph (positive recursion) cannot make a fact its own ancestor.
        """
        import heapq

        depth = {f: 0 for f in self._edb}
        best_cand = {}
        heap = []
        pending = {}
        waiting = defaultdict(list)
        for f, derivs in self._support.items():
            if f in self._edb:
                continue
            for deriv in derivs:
                children = deriv[1]
                rem = sum(1 for c in children if c not in depth)
                if rem == 0:
                    cand = 1 + max((depth[c] for c in children), default=0)
                    if cand < best_cand.get(f, 1 << 62):
                        best_cand[f] = cand
                        heapq.heappush(heap, (cand, _fact_key(f), f))
                else:
                    pending[(f, deriv)] = rem
                    for c in children:
                        if c not in depth:
                            waiting[c].append((f, deriv))
        while heap:
            d, _key, f = heapq.heappop(heap)
            if f in depth or d != best_cand.get(f):
                continue
            depth[f] = d
            for g, deriv in waiting.get(f, ()):
                pending[(g, deriv)] -= 1
                if pending[(g, deriv)] == 0:
                    children = deriv[1]
                    cand = 1 + max((depth[c] for c in children), default=0)
                    if cand < best_cand.get(g, 1 << 62):
                        best_cand[g] = cand
                        heapq.heappush(heap, (cand, _fact_key(g), g))

        derivations = {}
        for f, derivs in self._support.items():
            if f in self._edb:
                continue
            df = depth.get(f)
            if df is None:
                continue  # only cyclic support left: cannot happen post-DRed
            winner = None
            for deriv in derivs:
                ri, children = deriv
                if any(c not in depth for c in children):
                    continue
                cand = 1 + max((depth[c] for c in children), default=0)
                if cand != df:
                    continue
                key = (ri, tuple(_fact_key(c) for c in children))
                if winner is None or key < winner[0]:
                    winner = (key, deriv)
            if winner is not None:
                derivations[f] = Derivation(winner[1][0], winner[1][1])
        return derivations
