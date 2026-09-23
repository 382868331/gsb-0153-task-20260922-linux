"""Incremental maintenance for stratified Datalog with multi-source base facts.

This module adds a *stateful* layer on top of the same data model as
:mod:`datalog.engine`.  The one-shot :func:`datalog.evaluate` entry point is
unchanged; everything here is new, additive API.

Semantics (fixed, deterministic):

* **Multi-source base facts.**  Every base fact carries a set of source ids
  (``str`` or ``int``).  A base fact is *live* iff at least one source still
  supports it.  Deleting one source never removes a fact (or anything derived
  from it) while other sources remain.
* **Atomic batches.**  ``Session.apply_batch(add=..., remove=...)`` applies
  all removals (in entry order) and then all additions as one atomic step.
  A batch that fails validation or exhausts the fact budget leaves the
  session completely untouched (rolled back) and reports ``complete=False``
  with a reason — never a partial, unexplained change.
* **Positive recursive strata** are maintained with a DRed-style
  delete/rederive pass over the recorded derivation-node DAG: over-delete
  everything downstream of removed facts, then re-derive exactly the facts
  that still have support from live facts.  Recursion can never support
  itself: a fact survives only if a derivation node exists whose children
  are all outside the deleted set, bottoming out at live base facts.
* **Negation strata** (strata whose rules contain negated literals) are
  recomputed from scratch, in stratum order, but only when some predicate
  they depend on actually changed.
* **Rule-set replacement** validates the new rules (types, limits, safety,
  stratification) *before* publishing anything; on success the whole derived
  state is rebuilt under the new rules and the rule version is bumped.
  Snapshots taken earlier keep querying the old version; their evidence can
  never reference sources introduced later, because each snapshot freezes
  its own source map.
* **Evidence / retraction checkers do not trust solver flags.**  Evidence is
  verified structurally from the recorded derivation DAG down to live base
  facts; retractions are verified by an independent from-scratch replay.

Work sets: every report lists, per affected stratum, the facts actually
retracted (``deleted``), re-derived (``rederived``) and newly inserted
(``inserted``), plus the rule indices that fired — so untouched rules and
unrelated subgraphs are provably absent from the work sets.
"""

from __future__ import annotations

import heapq
from collections import namedtuple
from types import MappingProxyType

from .engine import (
    Derivation,
    _check_predicates_and_arity,
    _check_safety,
    _fact_key,
    _fmt_fact,
    _negated_ok,
    _render_explain,
    _stratify,
    _substitute,
    _validate_rules,
)
from .errors import LimitError, ValidationError
from .terms import Var, atom_vars, check_constant, check_predicate

# --- Declared session limits (independent of the one-shot evaluate limits) --

SESSION_MAX_PREDICATES = 20
SESSION_MAX_RULES = 100
SESSION_MAX_BASE_FACTS = 20_000
SESSION_DEFAULT_FACT_BUDGET = 1_000_000

#: Source id used when a batch entry is a bare fact without an explicit source.
DEFAULT_SOURCE = "default"


class _BudgetAbort(Exception):
    """Internal: the fact budget would be exceeded; abort the whole batch."""


# --- Reports ---------------------------------------------------------------

#: Per-stratum work set.  ``deleted`` = facts actually retracted in this
#: stratum, ``rederived`` = facts that were over-deleted and then recovered,
#: ``inserted`` = newly derived facts, ``rules_fired`` = rule indices that
#: produced at least one candidate, ``recomputed`` = the stratum contains
#: negation and was rebuilt wholesale.
StratumReport = namedtuple(
    "StratumReport",
    [
        "stratum",
        "predicates",
        "kind",
        "recomputed",
        "deleted",
        "rederived",
        "inserted",
        "rules_fired",
    ],
)


class UpdateReport:
    """Outcome of one atomic batch (or rule replacement).

    Attributes:
        batch_id: id of the committed batch (unchanged when rejected).
        rule_version: rule version after the batch.
        rules_replaced: whether this batch replaced the rule set.
        complete: ``False`` iff the batch was rejected (budget); the session
            is then byte-identical to before the call.
        reason: human-readable cause when ``complete`` is ``False``.
        added_facts / removed_facts: *net* fact changes vs. before the batch.
        affected_facts: ``added_facts | removed_facts``.
        retraction_reasons: ``{fact: reason_tuple}`` for every removed fact;
            each reason is verifiable with :meth:`Session.verify_retraction`.
        strata: per-stratum work sets (only affected strata are listed).
        ignored_removals: ``(fact, source)`` pairs that named a source not
            currently supporting the fact (documented no-op, not an error).
    """

    __slots__ = (
        "batch_id",
        "rule_version",
        "rules_replaced",
        "complete",
        "reason",
        "added_facts",
        "removed_facts",
        "retraction_reasons",
        "strata",
        "ignored_removals",
    )

    def __init__(
        self,
        *,
        batch_id,
        rule_version,
        rules_replaced,
        complete,
        reason,
        added_facts=frozenset(),
        removed_facts=frozenset(),
        retraction_reasons=None,
        strata=(),
        ignored_removals=(),
    ):
        self.batch_id = batch_id
        self.rule_version = rule_version
        self.rules_replaced = rules_replaced
        self.complete = complete
        self.reason = reason
        self.added_facts = frozenset(added_facts)
        self.removed_facts = frozenset(removed_facts)
        self.retraction_reasons = dict(retraction_reasons or {})
        self.strata = tuple(strata)
        self.ignored_removals = tuple(ignored_removals)

    @property
    def affected_facts(self):
        return self.added_facts | self.removed_facts

    def __repr__(self):
        return (
            f"UpdateReport(batch_id={self.batch_id}, rule_version={self.rule_version}, "
            f"complete={self.complete}, added={len(self.added_facts)}, "
            f"removed={len(self.removed_facts)}, reason={self.reason!r})"
        )


# --- Input parsing ----------------------------------------------------------

def _check_source(src, where):
    if isinstance(src, bool) or not isinstance(src, (str, int)):
        raise ValidationError(
            f"{where}: source id must be a str or int (bool rejected), got {src!r}"
        )


def _parse_change(entry, where):
    """Normalise one batch entry to ``(fact, source)``.

    Accepted forms: ``((pred, const...), source)`` — a 2-tuple whose first
    element is itself a tuple/list — or a bare fact tuple, which gets
    :data:`DEFAULT_SOURCE`.  Anything else raises a located ValidationError.
    """
    if (
        isinstance(entry, (tuple, list))
        and len(entry) == 2
        and isinstance(entry[0], (tuple, list))
    ):
        fact, source = tuple(entry[0]), entry[1]
    else:
        fact, source = entry, DEFAULT_SOURCE
    if isinstance(fact, list):
        fact = tuple(fact)
    if not isinstance(fact, tuple) or not fact:
        raise ValidationError(f"{where}: fact must be a non-empty tuple, got {fact!r}")
    check_predicate(fact[0], where)
    for j, arg in enumerate(fact[1:]):
        if isinstance(arg, Var):
            raise ValidationError(
                f"{where}: facts must be ground, found variable {arg!r}"
            )
        check_constant(arg, f"{where}, argument {j}")
    _check_source(source, where)
    return fact, source


# --- Strata -----------------------------------------------------------------

class _Stratum:
    __slots__ = ("index", "preds", "rules", "has_negation", "ref_preds")

    def __init__(self, index, preds, rules, has_negation, ref_preds):
        self.index = index
        self.preds = preds
        self.rules = rules
        self.has_negation = has_negation
        self.ref_preds = ref_preds

    @property
    def all_preds(self):
        return self.preds | self.ref_preds


def _build_strata(rules):
    preds = set()
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = _stratify(preds, rules)
    by_level = {}
    for ri, r in enumerate(rules):
        by_level.setdefault(stratum[r.head[0]], []).append((ri, r))
    out = []
    for level in sorted(by_level):
        rs = by_level[level]
        spreds = {r.head[0] for _, r in rs}
        refs = set()
        has_neg = False
        for _, r in rs:
            for lit in r.body:
                refs.add(lit.atom[0])
                if not lit.positive:
                    has_neg = True
        out.append(_Stratum(level, spreds, tuple(rs), has_neg, refs))
    return out


# --- Indexed join -----------------------------------------------------------

_UNBOUND = object()


def _xjoin(session, pos_lits, delta, distinguished):
    """Like ``engine._join`` but uses the session's per-argument indexes.

    ``children[i]`` is the fact matched to ``pos_lits[i]`` (body order); the
    literal at index ``distinguished`` (if not ``None``) is restricted to its
    predicate's delta set.
    """
    store = session._store
    index = session._index
    children = [None] * len(pos_lits)
    env = {}
    # Evaluate the delta-restricted (distinguished) literal first, then the
    # rest in body order; ``children`` is still filled in body order, so the
    # recorded derivations are identical — only the search order changes.
    if distinguished is None or not pos_lits:
        order = list(range(len(pos_lits)))
    else:
        order = [distinguished] + [i for i in range(len(pos_lits)) if i != distinguished]

    def candidates(i):
        atom = pos_lits[i].atom
        pred = atom[0]
        if i == distinguished:
            return delta.get(pred, ())
        best = None
        pidx = index.get(pred)
        if pidx is not None:
            for j, a in enumerate(atom[1:]):
                if isinstance(a, Var):
                    v = env.get(a, _UNBOUND)
                    if v is _UNBOUND:
                        continue
                else:
                    v = a
                bucket = pidx[j].get(v)
                if bucket is None:
                    return ()  # bound value matches nothing
                if best is None or len(bucket) < len(best):
                    best = bucket
        if best is not None:
            return best
        return store.get(pred, ())

    def backtrack(k):
        if k == len(order):
            yield dict(env), tuple(children)
            return
        i = order[k]
        atom = pos_lits[i].atom
        args = atom[1:]
        for fact in candidates(i):
            bound = []
            ok = True
            for a, v in zip(args, fact[1:]):
                if isinstance(a, Var):
                    prev = env.get(a, _UNBOUND)
                    if prev is _UNBOUND:
                        env[a] = v
                        bound.append(a)
                    elif prev != v:
                        ok = False
                        break
                elif a != v:
                    ok = False
                    break
            if ok:
                children[i] = fact
                yield from backtrack(k + 1)
            for a in bound:
                del env[a]

    yield from backtrack(0)


# --- Session ----------------------------------------------------------------

class Session:
    """A stateful stratified-Datalog session with atomic update batches.

    Args:
        rules: initial rules (iterable of :class:`Rule`); may be empty.
        facts: initial base facts; each entry is a fact tuple or
            ``(fact, source)`` pair (bare facts use :data:`DEFAULT_SOURCE`).
        max_facts: total-fact budget (base + derived); a batch that would
            exceed it is rejected wholesale (``complete=False``).
        max_rules / max_predicates / max_base_facts: declared input limits.

    The session is deterministic: the same construction and batch sequence
    always yields the same facts, derivations and reports.
    """

    def __init__(
        self,
        rules=(),
        facts=(),
        *,
        max_facts=SESSION_DEFAULT_FACT_BUDGET,
        max_rules=SESSION_MAX_RULES,
        max_predicates=SESSION_MAX_PREDICATES,
        max_base_facts=SESSION_MAX_BASE_FACTS,
    ):
        for name, val in (
            ("max_facts", max_facts),
            ("max_rules", max_rules),
            ("max_predicates", max_predicates),
            ("max_base_facts", max_base_facts),
        ):
            if isinstance(val, bool) or not isinstance(val, int):
                raise ValidationError(f"{name} must be an int (bool rejected), got {val!r}")
            if val < 0:
                raise ValidationError(f"{name} must be >= 0, got {val}")
        self._max_facts = max_facts
        self._max_rules = max_rules
        self._max_predicates = max_predicates
        self._max_base_facts = max_base_facts

        norm_rules = _validate_rules(rules, max_rules)
        _check_safety(norm_rules)
        entries = [_parse_change(e, f"initial fact {i}") for i, e in enumerate(facts)]
        sources = {}
        for fact, src in entries:
            sources.setdefault(fact, set()).add(src)
        if len(sources) > max_base_facts:
            raise LimitError(
                f"too many distinct base facts: {len(sources)} > limit {max_base_facts}"
            )
        _check_predicates_and_arity(list(sources), norm_rules, max_predicates)

        self._rules = tuple(norm_rules)
        self._strata = _build_strata(norm_rules)
        self._sources = sources
        self._store = {}
        self._index = {}
        self._nodes = {}        # nid -> (rule_index, head, children)
        self._node_keys = {}    # (rule_index, head, children) -> nid
        self._fact_nodes = {}   # fact -> {nid}
        self._consumers = {}    # fact -> {nid}  (nodes using fact as a child)
        self._next_nid = 0
        self._total = 0
        self._undo = None
        self._gross = None      # (added_set, removed_set) while a batch runs
        self._batch_id = 0
        self._rule_version = 0

        for fact in sources:
            self._raw_insert(fact)
        try:
            self._full_recompute()
        except _BudgetAbort as exc:
            raise LimitError(f"initial evaluation exceeds fact budget: {exc}") from None

    # -- version / state info ------------------------------------------------

    @property
    def batch_id(self):
        return self._batch_id

    @property
    def rule_version(self):
        return self._rule_version

    @property
    def rules(self):
        return self._rules

    def stats(self):
        """Current sizes: base/derived fact counts and derivation-node count."""
        return {
            "batch_id": self._batch_id,
            "rule_version": self._rule_version,
            "base_facts": len(self._sources),
            "derived_facts": self._total - len(self._sources),
            "total_facts": self._total,
            "derivation_nodes": len(self._nodes),
        }

    # -- low-level mutation primitives (with undo recording) -----------------

    def _raw_insert(self, fact):
        s = self._store.setdefault(fact[0], set())
        if fact in s:
            return False
        s.add(fact)
        idx = self._index.get(fact[0])
        if idx is None:
            idx = [dict() for _ in range(len(fact) - 1)]
            self._index[fact[0]] = idx
        for j, a in enumerate(fact[1:]):
            idx[j].setdefault(a, set()).add(fact)
        self._total += 1
        return True

    def _raw_remove(self, fact):
        s = self._store.get(fact[0])
        if not s or fact not in s:
            return False
        s.discard(fact)
        idx = self._index.get(fact[0])
        if idx is not None:
            for j, a in enumerate(fact[1:]):
                bucket = idx[j].get(a)
                if bucket is not None:
                    bucket.discard(fact)
                    if not bucket:
                        del idx[j][a]
        self._total -= 1
        return True

    def _raw_kill_node(self, nid):
        key = self._nodes.pop(nid)
        _ri, head, children = key
        del self._node_keys[key]
        fs = self._fact_nodes.get(head)
        if fs is not None:
            fs.discard(nid)
            if not fs:
                del self._fact_nodes[head]
        for c in set(children):
            cs = self._consumers.get(c)
            if cs is not None:
                cs.discard(nid)
                if not cs:
                    del self._consumers[c]
        return key

    def _raw_add_node_known(self, nid, key):
        self._nodes[nid] = key
        self._node_keys[key] = nid
        _ri, head, children = key
        self._fact_nodes.setdefault(head, set()).add(nid)
        for c in set(children):
            self._consumers.setdefault(c, set()).add(nid)

    def _insert_fact(self, fact):
        if self._raw_insert(fact):
            if self._undo is not None:
                self._undo.append(("fact+", fact))
            if self._gross is not None:
                self._gross[0].add(fact)

    def _remove_fact(self, fact):
        if self._raw_remove(fact):
            if self._undo is not None:
                self._undo.append(("fact-", fact))
            if self._gross is not None:
                self._gross[1].add(fact)

    def _add_node(self, ri, head, children):
        key = (ri, head, children)
        nid = self._node_keys.get(key)
        if nid is not None:
            return nid
        nid = self._next_nid
        self._next_nid += 1
        self._raw_add_node_known(nid, key)
        if self._undo is not None:
            self._undo.append(("node+", nid))
        return nid

    def _kill_node(self, nid):
        key = self._raw_kill_node(nid)
        if self._undo is not None:
            self._undo.append(("node-", nid, key))

    def _rollback(self, saved):
        for entry in reversed(self._undo):
            tag = entry[0]
            if tag == "fact+":
                self._raw_remove(entry[1])
            elif tag == "fact-":
                self._raw_insert(entry[1])
            elif tag == "node+":
                self._raw_kill_node(entry[1])
            else:  # "node-"
                self._raw_add_node_known(entry[1], entry[2])
        self._undo = None
        self._gross = None
        (
            self._total,
            self._next_nid,
            self._sources,
            self._rules,
            self._strata,
            self._rule_version,
        ) = saved

    # -- stratum evaluation ---------------------------------------------------

    def _fire_round(self, st, delta, round_no, for_recompute):
        """One semi-naive round over the session store.

        Returns ``(new_facts, fired, rules_fired)`` where ``fired`` lists
        every rule instantiation ``(rule_index, head, children)`` — all of
        them are recorded as derivation nodes (complete support recording),
        not just the first proof per fact.
        """
        new_facts = {}
        fired = []
        rules_fired = set()
        for ri, rule in st.rules:
            pos_lits = [l for l in rule.body if l.positive]
            neg_lits = [l for l in rule.body if not l.positive]
            if not pos_lits:
                distinguished = (None,) if round_no == 0 else ()
            elif for_recompute and round_no == 0 and not any(
                l.atom[0] in st.preds for l in pos_lits
            ):
                distinguished = (None,)  # non-recursive rule: full first round
            else:
                distinguished = tuple(
                    i for i, l in enumerate(pos_lits) if delta.get(l.atom[0])
                )
            for d in distinguished:
                for env, children in _xjoin(self, pos_lits, delta, d):
                    if neg_lits and not _negated_ok(neg_lits, env, self._store):
                        continue
                    head = _substitute(rule.head, env)
                    rules_fired.add(ri)
                    fired.append((ri, head, children))
                    if head not in new_facts and head not in self._store.get(head[0], ()):
                        new_facts[head] = True
        return list(new_facts), fired, rules_fired

    def _run_stratum_fixpoint(self, st, delta_init, for_recompute):
        delta = {p: set(s) for p, s in delta_init.items() if s}
        inserted = set()
        rules_fired = set()
        round_no = 0
        while True:
            if round_no > 0 and not any(delta.values()):
                break
            new_facts, fired, rf = self._fire_round(st, delta, round_no, for_recompute)
            rules_fired |= rf
            for ri, head, children in fired:
                self._add_node(ri, head, children)
            if new_facts:
                if self._total + len(new_facts) > self._max_facts:
                    raise _BudgetAbort(
                        f"fact budget exceeded: {self._total} facts so far + "
                        f"{len(new_facts)} facts in the next round > budget "
                        f"{self._max_facts}"
                    )
                for f in new_facts:
                    self._insert_fact(f)
                    inserted.add(f)
            delta = {}
            for f in new_facts:
                delta.setdefault(f[0], set()).add(f)
            round_no += 1
        return inserted, rules_fired

    def _recompute_stratum(self, st):
        """Drop and rebuild one stratum's derived facts (negation strata and
        full recomputes).  Returns a :class:`StratumReport`."""
        old_derived = set()
        for p in st.preds:
            for f in self._store.get(p, ()):
                if f not in self._sources:
                    old_derived.add(f)
        for f in old_derived:
            self._remove_fact(f)
        nids = [nid for nid, key in self._nodes.items() if key[1][0] in st.preds]
        for nid in nids:
            self._kill_node(nid)
        delta_init = {p: set(self._store.get(p, ())) for p in st.preds}
        inserted, rules_fired = self._run_stratum_fixpoint(st, delta_init, True)
        added = inserted - old_derived
        removed = old_derived - inserted
        return StratumReport(
            stratum=st.index,
            predicates=frozenset(st.preds),
            kind="negation" if st.has_negation else "positive",
            recomputed=True,
            deleted=frozenset(removed),
            rederived=frozenset(),
            inserted=frozenset(added),
            rules_fired=frozenset(rules_fired),
        )

    def _full_recompute(self):
        for p in list(self._store):
            for f in list(self._store[p]):
                if f not in self._sources:
                    self._remove_fact(f)
        for nid in list(self._nodes):
            self._kill_node(nid)
        reports = []
        for st in self._strata:
            reports.append(self._recompute_stratum(st))
        return reports

    # -- DRed: over-delete + structural rederive ------------------------------

    def _dred_delete(self, st, removed_all):
        """Delete/rederive for one positive stratum.

        Over-delete every fact downstream of ``removed_all`` through this
        stratum's derivation DAG, then re-derive exactly the facts that still
        have a derivation node whose children all survived.  A pure recursive
        cycle cannot keep itself alive: with no node grounded outside the
        deleted set, the whole cycle is retracted.
        """
        seeds = [f for f in removed_all if f[0] in st.all_preds]
        if not seeds:
            return set(), set()
        D = set(seeds)
        tainted = set()
        stack = list(seeds)
        while stack:
            f = stack.pop()
            for nid in self._consumers.get(f, ()):
                if nid in tainted:
                    continue
                node = self._nodes[nid]
                if node[1][0] not in st.preds:
                    continue  # another stratum's node: handled at its own level
                tainted.add(nid)
                head = node[1]
                if head not in D and head not in self._sources:
                    D.add(head)
                    stack.append(head)
        # Rederive: a fact in D survives iff some untainted node supports it.
        dirty = {}
        for nid in tainted:
            children = self._nodes[nid][2]
            dirty[nid] = sum(1 for c in children if c in D)
        rederived = set()
        queue = []
        for f in D:
            for nid in self._fact_nodes.get(f, ()):
                if nid not in tainted:
                    queue.append(f)
                    break
        while queue:
            f = queue.pop()
            if f not in D:
                continue
            D.discard(f)
            rederived.add(f)
            for nid in self._consumers.get(f, ()):
                d = dirty.get(nid)
                if d is None:
                    continue
                d -= 1
                if d == 0:
                    del dirty[nid]
                    tainted.discard(nid)
                    head = self._nodes[nid][1]
                    if head in D:
                        queue.append(head)
                else:
                    dirty[nid] = d
        deleted = set()
        for f in D:
            if f not in self._sources and f in self._store.get(f[0], ()):
                self._remove_fact(f)
                deleted.add(f)
        for nid in tainted:
            self._kill_node(nid)
        return deleted, rederived

    # -- batch processing -----------------------------------------------------

    def _incremental_update(self, added_base, removed_base):
        for f in removed_base:
            self._remove_fact(f)
        for f in added_base:
            self._insert_fact(f)
        removed_all = set(removed_base)
        added_by_pred = {}
        for f in added_base:
            added_by_pred.setdefault(f[0], set()).add(f)
        changed_preds = set(added_by_pred) | {f[0] for f in removed_base}
        reports = []
        for st in self._strata:
            if not (changed_preds & st.all_preds):
                continue  # unaffected stratum: no work at all
            if st.has_negation:
                rep = self._recompute_stratum(st)
            else:
                deleted, rederived = self._dred_delete(st, removed_all)
                delta = {}
                for p in st.all_preds:
                    a = added_by_pred.get(p)
                    if a:
                        delta[p] = set(a)
                inserted, rules_fired = self._run_stratum_fixpoint(st, delta, False)
                rep = StratumReport(
                    stratum=st.index,
                    predicates=frozenset(st.preds),
                    kind="positive",
                    recomputed=False,
                    deleted=frozenset(deleted),
                    rederived=frozenset(rederived),
                    inserted=frozenset(inserted),
                    rules_fired=frozenset(rules_fired),
                )
            reports.append(rep)
            for f in rep.inserted:
                added_by_pred.setdefault(f[0], set()).add(f)
            changed_preds |= {f[0] for f in rep.inserted} | {f[0] for f in rep.deleted}
            removed_all |= rep.deleted
        return reports

    def apply_batch(self, *, add=(), remove=(), rules=None):
        """Apply one atomic batch of fact changes and/or a rule replacement.

        ``add`` / ``remove`` entries are ``(fact, source)`` pairs or bare
        facts (default source).  Within a batch, removals are applied before
        additions (fixed default).  ``rules`` (optional) replaces the whole
        rule set atomically: it is validated and stratified *before* anything
        is published; on any failure the session is untouched.

        Returns an :class:`UpdateReport`.  A batch rejected for budget
        reasons returns ``complete=False`` and changes nothing.
        """
        adds = [_parse_change(e, f"add entry {i}") for i, e in enumerate(add)]
        rems = [_parse_change(e, f"remove entry {i}") for i, e in enumerate(remove)]
        if rules is not None:
            norm_rules = _validate_rules(rules, self._max_rules)
            _check_safety(norm_rules)
        else:
            norm_rules = None

        new_sources = {f: set(s) for f, s in self._sources.items()}
        ignored = []
        lost_sources = {}
        for fact, src in rems:
            s = new_sources.get(fact)
            if s and src in s:
                s.discard(src)
                lost_sources.setdefault(fact, []).append(src)
                if not s:
                    del new_sources[fact]
            else:
                ignored.append((fact, src))
        for fact, src in adds:
            new_sources.setdefault(fact, set()).add(src)
        if len(new_sources) > self._max_base_facts:
            raise LimitError(
                f"too many distinct base facts: {len(new_sources)} > limit "
                f"{self._max_base_facts}"
            )
        eff_rules = norm_rules if norm_rules is not None else self._rules
        _check_predicates_and_arity(list(new_sources), eff_rules, self._max_predicates)
        new_strata = _build_strata(norm_rules) if norm_rules is not None else None

        added_base = [f for f in new_sources if f not in self._sources]
        removed_base = [f for f in self._sources if f not in new_sources]

        saved = (
            self._total,
            self._next_nid,
            self._sources,
            self._rules,
            self._strata,
            self._rule_version,
        )
        self._undo = []
        self._gross = (set(), set())
        self._sources = new_sources
        if norm_rules is not None:
            self._rules = tuple(norm_rules)
            self._strata = new_strata
        try:
            if norm_rules is not None:
                strata_reports = self._full_recompute()
            else:
                strata_reports = self._incremental_update(added_base, removed_base)
        except _BudgetAbort as exc:
            self._rollback(saved)
            return UpdateReport(
                batch_id=self._batch_id,
                rule_version=self._rule_version,
                rules_replaced=norm_rules is not None,
                complete=False,
                reason=str(exc),
                ignored_removals=ignored,
            )
        self._undo = None
        gross_added, gross_removed = self._gross[0], self._gross[1]
        self._gross = None
        old_rule_version = self._rule_version
        if norm_rules is not None:
            self._rule_version += 1
        self._batch_id += 1

        net_added = gross_added - gross_removed
        net_removed = gross_removed - gross_added
        reasons = {}
        for f in net_removed:
            if f in lost_sources and f not in new_sources:
                reasons[f] = ("base-lost-source", tuple(lost_sources[f]))
            elif norm_rules is not None:
                reasons[f] = ("rules-replaced", old_rule_version)
            else:
                st = next(
                    (r for r in strata_reports if f in r.deleted), None
                )
                if st is not None and st.recomputed:
                    reasons[f] = ("negation-stratum-recomputed", st.stratum)
                elif st is not None:
                    reasons[f] = ("support-retracted", st.stratum)
                else:
                    reasons[f] = ("support-retracted", None)
        return UpdateReport(
            batch_id=self._batch_id,
            rule_version=self._rule_version,
            rules_replaced=norm_rules is not None,
            complete=True,
            reason=None,
            added_facts=net_added,
            removed_facts=net_removed,
            retraction_reasons=reasons,
            strata=strata_reports,
            ignored_removals=ignored,
        )

    def replace_rules(self, rules):
        """Atomically replace the whole rule set (one batch, no fact changes).

        Safety and stratification are checked before publishing; on failure
        the original rules stay in force.
        """
        return self.apply_batch(rules=rules)

    # -- snapshots ------------------------------------------------------------

    def current_facts(self):
        """frozenset of all facts currently in the store (no derivations)."""
        return frozenset(f for fs in self._store.values() for f in fs)

    def snapshot(self):
        """An immutable :class:`Snapshot` of the current committed state.

        The snapshot freezes the fact set, the per-fact best derivations
        (shortest, then lexicographically smallest — the same tie-break as
        :func:`datalog.evaluate`), the live source map and the rule list, so
        it keeps answering for its own rule version forever.
        """
        depth, best = _compute_derivations(
            self._sources, self._nodes, self._fact_nodes, self._consumers
        )
        facts = frozenset(f for fs in self._store.values() for f in fs)
        derivations = {
            f: Derivation(ri, children)
            for f, (_d, ri, children) in best.items()
            if f not in self._sources
        }
        return Snapshot(
            rule_version=self._rule_version,
            batch_id=self._batch_id,
            facts=facts,
            sources={f: frozenset(s) for f, s in self._sources.items()},
            derivations=derivations,
            rules=self._rules,
        )

    # -- independent checkers --------------------------------------------------

    def verify_retraction(self, fact):
        """Independently confirm that ``fact`` is not derivable any more.

        Replays the *current* rules over the *current* live base facts from
        scratch with a small standalone evaluator (no engine/session code)
        and checks membership — the check does not trust any flag or cached
        state of the session.
        """
        fact = tuple(fact)
        if fact in self._sources:
            return False
        if fact in self._store.get(fact[0], ()):
            return False
        model = _standalone_least_model(list(self._sources), self._rules)
        return fact not in model


# --- Best-derivation computation (shortest, then lexicographic) -------------

def _compute_derivations(sources, nodes, fact_nodes, consumers):
    """Least-fixpoint proof depths over the live derivation-node DAG.

    Returns ``(depth, best)`` with ``best[fact] = (depth, rule_index,
    children)`` minimised by ``(depth, rule_index, children keys)`` — the
    same tie-break as the one-shot engine.  Dijkstra-style: facts finalise
    in non-decreasing depth order, so the first finalisation is minimal and
    cyclic self-support (already impossible by construction) cannot win.
    """
    depth = {}
    best = {}
    cand = {}
    remaining = {}
    maxchild = {}
    heap = []
    counter = 0

    def push(d, f):
        nonlocal counter
        heapq.heappush(heap, (d, counter, f))
        counter += 1

    for f in sources:
        push(0, f)
    for nid, (ri, head, children) in nodes.items():
        uniq = set(children)
        if not uniq:
            val = 1
            key = (val, ri, tuple(_fact_key(c) for c in children))
            if head not in cand or key < cand[head]:
                cand[head] = key
                best[head] = (val, ri, children)
            push(val, head)
        else:
            remaining[nid] = len(uniq)
            maxchild[nid] = 0
    while heap:
        d, _seq, f = heapq.heappop(heap)
        if f in depth:
            continue
        depth[f] = d
        for nid in consumers.get(f, ()):
            r = remaining.get(nid)
            if r is None:
                continue
            r -= 1
            if d > maxchild[nid]:
                maxchild[nid] = d
            if r == 0:
                del remaining[nid]
                ri, head, children = nodes[nid]
                val = 1 + maxchild[nid]
                key = (val, ri, tuple(_fact_key(c) for c in children))
                if head not in depth and (head not in cand or key < cand[head]):
                    cand[head] = key
                    best[head] = (val, ri, children)
                push(val, head)
            else:
                remaining[nid] = r
    return depth, best


# --- Snapshot ---------------------------------------------------------------

class Snapshot:
    """Immutable view of one committed session state (one rule version).

    Evidence checked against a snapshot can only reference sources that were
    live when the snapshot was taken — later batches and rule versions are
    invisible to it.
    """

    __slots__ = (
        "rule_version",
        "batch_id",
        "facts",
        "derived",
        "sources",
        "rules",
        "_derivations",
    )

    def __init__(self, *, rule_version, batch_id, facts, sources, derivations, rules):
        self.rule_version = rule_version
        self.batch_id = batch_id
        self.facts = frozenset(facts)
        self.derived = frozenset(f for f in facts if f not in sources)
        self.sources = MappingProxyType(dict(sources))
        self.rules = tuple(rules)
        self._derivations = dict(derivations)

    def query(self, pred, *pattern):
        """Sorted facts for ``pred`` matching ``pattern`` (constants or Var)."""
        check_predicate(pred, "query")
        for i, p in enumerate(pattern):
            if not isinstance(p, Var):
                check_constant(p, f"query pattern argument {i}")
        hits = [
            f
            for f in self.facts
            if f[0] == pred
            and len(f) - 1 == len(pattern)
            and all(isinstance(p, Var) or p == a for p, a in zip(pattern, f[1:]))
        ]
        hits.sort(key=_fact_key)
        return hits

    def derivation(self, fact):
        """The recorded best :class:`Derivation` for ``fact`` (``None`` for a
        base fact).  Raises ``KeyError`` for a fact not in the snapshot."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")
        return self._derivations.get(fact)

    def explain(self, fact):
        """Render the derivation DAG of ``fact`` as an indented source chain."""
        fact = tuple(fact)
        if fact not in self.facts:
            raise KeyError(f"unknown fact {fact!r}")

        def leaf_label(f):
            srcs = sorted(self.sources.get(f, ()), key=repr)
            return f"[base fact, sources={srcs}]"

        return _render_explain(fact, self._derivations, self.rules, leaf_label)

    # -- evidence checking (structural, from live base facts upward) ---------

    def check_evidence(self, fact):
        """Verify the evidence for ``fact`` bottom-up from live base facts.

        Returns ``(ok, message)``.  Every derivation node is re-matched
        against the rule body, negated literals are checked absent, and every
        leaf must be a base fact with at least one live source in *this*
        snapshot.  Nothing is taken on trust from solver flags.
        """
        fact = tuple(fact)
        if fact not in self.facts:
            return (False, f"{fact!r} is not a fact of this snapshot")
        return self._check(fact, set())

    def _check(self, f, active):
        if self.sources.get(f):
            return (True, "base fact with live sources")
        d = self._derivations.get(f)
        if d is None:
            return (False, f"{_fmt_fact(f)}: no live source and no derivation")
        if f in active:
            return (False, f"cycle through {_fmt_fact(f)} (evidence must be a DAG)")
        if not (0 <= d.rule_index < len(self.rules)):
            return (False, f"rule index {d.rule_index} out of range for this version")
        rule = self.rules[d.rule_index]
        pos_lits = [l for l in rule.body if l.positive]
        if len(pos_lits) != len(d.children):
            return (False, f"{_fmt_fact(f)}: child count does not match rule body")
        env = {}
        for lit, child in zip(pos_lits, d.children):
            if lit.atom[0] != child[0]:
                return (False, f"{_fmt_fact(f)}: child predicate mismatch")
            if child not in self.facts:
                return (False, f"{_fmt_fact(f)}: child {_fmt_fact(child)} is not a fact")
            for a, v in zip(lit.atom[1:], child[1:]):
                if isinstance(a, Var):
                    if a in env:
                        if env[a] != v:
                            return (False, f"{_fmt_fact(f)}: inconsistent variable binding")
                    else:
                        env[a] = v
                elif a != v:
                    return (False, f"{_fmt_fact(f)}: constant mismatch in body")
        head = (rule.head[0],) + tuple(
            env[a] if isinstance(a, Var) else a for a in rule.head[1:]
        )
        if head != f:
            return (False, f"{_fmt_fact(f)}: rule head does not unify to the fact")
        for lit in rule.body:
            if not lit.positive:
                g = (lit.atom[0],) + tuple(
                    env[a] if isinstance(a, Var) else a for a in lit.atom[1:]
                )
                if g in self.facts:
                    return (
                        False,
                        f"{_fmt_fact(f)}: negated literal {_fmt_fact(g)} is present",
                    )
        active.add(f)
        for c in d.children:
            ok, msg = self._check(c, active)
            if not ok:
                return (False, msg)
        active.discard(f)
        return (True, "ok")

    def verify_all(self):
        """Check evidence for every derived fact in the snapshot.

        Returns ``(ok, problems)`` with ``problems`` capped at 20 messages.
        """
        problems = []
        for f in sorted(self.derived, key=_fact_key):
            ok, msg = self.check_evidence(f)
            if not ok:
                problems.append(msg)
                if len(problems) >= 20:
                    break
        return (not problems, problems)


# --- Standalone checker evaluator (shares no code with engine/session) -------

def _standalone_least_model(base_facts, rules):
    """Naive stratified least model, written independently for verification.

    Used only by :meth:`Session.verify_retraction`; intentionally simple
    (no indexes, no semi-naive) and small-domain oriented.
    """
    preds = set()
    for r in rules:
        preds.add(r.head[0])
        for lit in r.body:
            preds.add(lit.atom[0])
    stratum = {p: 0 for p in preds}
    for _ in range(len(preds) + 1):
        changed = False
        for r in rules:
            for lit in r.body:
                need = stratum[lit.atom[0]] + (0 if lit.positive else 1)
                if need > stratum[r.head[0]]:
                    stratum[r.head[0]] = need
                    changed = True
        if not changed:
            break
    store = {}
    for f in base_facts:
        store.setdefault(f[0], set()).add(f)

    def fire(rule):
        pos_lits = [l for l in rule.body if l.positive]
        neg_lits = [l for l in rule.body if not l.positive]
        out = set()

        def bt(i, env):
            if i == len(pos_lits):
                for lit in neg_lits:
                    g = (lit.atom[0],) + tuple(
                        env[a] if isinstance(a, Var) else a for a in lit.atom[1:]
                    )
                    if g in store.get(g[0], ()):
                        return
                out.add(
                    (rule.head[0],)
                    + tuple(env[a] if isinstance(a, Var) else a for a in rule.head[1:])
                )
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

    levels = sorted({stratum[r.head[0]] for r in rules})
    for s in levels:
        srules = [r for r in rules if stratum[r.head[0]] == s]
        while True:
            new = set()
            for r in srules:
                for f in fire(r):
                    if f not in store.get(f[0], ()):
                        new.add(f)
            if not new:
                break
            for f in new:
                store.setdefault(f[0], set()).add(f)
    return {f for fs in store.values() for f in fs}
