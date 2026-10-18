"""
MemCore — recall & search engine.

Hybrid FTS5 BM25 + dense vector RRF search, Salience L1 previews,
freshness/decay ranking, and scope-enforced reads.
"""
from __future__ import annotations

import array
from datetime import datetime, timezone
import json
import math
import operator
import re
import sqlite3
import unicodedata
from typing import Any

from . import ablation as _ablation
from . import embedding
from . import store
from .write import (
    MemCoreError,
    _membership_role,
    _now,
    _new_id,
    _audit,
    fingerprint,
    check_source_staleness,
    _table_cols,
    _clear_table_cols_cache,
    _conn_schema_marker,
    _TABLE_COLS_CACHE_MAX,
    _CONN_TABLE_COLS,
    _eval_now_iso,
    _coerce_now,
    REINFORCEMENT_WINDOW_DAYS,
    FRESHNESS_AGING_DAYS,
    FRESHNESS_STALE_DAYS,
)

# Ã¢â€â‚¬Ã¢â€â‚¬ reads (scope enforced in SQL WHERE) Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

def _recall_tombstone_guard(alias='m'):
    """SQL predicate excluding claims blocked by active scope-aware tombstones."""
    return (
        f'{alias}.claim_fingerprint IS NOT NULL AND '
        'NOT EXISTS (SELECT 1 FROM tombstone t '
        f'WHERE t.claim_fingerprint={alias}.claim_fingerprint '
        'AND t.overridden_by IS NULL AND ('
        "t.scope='global' OR "
        f't.scope={alias}.project_id OR '
        f"({alias}.scope='private' AND "
        f"t.scope='private:' || {alias}.project_id || ':' || {alias}.owner_agent_id)))"
    )


def version_at(conn, memory_id, agent_id, as_of):
    """The version of one memory that was valid at a timestamp.

    Point-in-time read: the version whose [valid_from, valid_until) window
    contains as_of. Scope-enforced like every other read. Returns the
    memory_version row or None. Never mutates.
    """
    row = conn.execute(
        'SELECT m.project_id FROM memory m WHERE m.id=?', (memory_id,)
    ).fetchone()
    if row is None:
        return None
    project_id = row[0]
    if _membership_role(conn, project_id, agent_id) is None:
        return None
    cols = {r[1] for r in conn.execute('PRAGMA table_info(memory_version)')}
    if 'valid_until' not in cols:
        return None
    return conn.execute(
        'SELECT v.id, v.content, v.valid_from, v.valid_until, v.created_by_agent_id '
        'FROM memory_version v JOIN memory m ON m.id = v.memory_id '
        'WHERE v.memory_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        '  AND datetime(v.valid_from) <= datetime(?) '
        '  AND (v.valid_until IS NULL OR datetime(?) < datetime(v.valid_until)) '
        'ORDER BY datetime(v.valid_from) DESC LIMIT 1',
        (memory_id, agent_id, as_of, as_of),
    ).fetchone()


def visible_memories(conn, project_id, agent_id, include_disabled=False,
                     include_rejected=False):
    """All memories agent_id may read in project_id.

    Scope rule lives in the WHERE clause, never in Python post-filtering:
      project scope -> every member reads it
      private scope -> owner only
    Excludes rejected/superseded/disabled from 'current truth' by default.
    """
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    excluded = ["'superseded'"]
    if not include_rejected:
        excluded.append("'rejected'")
    if not include_disabled:
        excluded.append("'disabled'")
    if include_rejected:
        tombstone_sql = (
            "  AND (m.lifecycle='rejected' OR (" +
            _recall_tombstone_guard('m') + ')) '
        )
    else:
        tombstone_sql = '  AND ' + _recall_tombstone_guard('m') + ' '
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
        '       v.content, m.owner_agent_id, m.type '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        f'  AND m.lifecycle NOT IN ({", ".join(excluded)}) ' +
        tombstone_sql +
        'ORDER BY m.pinned DESC, m.updated_at DESC, m.id ASC',
        (project_id, agent_id)
    )
    return cur.fetchall()


def private_memories(conn, project_id, agent_id):
    """ONLY this agent's private memories in a project. Others' never appear."""
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    cur = conn.execute(
        'SELECT m.id, m.scope, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND m.scope = 'private' "
        '  AND m.owner_agent_id = ?',
        (project_id, agent_id)
    )
    return cur.fetchall()


# —— Lane 3.2: Fleet alias map (static, query-side only).
# Add entries ONLY from failing baseline paraphrase queries.
# Substring containment on lowered raw query, so glued Thai matches.
_FLEET_ALIASES = {
    'ai gateway': '9router',
    'ทีม': 'fleet roster',
    'พี่โชค': 'thai',
    'สแกน': 'scan pacing',
}


def _expand_aliases(query: str) -> str:
    """Expand known fleet aliases in the raw query before tokenization.

    Substring match on lowered query; matched values appended with spaces.
    Eval-only: MEMCORE_ABLATE_ALIAS_EXPANSION=1 returns query unchanged.
    """
    try:
        if _ablation.is_alias_expansion_ablated():
            return query
    except Exception:
        pass
    q = str(query).lower()
    expansions = []
    for key, val in _FLEET_ALIASES.items():
        if key in q:
            expansions.append(val)
    if expansions:
        return query + ' ' + ' '.join(expansions)
    return query


def _thai_bigrams(token: str) -> list[str]:
    """Generate character bigrams from a Thai/Unicode token (ord>127, len>=4).

    Used as extra OR-terms in FTS5 query (as prefix terms) and exact-fallback
    (as substring ORs) to catch glued-word substrings. Capped by caller
    (total OR-terms <= 32).
    Eval-only: MEMCORE_ABLATE_THAI_BIGRAM=1 returns [].
    """
    try:
        if _ablation.is_thai_bigram_ablated():
            return []
    except Exception:
        pass
    if len(token) < 4:
        return []
    # Only emit for tokens containing non-ASCII
    if all(ord(ch) <= 127 for ch in token):
        return []
    return [token[i:i+2] for i in range(len(token) - 1)]


def _query_tokens(query: str) -> list[str]:
    """Split a raw query into word-ish tokens (Unicode L/N/M + underscore).

    Shared by _fts_query and the search exact-fallback so both see the
    same token stream.
    """
    tokens, buf = [], []
    for ch in str(query):
        category = unicodedata.category(ch)
        if ch == '_' or category[:1] in ('L', 'N', 'M'):
            buf.append(ch)
        elif buf:
            tokens.append(''.join(buf))
            buf = []
    if buf:
        tokens.append(''.join(buf))
    return tokens


def _fts_query(query: str) -> str:
    """Sanitize a raw Unicode user string into a safe FTS5 expression.

    Keep Unicode letters/numbers/marks plus underscore, split on punctuation,
    then quote each token. For Thai/Unicode tokens (ord>127, len>=4) also
    emit character bigrams as extra prefix OR-terms ("bg"*) to catch
    glued-word substrings — plain "bg" never matches in FTS5 unicode61,
    only prefix "bg"* matches token starts.
    Total OR-terms capped at 32 (ponytail: O(query_len) terms; upgrade path
    is a proper segmenter only if owner approves a dep).
    """
    tokens = _query_tokens(query)
    if not tokens:
        return ''

    # Build OR-terms: each token + its Thai bigrams as prefix terms (if applicable)
    terms = []
    for token in tokens:
        terms.append('"%s"' % token)
        # Lane 3.1: add Thai char bigrams as prefix terms for glued words
        for bg in _thai_bigrams(token):
            terms.append('"%s"*' % bg)
            if len(terms) >= 32:
                break
        if len(terms) >= 32:
            break

    return ' OR '.join(terms)


# _eval_now_iso and _coerce_now imported from .write


#: Retention-ranking coefficients (Adopt-1; reference: ai-memory decay.rs).
#: retention = salience * exp(-lambda(type) * age_days)
#:             + sigma * ln(1 + recall_count) * exp(-mu * days_since_access)
#: Higher score ranks first. Derived ONLY from existing columns; decay never
#: hides rows and never tombstones — it only refines rank inside the
#: existing CASE ordering family.
_DECAY_LAMBDA_DEFAULT = 0.02  # scalar fall-back, ~35-day half-life
_DECAY_SIGMA = 0.6  # reinforcement magnitude
_DECAY_MU = 0.04  # reinforcement recency fall-off per day
#: Per-memory_type half-life map (days). Types absent here fall back to the
#: scalar _DECAY_LAMBDA_DEFAULT, byte-identical to the single-lambda formula.
_DECAY_HALF_LIFE_DAYS_BY_TYPE = {
    'fact': 60.0,
    'decision': 90.0,
    'preference': 90.0,
    'note': 30.0,
    'observation': 14.0,
}
_DECAY_SALIENCE_BY_VERIFICATION = {
    'user_authoritative': 1.5,
    'runtime_verified': 1.25,
    'source_backed': 1.1,
}
_DECAY_SALIENCE_BY_LIFECYCLE = {
    'accepted': 1.0,
    'conflict': 0.9,
}


def _decay_lambda_sql(alias='m'):
    """Per-type decay rate: CASE over memory_type, scalar default fallback."""
    if alias == 'm' and '_DECAY_LAMBDA_SQL_M' in globals():
        return _DECAY_LAMBDA_SQL_M
    whens = ' '.join(
        "WHEN '%s' THEN %.17g" % (mem_type, math.log(2) / half_life)
        for mem_type, half_life in sorted(_DECAY_HALF_LIFE_DAYS_BY_TYPE.items())
    )
    return '(CASE %s.type %s ELSE %.17g END)' % (alias, whens, _DECAY_LAMBDA_DEFAULT)


def _decay_salience_sql(alias='m'):
    """Verification/lifecycle weight; unknown values fall back to 1.0/0.8."""
    if alias == 'm' and '_DECAY_SALIENCE_SQL_M' in globals():
        return _DECAY_SALIENCE_SQL_M
    ver = ' '.join(
        "WHEN '%s' THEN %s" % (value, weight)
        for value, weight in sorted(_DECAY_SALIENCE_BY_VERIFICATION.items())
    )
    life = ' '.join(
        "WHEN '%s' THEN %s" % (value, weight)
        for value, weight in sorted(_DECAY_SALIENCE_BY_LIFECYCLE.items())
    )
    return (
        '((CASE %s.verification %s ELSE 1.0 END) * '
        '(CASE %s.lifecycle %s ELSE 0.8 END))' % (alias, ver, alias, life)
    )


_DECAY_LAMBDA_SQL_M = _decay_lambda_sql('m')
_DECAY_SALIENCE_SQL_M = _decay_salience_sql('m')

#: Pre-assembled retention ORDER-BY fragments for the default 'm' alias
#: (the only alias search/export use). Hoisting the %-formatting here removes
#: per-call string building from the search hot path; the fragment text is
#: byte-identical to what _retention_order() used to assemble each call.
_RETENTION_AGE_SQL_M = (
    'max(0.0, COALESCE(julianday(?) - julianday(m.updated_at), 0.0))')
_RETENTION_ACCESS_SQL_M = (
    'CASE WHEN m.last_recalled IS NULL THEN 0.0 ELSE %.17g * '
    'ln(1.0 + COALESCE(m.recall_count, 0)) * exp(-%.17g * '
    'max(0.0, COALESCE(julianday(?) - julianday(m.last_recalled), 0.0))) END'
    % (_DECAY_SIGMA, _DECAY_MU)
)
_RETENTION_EXPR_WITH_RECALL_M = (
    'COALESCE(%s * exp(-%s * %s) + %s, 0.0)'
    % (_DECAY_SALIENCE_SQL_M, _DECAY_LAMBDA_SQL_M,
       _RETENTION_AGE_SQL_M, _RETENTION_ACCESS_SQL_M)
)
_RETENTION_EXPR_NO_RECALL_M = (
    'COALESCE(%s * exp(-%s * %s) + 0.0, 0.0)'
    % (_DECAY_SALIENCE_SQL_M, _DECAY_LAMBDA_SQL_M, _RETENTION_AGE_SQL_M)
)

#: Hard cap on the per-connection table-columns cache. Entries are keyed by
#: id(conn), and sqlite3 connections support neither weakrefs nor attribute
#: slots, so entries cannot auto-evict — the cap drops the cache wholesale
#: when it grows past the bound.
# schema table cols cache imported from .write


def _has_recall_cols(conn) -> bool:
    cols = _table_cols(conn, 'memory')
    return 'recall_count' in cols and 'last_recalled' in cols


def _retention_order(conn, enabled, alias='m'):
    """ORDER-BY fragment + params for the continuous retention tie-break.

    Returns ('', ()) when disabled (MEMCORE_ABLATE_DECAY=1): the caller then
    emits the legacy CASE+bm25 ordering byte-identically. Enabled, it returns
    (fragment, [now_iso, (now_iso when recall columns exist)]) where now_iso
    comes from the simulated clock (FAKE_NOW else real time). Never raises:
    unreadable columns degrade to the age-only term.
    """
    if not enabled:
        return '', ()
    store._register_math_functions(conn)
    has_recall = _has_recall_cols(conn)
    now_iso = _eval_now_iso()
    if alias == 'm':
        # Hot path (search/export): pre-assembled constant fragments, no
        # per-call %-formatting.
        if has_recall:
            return _RETENTION_EXPR_WITH_RECALL_M, (now_iso, now_iso)
        return _RETENTION_EXPR_NO_RECALL_M, (now_iso,)
    age = ('max(0.0, COALESCE(julianday(?) - julianday(%s.updated_at), 0.0))'
           % alias)
    if has_recall:
        access = (
            'CASE WHEN %s.last_recalled IS NULL THEN 0.0 ELSE %.17g * '
            'ln(1.0 + COALESCE(%s.recall_count, 0)) * exp(-%.17g * '
            'max(0.0, COALESCE(julianday(?) - julianday(%s.last_recalled), 0.0))) END'
            % (alias, _DECAY_SIGMA, alias, _DECAY_MU, alias)
        )
        params = (now_iso, now_iso)
    else:
        access = '0.0'
        params = (now_iso,)
    fragment = (
        'COALESCE(%s * exp(-%s * %s) + %s, 0.0)'
        % (_decay_salience_sql(alias), _decay_lambda_sql(alias), age, access)
    )
    return fragment, params


#: Memories recalled within this window resist freshness decay.
#: A fact the fleet actually uses stays current; a fact nobody recalls fades
#: on the plain clock. Tuned against the fleet's weekly cadence.
# REINFORCEMENT_WINDOW_DAYS imported from .write


def record_recall(conn, memory_ids) -> int:
    """Bump recall counters for retrieved memories. Best-effort, never raises.

    Called by the tool layer AFTER a successful read-only search, on a
    writable connection — never inside search() itself, which must stay
    safe on read-only handles. Returns the number of rows touched.
    """
    if not memory_ids:
        return 0
    if isinstance(memory_ids, (str, bytes)):
        memory_ids = [memory_ids]
    try:
        raw_ids = list(memory_ids)
    except (TypeError, ValueError):
        return 0
    ids = [m for m in dict.fromkeys(raw_ids) if m and isinstance(m, str)]
    if not ids:
        return 0
    try:
        if not _has_recall_cols(conn):
            return 0
        now = _eval_now_iso()
        touched = 0
        for mem_id in ids:
            cur = conn.execute(
                'UPDATE memory SET recall_count = recall_count + 1, '
                'last_recalled = ? WHERE id = ?',
                (now, mem_id),
            )
            touched += cur.rowcount
        conn.commit()
        return touched
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        return 0


#: Freshness label projected at read time (never written). Must agree with the
#: SEQUENTIAL sweep in apply_freshness_decay, which advances a row at most ONE
#: step per run: 'current' -> 'aging', and only a row already stored 'aging' can
#: reach 'stale'. Two errors came from projecting 'current' -> 'stale' in a single
#: hop (overstating decay: a 95-day row read 'stale' while the sweep writes
#: 'aging') and from stopping there (understating it: a row the sweep had already
#: aged kept reading 'aging' past the stale threshold). Both directions now
#: mirror the sweep. A stored 'stale' is terminal and always wins.
_FRESHNESS_PROJECTION_SQL = (
    "(CASE WHEN m.freshness = 'stale' THEN 'stale' "
    "WHEN m.freshness = 'aging' "
    "  AND NOT (m.last_recalled IS NOT NULL AND julianday(?) - julianday(m.last_recalled) < ?) "
    "  AND julianday(?) - julianday(m.updated_at) >= ? THEN 'stale' "
    "WHEN m.freshness = 'aging' THEN 'aging' "
    "WHEN m.last_recalled IS NOT NULL AND julianday(?) - julianday(m.last_recalled) < ? THEN 'current' "
    "WHEN julianday(?) - julianday(m.updated_at) >= ? THEN 'aging' "
    "ELSE 'current' END)"
)


def _freshness_params(now_iso: str) -> tuple:
    return (
        now_iso, REINFORCEMENT_WINDOW_DAYS, now_iso, FRESHNESS_STALE_DAYS,
        now_iso, REINFORCEMENT_WINDOW_DAYS, now_iso, FRESHNESS_AGING_DAYS
    )


_LIFECYCLE_ORDER = {'accepted': 0, 'conflict': 1}
_VERIF_ORDER = {'user_authoritative': 0, 'runtime_verified': 1, 'source_backed': 2}


def _run_substring_lane(conn, project_id, agent_id, substr_terms, detail_filter,
                        detail_params, retention_tail, retention_params, now_iso,
                        limit):
    """Unicode substring lane: unranked ``instr(content, term)`` OR-terms.

    unicode61 never segments glued Thai, so this exists purely to bridge that.
    It carries NO bm25 rank, which is why search() runs it only as a fallback and
    never lets it lead the result. Terms are ordered longest-first so SQLite's
    OR evaluation short-circuits early on selective matches.
    """
    sorted_terms = sorted(dict.fromkeys(substr_terms), key=len, reverse=True)
    or_clauses = ' OR '.join(['instr(v.content, ?) > 0'] * len(sorted_terms))
    return conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        '       v.content, m.owner_agent_id, 0.0 AS rank, '
        '       m.claim_fingerprint '
        'FROM memory m JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        '  AND ' + _recall_tombstone_guard('m') + ' '
        '  AND (' + or_clauses + ') ' + detail_filter +
        'ORDER BY m.pinned DESC, '
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, "
        "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 "
        "WHEN 'source_backed' THEN 2 ELSE 3 END, "
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END" +
        retention_tail + ', '
        'm.updated_at DESC, m.id ASC LIMIT ?',
        _freshness_params(now_iso) +
        (project_id, agent_id) + tuple(sorted_terms) + detail_params +
        tuple(retention_params) + (min(500, limit * DISTINCT_OVERFETCH),)
    ).fetchall()


#: Vector cosine lives in the embedding seam (single implementation, C-level
#: array/operator math). This module-level alias keeps the public name
#: ``core.cosine_similarity`` working for callers and tests.
cosine_similarity = embedding.cosine_similarity


def _vector_search_lane(conn, project_id, agent_id, query_vec: list[float],
                        limit: int = 20, detail_filter: str = '',
                        detail_params: tuple = ()) -> list[tuple]:
    """Retrieve candidate memories having embeddings and rank by cosine similarity.

    Returns rows matching the standard 9-tuple shape:
    (id, scope, lifecycle, verification, freshness, content, owner_agent_id, rank, claim_fingerprint)
    """
    if not query_vec:
        return []
    if not store._table_exists(conn, 'memory_embedding'):
        return []

    q_arr = array.array('f', query_vec)
    query_norm_sq = sum(x * x for x in q_arr)
    if query_norm_sq <= 0.0:
        return []
    query_norm = math.sqrt(query_norm_sq)
    q_len = len(q_arr)
    mul = operator.mul

    now_iso = _eval_now_iso()
    sql = (
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        '       v.content, m.owner_agent_id, e.vector, '
        '       m.claim_fingerprint, m.pinned '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'JOIN memory_embedding e ON e.version_id = v.id AND e.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        '  AND ' + _recall_tombstone_guard('m') + ' ' + detail_filter
    )
    params = _freshness_params(now_iso) + (project_id, agent_id) + detail_params

    cur = conn.execute(sql, params)
    candidates = []
    for row in cur.fetchall():
        mid, scope, lifecycle, verif, freshness, content, owner, blob, fp, pinned = row
        v_arr = array.array('f')
        v_arr.frombytes(blob)
        if len(v_arr) != q_len:
            continue
        dot = sum(map(mul, q_arr, v_arr))
        doc_norm_sq = sum(map(mul, v_arr, v_arr))
        if doc_norm_sq <= 0.0:
            sim = 0.0
        else:
            sim = dot / (query_norm * math.sqrt(doc_norm_sq))
        # rank: lower is better to match bm25 convention (e.g. -sim)
        rank = -sim
        candidates.append((mid, scope, lifecycle, verif, freshness, content, owner, rank, fp, pinned))

    def _cand_sort_key(c):
        pinned_order = 0 if c[9] else 1
        lifecycle_order = _LIFECYCLE_ORDER.get(c[2], 2)
        verif_order = _VERIF_ORDER.get(c[3], 3)
        return (pinned_order, lifecycle_order, verif_order, c[7])

    candidates.sort(key=_cand_sort_key)
    return [c[:9] for c in candidates[:limit]]


def _merge_exact_fallback(base_rows, exact_rows, limit, user_query,
                          query_tok=None, query_numbers=None,
                          query_tokens=None):
    """Merge ranked primary candidates with exact substring fallback rows.

    C3: collapse across the whole overfetch window, demote contradictory
    rows (numbers, exclusive choices, negated phrases), and only THEN slice
    to ``limit`` — a compatible row from the overfetch window can replace a
    demoted row instead of being cut early.
    ``query_tok``/``query_numbers``/``query_tokens`` may be precomputed by
    search() so the query side of the incompatibility check is analyzed
    exactly once (C2).
    """
    merged = list(base_rows)
    if exact_rows:
        seen = {row[0] for row in merged}
        merge_cap = min(500, limit * DISTINCT_OVERFETCH)
        for row in exact_rows:
            if row[0] not in seen:
                merged.append(row)
                seen.add(row[0])
                if len(merged) >= merge_cap:
                    break
    collapsed = _collapse_duplicate_claims(merged, limit=None)
    return _demote_numeric_mismatch(
        user_query, collapsed, query_tok=query_tok,
        query_numbers=query_numbers, query_tokens=query_tokens)[:limit]


def _extract_non_ascii_substr_terms(user_query: str) -> list[str]:
    """Match terms for the Unicode substring fallback lane (single pass).

    unicode61 never segments glued Thai, so non-ASCII queries need a
    substring fallback. Match terms = NON-ASCII word tokens of the
    PRE-expansion query + their char bigrams. ASCII tokens are deliberately
    excluded — FTS already handles them, and instr-ORing them (e.g. 'agent'
    matching 'agents') flips negation guards. Alias expansion can never break
    the verbatim check (pre-expansion query only); glued->spaced still
    bridges via bigrams. Capped at MAX_SUBSTR_TERMS.
    """
    if not any(ord(ch) > 127 for ch in user_query):
        return []
    terms: list[str] = []
    seen_terms: set[str] = set()
    for token in _query_tokens(user_query):
        if all(ord(ch) <= 127 for ch in token) or token in seen_terms:
            continue
        seen_terms.add(token)
        terms.append(token)
        for bg in _thai_bigrams(token):
            if bg in seen_terms:
                continue
            seen_terms.add(bg)
            terms.append(bg)
            if len(terms) >= MAX_SUBSTR_TERMS:
                break
        if len(terms) >= MAX_SUBSTR_TERMS:
            break
    return terms


def _search_detail_filter(conn, scope_detail):
    """(sql, params) for a scope_detail narrowing.

    Returns (None, ()) when the column is absent (pre-0016 store): the
    caller must then refuse the narrowed query rather than silently run it
    unscoped.
    """
    if scope_detail is None:
        return '', ()
    if 'scope_detail' not in _table_cols(conn, 'memory'):
        return None, ()
    return '  AND m.scope_detail = ? ', (str(scope_detail),)


def _run_fts_lane(conn, project_id, agent_id, match_expr, detail_filter,
                  detail_params, retention_tail, retention_params, now_iso,
                  fts_limit):
    """FTS5 bm25 lane: ranked primary candidates as 9-tuple rows."""
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        '       v.content, m.owner_agent_id, '
        '       bm25(memory_version_fts) AS rank, m.claim_fingerprint '
        'FROM memory_version_fts fts '
        'JOIN memory_version v ON v.rowid = fts.rowid '
        'JOIN memory m ON m.id = v.memory_id '
        'WHERE memory_version_fts MATCH ? '
        '  AND v.id = m.current_version_id '
        '  AND m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        '  AND ' + _recall_tombstone_guard('m') + ' ' + detail_filter +
        'ORDER BY m.pinned DESC, ' +
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, " +
        "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, " +
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END" +
        retention_tail + ', ' +
        'rank ASC, m.updated_at DESC, m.id ASC LIMIT ?',
        _freshness_params(now_iso) +
        (match_expr, project_id, agent_id) + detail_params +
        tuple(retention_params) + (fts_limit,)
    )
    return cur.fetchall()


def _fuse_rrf_candidates(fts_rows, vec_rows, k_rrf=60):
    """Reciprocal Rank Fusion over the FTS and vector lanes (id-deduped)."""
    fts_rank_map = {row[0]: idx for idx, row in enumerate(fts_rows)}
    vec_rank_map = {row[0]: idx for idx, row in enumerate(vec_rows)}
    doc_map = {}
    for row in fts_rows:
        doc_map[row[0]] = row
    for row in vec_rows:
        if row[0] not in doc_map:
            doc_map[row[0]] = row
    fused = []
    for mid in set(fts_rank_map) | set(vec_rank_map):
        score = 0.0
        if mid in fts_rank_map:
            score += 1.0 / (k_rrf + fts_rank_map[mid])
        if mid in vec_rank_map:
            score += 1.0 / (k_rrf + vec_rank_map[mid])
        r = doc_map[mid]
        # (id, scope, lifecycle, verification, freshness, content, owner, -score, fp)
        fused.append((r[0], r[1], r[2], r[3], r[4], r[5], r[6], -score, r[8]))

    def _fused_sort_key(c):
        lifecycle_order = _LIFECYCLE_ORDER.get(c[2], 2)
        verif_order = _VERIF_ORDER.get(c[3], 3)
        return (lifecycle_order, verif_order, c[7])

    fused.sort(key=_fused_sort_key)
    return fused


def _decay_ablated() -> bool:
    """MEMCORE_ABLATE_DECAY flag (ablation is eval-only); never raises."""
    try:
        return bool(_ablation.is_decay_ablated())
    except Exception:
        return False


def _fetch_query_vector(user_query):
    """Best-effort query embedding for the vector lane; None when unavailable."""
    try:
        return embedding.get_embedding(user_query, timeout=0.15)
    except Exception:
        return None


def _run_vector_lane(conn, project_id, agent_id, query_vec, limit,
                     detail_filter, detail_params, min_sim: float = 0.25):
    """Vector lane wrapper: never raises — degraded search beats no search."""
    if not query_vec:
        return []
    try:
        raw = _vector_search_lane(
            conn, project_id, agent_id, query_vec,
            limit=limit * DISTINCT_OVERFETCH,
            detail_filter=detail_filter, detail_params=detail_params
        )
        return [r for r in raw if -r[7] >= min_sim]
    except Exception:
        return []


def _run_summary_lane(conn, project_id, agent_id, raw_query, detail_filter,
                      detail_params, retention_tail, retention_params, now_iso,
                      limit):
    """Situational trigger (when_to_use) lexical matching lane."""
    if 'when_to_use' not in _table_cols(conn, 'memory_version'):
        return []
    tokens = [
        t.lower() for t in dict.fromkeys(_query_tokens(raw_query))
        if len(t) >= 2
    ][:16]
    if not tokens:
        return []
    match_exprs = ['(CASE WHEN instr(lower(v.when_to_use), ?) > 0 THEN 1 ELSE 0 END)' for _ in tokens]
    or_clauses = ' OR '.join(['instr(lower(v.when_to_use), ?) > 0'] * len(tokens))
    score_sql = '-(' + ' + '.join(match_exprs) + ')'
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        '       v.content, m.owner_agent_id, '
        '       ' + score_sql + ' AS rank, m.claim_fingerprint '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE v.when_to_use IS NOT NULL AND v.when_to_use != \'\' '
        '  AND m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        '  AND ' + _recall_tombstone_guard('m') + ' '
        '  AND (' + or_clauses + ') ' + detail_filter +
        'ORDER BY m.pinned DESC, ' +
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, " +
        "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, " +
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END" +
        retention_tail + ', ' +
        'rank ASC, m.updated_at DESC, m.id ASC LIMIT ?',
        _freshness_params(now_iso) +
        tuple(tokens) + (project_id, agent_id) + tuple(tokens) + detail_params +
        tuple(retention_params) + (min(500, limit * DISTINCT_OVERFETCH),)
    )
    return cur.fetchall()


def _is_single_source_stale(source_file: str | None, source_hash: str | None) -> bool:
    """Read-only SHA-256 check for a single source file (never mutates DB)."""
    if not source_file or not source_hash:
        return False
    import hashlib
    import pathlib
    try:
        p = pathlib.Path(source_file)
        if not p.is_absolute() or not p.is_file():
            return False
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        return digest.lower() != str(source_hash).lower()
    except Exception:
        return False


#: Public alias for single source file staleness check
is_single_source_stale = _is_single_source_stale


def _source_file_is_stale_readonly(conn, memory_ids) -> set[str]:
    """Read-only SHA-256 check for source-backed memories (never mutates DB)."""
    if not memory_ids:
        return set()
    marks = ','.join('?' for _ in memory_ids)
    rows = conn.execute(
        f'SELECT m.id, v.source_file, v.source_hash '
        f'FROM memory m JOIN memory_version v ON v.id = m.current_version_id '
        f'WHERE m.id IN ({marks}) AND v.source_file IS NOT NULL AND v.source_hash IS NOT NULL',
        tuple(memory_ids)
    ).fetchall()
    stale_ids: set[str] = set()
    for mid, source_file, source_hash in rows:
        if _is_single_source_stale(source_file, source_hash):
            stale_ids.add(mid)
    return stale_ids


def _filter_strict_rows(conn, rows):
    """Filter rows to strict authoritative current truth (accepted + current + verified)."""
    if not rows:
        return []
    stale_ids = _source_file_is_stale_readonly(conn, [r[0] for r in rows])
    out = []
    for r in rows:
        # r = (id, scope, lifecycle, verification, freshness, content, owner, rank, fp)
        if r[0] in stale_ids:
            continue
        if len(r) > 2 and r[2] != 'accepted':
            continue
        if len(r) > 3 and r[3] in ('unverified', 'disputed'):
            continue
        if len(r) > 4 and r[4] != 'current':
            continue
        out.append(r)
    return out


def _filter_changed_since(conn, rows, changed_since: str):
    """Filter candidate rows to those updated at or after changed_since."""
    if not rows or not changed_since:
        return rows
    ids = [r[0] for r in rows]
    marks = ','.join('?' for _ in ids)
    qualifying = {
        row[0] for row in conn.execute(
            f'SELECT m.id FROM memory m WHERE m.id IN ({marks}) '
            'AND datetime(m.updated_at) >= datetime(?)',
            (*ids, str(changed_since).strip())
        ).fetchall()
    }
    return [r for r in rows if r[0] in qualifying]


def _search_changed_since_only(conn, project_id, agent_id, changed_since,
                               detail_filter, detail_params, retention_tail,
                               retention_params, now_iso, limit, mode='standard'):
    """Delta query when user_query is empty and changed_since is provided."""
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        '       v.content, m.owner_agent_id, 0.0 AS rank, '
        '       m.claim_fingerprint '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        '  AND datetime(m.updated_at) >= datetime(?) '
        '  AND ' + _recall_tombstone_guard('m') + ' ' + detail_filter +
        'ORDER BY m.pinned DESC, ' +
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, " +
        "CASE m.verification WHEN 'user_authoritative' THEN 0 WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, " +
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END" +
        retention_tail + ', m.updated_at DESC, m.id ASC LIMIT ?',
        _freshness_params(now_iso) +
        (project_id, agent_id, str(changed_since).strip()) + detail_params +
        tuple(retention_params) + (limit,)
    )
    rows = cur.fetchall()
    if mode == 'strict':
        rows = _filter_strict_rows(conn, rows)
    return rows[:limit]


def _search_historical(conn, project_id, agent_id, user_query, raw_query,
                       detail_filter, detail_params, now_iso, limit,
                       as_of=None, changed_since=None):
    """Point-in-time (as_of) or full bitemporal version search (mode='historical')."""
    v_cols = _table_cols(conn, 'memory_version')
    has_valid_until = 'valid_until' in v_cols
    has_wtu = 'when_to_use' in v_cols
    tokens = [
        t.lower() for t in dict.fromkeys(_query_tokens(raw_query))
        if len(t) >= 2
    ][:16]
    where_extra = ''
    extra_params: list[Any] = []
    if as_of is not None:
        ts = str(as_of).strip()
        if has_valid_until:
            where_extra += (
                '  AND datetime(v.valid_from) <= datetime(?) '
                '  AND (v.valid_until IS NULL OR datetime(?) < datetime(v.valid_until)) '
            )
            extra_params.extend([ts, ts])
        else:
            where_extra += '  AND datetime(v.valid_from) <= datetime(?) '
            extra_params.append(ts)
    if changed_since is not None:
        where_extra += '  AND datetime(v.valid_from) >= datetime(?) '
        extra_params.append(str(changed_since).strip())
    if tokens:
        if has_wtu:
            token_clauses = [
                '(instr(lower(v.content), ?) > 0 OR (v.when_to_use IS NOT NULL AND instr(lower(v.when_to_use), ?) > 0))'
                for _ in tokens
            ]
            where_extra += '  AND (' + ' OR '.join(token_clauses) + ') '
            for t in tokens:
                extra_params.extend([t, t])
        else:
            token_clauses = ['instr(lower(v.content), ?) > 0' for _ in tokens]
            where_extra += '  AND (' + ' OR '.join(token_clauses) + ') '
            for t in tokens:
                extra_params.append(t)

    lifecycle_sql = (
        "CASE WHEN v.id = m.current_version_id THEN m.lifecycle ELSE 'superseded' END"
        if as_of is None else 'm.lifecycle'
    )
    freshness_sql = (
        "CASE WHEN v.id = m.current_version_id THEN " + _FRESHNESS_PROJECTION_SQL + " ELSE 'historical' END"
        if as_of is None else "'current'"
    )
    fresh_params = _freshness_params(now_iso) if as_of is None else ()
    cur = conn.execute(
        'SELECT m.id, m.scope, ' + lifecycle_sql + ' AS lifecycle, m.verification, '
        '       ' + freshness_sql + ' AS freshness, '
        '       v.content, m.owner_agent_id, 0.0 AS rank, '
        '       m.claim_fingerprint, v.valid_from, v.id '
        'FROM memory_version v '
        'JOIN memory m ON m.id = v.memory_id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle NOT IN ('rejected', 'disabled') " +
        where_extra + detail_filter +
        'ORDER BY CASE WHEN v.id = m.current_version_id THEN 0 ELSE 1 END, '
        'datetime(v.valid_from) DESC, m.id ASC LIMIT ?',
        fresh_params + (project_id, agent_id) + tuple(extra_params) + detail_params + (limit * 2,)
    )
    out = []
    for row in cur.fetchall():
        mid, scope, lc, verif, fresh, content, owner, rank, fp, _vf, _vid = row
        ver_fp = fingerprint(content)
        # Ensure active tombstones still block rejected claims unless searching historical versions explicitly
        if as_of is not None:
            tomb = conn.execute(
                'SELECT 1 FROM tombstone t WHERE t.claim_fingerprint = ? '
                'AND t.overridden_by IS NULL '
                "AND t.reason NOT LIKE 'corrected:%' "
                'AND datetime(t.created_at) <= datetime(?) '
                'AND (t.scope = \'global\' OR t.scope = ?)',
                (ver_fp, str(as_of).strip(), project_id)
            ).fetchone()
            if tomb is not None:
                continue
        out.append((mid, scope, lc, verif, fresh, content, owner, rank, ver_fp))
        if len(out) >= limit:
            break
    return out


def search(conn, project_id, agent_id, query, limit=20,
           scope_detail=None, query_vec=None, *,
           mode: str = 'standard', as_of: str | None = None,
           changed_since: str | None = None):
    """FTS5 + Vector hybrid search over memory content, scope-enforced in SQL.

    Deterministic rank: FTS bm25 + Vector cosine similarity via Reciprocal
    Rank Fusion (RRF, k=60) + pinned + lifecycle/verification/freshness
    + continuous retention (salience * exp(-lambda*age) + reinforcement),
    then updated_at/id tie-breaks. MEMCORE_ABLATE_DECAY=1 neutralizes
    exactly the retention term (legacy CASE+bm25 ordering).
    For non-ASCII queries, try an exact Unicode substring match first because
    SQLite unicode61 does not segment Thai/CJK natural-language words well.

    scope_detail narrows to one private subdivision (e.g. 'skill:x'); None
    means no narrowing. Project-scope rows never carry a detail tag.
    mode supports 'standard' (default), 'strict' (accepted + current + non-disputed),
    and 'historical' (includes superseded versions or point-in-time as_of).
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        raise MemCoreError('search limit must be an integer')
    if limit < 1:
        raise MemCoreError('search limit must be >= 1')
    limit = min(limit, 500)
    mode_norm = str(mode or 'standard').strip().lower()
    if mode_norm not in ('standard', 'strict', 'historical'):
        raise MemCoreError(f'invalid search mode: {mode}; must be standard|strict|historical')

    user_query = str(query or '').strip()
    has_query = bool(user_query and any(c.isalnum() for c in user_query))
    if not has_query and as_of is None and changed_since is None and mode_norm != 'historical':
        return []

    now_iso = _eval_now_iso()
    retention_expr, retention_params = _retention_order(
        conn, not _decay_ablated(), alias='m')
    retention_tail = (
        (', ' + retention_expr + ' DESC') if retention_expr else '')
    raw_query = _expand_aliases(user_query) if has_query else ''
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    detail_filter, detail_params = _search_detail_filter(conn, scope_detail)
    if detail_filter is None:
        return []

    if as_of is not None or mode_norm == 'historical':
        if not has_query and as_of is None and changed_since is None:
            return []
        return _search_historical(
            conn, project_id, agent_id, user_query, raw_query,
            detail_filter, detail_params, now_iso, limit,
            as_of=as_of, changed_since=changed_since
        )

    if not has_query and changed_since is not None:
        return _search_changed_since_only(
            conn, project_id, agent_id, changed_since,
            detail_filter, detail_params, retention_tail,
            retention_params, now_iso, limit, mode=mode_norm
        )

    substr_terms = _extract_non_ascii_substr_terms(user_query)

    # Lane 3.3: Local vector search lane (semantic similarity)
    if query_vec is None:
        query_vec = _fetch_query_vector(user_query)
    vec_rows = _run_vector_lane(
        conn, project_id, agent_id, query_vec, limit,
        detail_filter, detail_params)

    match_expr = _fts_query(raw_query)
    fts_rows = []
    if match_expr:
        fts_rows = _run_fts_lane(
            conn, project_id, agent_id, match_expr, detail_filter,
            detail_params, retention_tail, retention_params, now_iso,
            min(500, limit * DISTINCT_OVERFETCH)
        )

    # Lane 3.4: Situational trigger (when_to_use / v.summary) lane
    summary_rows = _run_summary_lane(
        conn, project_id, agent_id, raw_query, detail_filter,
        detail_params, retention_tail, retention_params, now_iso, limit
    )
    if summary_rows:
        seen_fts = {r[0] for r in fts_rows}
        for sr in summary_rows:
            if sr[0] not in seen_fts:
                fts_rows.append(sr)
                seen_fts.add(sr[0])

    # Substring lane runs ONLY as a fallback, and only when the ranked lane did
    # not already supply enough DISTINCT claims.
    exact_rows = []
    if substr_terms and len({row[8] for row in fts_rows}) < limit:
        exact_rows = _run_substring_lane(
            conn, project_id, agent_id, substr_terms, detail_filter,
            detail_params, retention_tail, retention_params, now_iso, limit)

    if not vec_rows and not fts_rows and not exact_rows:
        return []
    # Query-side analysis for the demotion pass: computed once here, so the
    # incompatibility check never re-tokenizes the query per row (C2).
    from memcore import contradiction as _qd
    query_tokens = _qd._tokens(user_query)
    query_tok = set(query_tokens)
    query_numbers = _qd.numbers(user_query)

    fetch_cap = limit * 2 if (mode_norm == 'strict' or changed_since is not None) else limit
    if not vec_rows:
        results = _merge_exact_fallback(
            fts_rows, exact_rows, fetch_cap, user_query,
            query_tok=query_tok, query_numbers=query_numbers,
            query_tokens=query_tokens)
    else:
        fused = _fuse_rrf_candidates(fts_rows, vec_rows)
        results = _merge_exact_fallback(
            fused, exact_rows, fetch_cap, user_query,
            query_tok=query_tok, query_numbers=query_numbers,
            query_tokens=query_tokens)

    if results:
        stale_ids = _source_file_is_stale_readonly(conn, [r[0] for r in results])
        if stale_ids:
            refreshed = []
            for r in results:
                if r[0] in stale_ids and len(r) > 4:
                    r_list = list(r)
                    r_list[4] = 'stale'
                    refreshed.append(tuple(r_list))
                else:
                    refreshed.append(r)
            results = refreshed

    if changed_since is not None:
        results = _filter_changed_since(conn, results, changed_since)
    if mode_norm == 'strict':
        results = _filter_strict_rows(conn, results)
    return results[:limit]


def finalize_recall(conn, rows):
    """Apply recall side effects and freshness labels to already selected rows.

    Shared by the SDK recall path and integrations that assemble rows from
    more than one governed source, such as pinned plus query-matched memories.
    Best-effort writes ensure read-only queries still return their candidates.
    """
    rows = list(rows or [])
    if not rows:
        return []

    recall_ids = [r[0] for r in rows]
    flipped = set()
    try:
        record_recall(conn, recall_ids)
        flipped = set(check_source_staleness(conn, recall_ids))
    except Exception:
        # Best-effort: failures on read-only databases or temporary locks
        # must never break memory retrieval for the caller.
        pass
    out = []
    for r in rows:
        row_list = list(r)
        if len(row_list) > 4 and row_list[0] in flipped:
            row_list[4] = 'stale'
        if len(row_list) > 5:
            out.append(tuple(row_list))
        else:
            out.append(r)
    return out


def recall(conn, project_id, agent_id, query, limit=20,
           scope_detail=None, query_vec=None, get_head=None, repo_root=None,
           *, mode: str = 'standard', as_of: str | None = None,
           changed_since: str | None = None):
    """The unified, deep memory recall lifecycle.

    Encapsulates candidate search, access reinforcement, source staleness
    verification, and final freshness resolution into a single interface.
    Returns list of candidate tuples with finalized freshness state. The legacy
    ``get_head`` and ``repo_root`` keywords are accepted for call compatibility;
    source-file hashes now determine freshness.
    """
    rows = search(
        conn, project_id, agent_id, query, limit=limit,
        scope_detail=scope_detail, query_vec=query_vec,
        mode=mode, as_of=as_of, changed_since=changed_since
    )
    finalized = finalize_recall(conn, rows)
    if str(mode or 'standard').strip().lower() == 'strict':
        finalized = [r for r in finalized if len(r) > 4 and r[2] == 'accepted' and r[4] == 'current']
    return finalized


#: How much deeper than ``limit`` search() reads before collapsing duplicate
#: claim copies. The fleet corroborates by re-writing one claim from several
#: agents, so a shallow window can be entirely copies of a single fact; reading
#: a few times deeper lets distinct claims fill the freed slots.
#: Measured sweep on the live-store copy: 1 -> mean result overlap 0.86 and
#: SLOWER (fewer distinct rows to sort); 2 -> identical results on every probe;
#: 3 -> no gain over 2. So 2 is the value: same recall, less work.
DISTINCT_OVERFETCH = 2

#: Cap on `instr(v.content, ?)` OR-terms in the Thai substring lane. Each term
#: is a full table scan of the joined version rows, so this is the single
#: biggest lever on Thai-query latency (measured: a 32-term glued Thai query
#: costs ~5ms, ASCII ~1ms). Lower it if recall ever degrades for long queries.
MAX_SUBSTR_TERMS = 32


def _collapse_duplicate_claims(rows, limit=None):
    """Keep the first (best-ranked) copy of each claim; fold the rest behind it.

    The fleet corroborates by re-writing the same claim from several agents, so
    one true fact arrives as N identical rows. Without this the result window
    fills with copies of a single fact and the remaining slots carry nothing new
    — measured on the live store, ``HERMES_PROFILE`` returned 4 rows carrying 1
    distinct claim. Folding is stable: the canonical (best-ranked) copy keeps
    its position and every duplicate moves behind all distinct claims, so no
    claim is ever lost and ranking order is preserved.
    """
    rows = list(rows or [])
    if len(rows) < 2:
        return rows[:limit] if limit else rows
    keep = []
    folded = []
    seen = set()
    for row in rows:
        fp = row[8] if len(row) > 8 else None
        if not fp:
            try:
                fp = fingerprint(
                    ' '.join(str(row[5] if len(row) > 5 else row).split()))
            except Exception:
                fp = None
        if fp is not None:
            if fp in seen:
                folded.append(row)
                continue
            seen.add(fp)
        keep.append(row)
    out = keep + folded
    return out[:limit] if limit else out


#: Age thresholds mirroring apply_freshness_decay's defaults, applied when
#: projecting the freshness label at read time.
FRESHNESS_AGING_DAYS = 30
FRESHNESS_STALE_DAYS = 90


def _demote_numeric_mismatch(query, rows, query_tok=None, query_numbers=None,
                             query_tokens=None):
    """Stable-partition hits whose content contradicts the query's values.

    A query naming a concrete value ("port 8080", "page size 100") should not
    lead with a memory stating a different value for the same subject. Lexical
    recall has no polarity, so demotion uses three surface signals: disjoint
    standalone numbers, exclusive-choice members, and a contiguous phrase the
    fact explicitly negates ("A B does not ...") while the query affirms it.

    Only reorders rows it actually demotes; ties keep their original order, so
    an unrelated hit can never be pushed above a row that merely mentions a
    number by accident (e.g. "9router", "v1") — those share no standalone
    digits with the query and stay put.

    A window whose rows are ALL demoted has no compatible answer: leading (or
    only) with a contradiction reads as confirmation of the wrong value, so
    it collapses to [] instead. A single demoted candidate therefore yields
    nothing rather than a wrong-value hit.

    ``query_tok``/``query_numbers``/``query_tokens`` may be precomputed once
    by the caller: the per-row check never re-tokenizes the query (C2).
    """
    rows = list(rows or [])
    if not rows:
        return rows
    from memcore import contradiction as _cd
    if query_tokens is None:
        query_tokens = _cd._tokens(query)
    if query_numbers is None:
        query_numbers = _cd.numbers(query)
    if query_tok is None:
        query_tok = set(query_tokens)
    phrase_pairs = _cd._query_phrase_pairs(query_tokens)
    choice_sets = [excl for excl in _cd._EXCLUSIVE_SETS if query_tok & excl]
    # Numeric demotion needs numbers on the query side; the incompatibility
    # check below additionally fires on exclusive-choice words or a negated
    # query phrase, so a query without numbers still proceeds when either of
    # those signals is present. Phrase demotion only applies to affirmative
    # queries (a negated query already states what it expects).
    phrase_active = bool(phrase_pairs) and _cd.polarity(query) == 0
    if not query_numbers and not choice_sets and not phrase_active:
        return rows
    keep, demoted = [], []
    for row in rows:
        content = row[5] if len(row) > 5 else row
        if query_numbers:
            row_numbers = _cd.numbers(content)
            if row_numbers and not (row_numbers & query_numbers):
                demoted.append(row)
                continue
        # Cheap pre-screen before the token-level check: a row can only fire
        # via an exclusive member substring or a negative marker; skipping
        # the rest avoids tokenizing every candidate row.
        if not _cd._row_may_contradict(content, bool(choice_sets), phrase_active):
            keep.append(row)
            continue
        if _cd.query_fact_incompatible(
                query, content, query_tok=query_tok,
                query_numbers=query_numbers, phrase_pairs=phrase_pairs,
                query_tokens=query_tokens, choice_sets=choice_sets):
            demoted.append(row)
        else:
            keep.append(row)
    if not keep:
        return []
    return keep + demoted


def conflict_memories(conn, project_id, agent_id):
    """Readable conflict memories for one member; private scope never leaks."""
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    cur = conn.execute(
        'SELECT m.id, m.owner_agent_id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        "WHERE m.project_id = ? AND m.lifecycle = 'conflict' "
        "  AND (m.scope='project' OR m.owner_agent_id=?) "
        '  AND ' + _recall_tombstone_guard('m') + ' '
        'ORDER BY m.updated_at DESC, m.id ASC',
        (project_id, agent_id)
    )
    return cur.fetchall()


def superseded_history(conn, memory_id, agent_id):
    """Readable versions of one memory, oldest first; scope enforced in SQL."""
    if not isinstance(memory_id, str) or not isinstance(agent_id, str):
        return []
    cur = conn.execute(
        'SELECT v.id, v.content, v.created_at, v.supersedes_version_id '
        'FROM memory m JOIN memory_version v ON v.memory_id=m.id '
        'WHERE m.id=? '
        '  AND EXISTS (SELECT 1 FROM project_membership pm '
        '              WHERE pm.project_id=m.project_id AND pm.agent_id=?) '
        "  AND (m.scope='project' OR m.owner_agent_id=?) "
        'ORDER BY v.created_at ASC, v.rowid ASC',
        (memory_id, agent_id, agent_id)
    )
    return cur.fetchall()


def get_memories(conn, project_id, agent_id, memory_ids, *,
                 include_history=False, include_evidence=False,
                 record_access=True):
    """Layer-3 progressive disclosure: fetch full governed records by ID list.

    Enforces SQL WHERE project membership, private-scope ownership, and active
    tombstone guards. Performs live read-time SHA-256 provenance checks on
    source_file-backed rows and preserves caller ID order.
    """
    if not memory_ids or not isinstance(memory_ids, (list, tuple, set)):
        return []
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    clean_ids = []
    seen = set()
    for mid in memory_ids:
        if isinstance(mid, str):
            s = mid.strip()
            if s and s not in seen:
                seen.add(s)
                clean_ids.append(s)
    if not clean_ids:
        return []
    clean_ids = clean_ids[:50]
    placeholders = ','.join('?' for _ in clean_ids)
    v_cols = _table_cols(conn, 'memory_version')
    wtu_col = 'v.when_to_use' if 'when_to_use' in v_cols else 'NULL'
    now_iso = _eval_now_iso()
    cur = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
        f'       v.content, m.owner_agent_id, m.type, {wtu_col} AS when_to_use, '
        '       v.source_file, v.source_commit, v.source_hash, '
        '       v.valid_from, v.valid_until, m.pinned, m.critical, '
        '       COALESCE(m.recall_count, 0), m.updated_at '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        f'WHERE m.project_id = ? AND m.id IN ({placeholders}) '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        '  AND ' + _recall_tombstone_guard('m'),
        _freshness_params(now_iso) + (project_id, *clean_ids, agent_id),
    )
    by_id = {}
    for row in cur.fetchall():
        (mid, scope, lifecycle, verification, freshness, content, owner,
         mtype, when_to_use, source_file, source_commit, source_hash,
         valid_from, valid_until, pinned, critical, recall_count, updated_at) = row
        effective_freshness = freshness
        if source_file and _is_single_source_stale(source_file, source_hash):
            effective_freshness = 'stale'
        item = {
            'id': mid,
            'scope': scope,
            'lifecycle': lifecycle,
            'verification': verification,
            'freshness': effective_freshness,
            'type': mtype,
            'content': content,
            'when_to_use': when_to_use,
            'owner': owner,
            'source_file': source_file,
            'source_commit': source_commit,
            'source_hash': source_hash,
            'valid_from': valid_from,
            'valid_until': valid_until,
            'pinned': bool(pinned),
            'critical': bool(critical),
            'recall_count': int(recall_count or 0),
            'updated_at': updated_at,
        }
        if include_history:
            hist = superseded_history(conn, mid, agent_id)
            item['history'] = [
                {
                    'version_id': h[0],
                    'content': h[1],
                    'created_at': h[2],
                    'supersedes_version_id': h[3],
                }
                for h in hist
            ]
        if include_evidence:
            ev_rows = conn.execute(
                'SELECT e.id, e.kind, e.locator, e.excerpt '
                'FROM evidence_link el '
                'JOIN evidence e ON e.id = el.evidence_id '
                'JOIN memory m ON m.current_version_id = el.memory_version_id '
                'WHERE m.id = ? ORDER BY e.created_at ASC',
                (mid,),
            ).fetchall()
            item['evidence'] = [
                {'id': er[0], 'kind': er[1], 'locator': er[2], 'excerpt': er[3]}
                for er in ev_rows
            ]
        by_id[mid] = item

    ordered = [by_id[mid] for mid in clean_ids if mid in by_id]
    if record_access and ordered:
        try:
            with store.write_tx(conn):
                for item in ordered:
                    conn.execute(
                        'UPDATE memory SET recall_count = COALESCE(recall_count, 0) + 1, '
                        'last_recalled = ? WHERE id = ?',
                        (now_iso, item['id']),
                    )
                    item['recall_count'] += 1
        except (sqlite3.OperationalError, store.StoreError):
            pass
    return ordered


def pack_search_results(results, max_tokens=None, preview_chars=180):
    """Pack search result dicts within a token budget using progressive disclosure.

    When max_tokens is None or <= 0, returns results unchanged.
    Otherwise estimates token cost (~4 chars/token + 12 metadata tokens/row).
    Full rows that fit are kept in full; rows whose full body would exceed the
    remaining budget are downgraded to a Salience L1 preview if the preview fits.
    """
    if not results or max_tokens is None:
        return list(results or [])
    try:
        budget_tok = int(max_tokens)
    except (TypeError, ValueError):
        return list(results)
    if budget_tok <= 0:
        return list(results)

    p_chars = min(max(int(preview_chars or 180), 40), 500)
    packed = []
    used_tok = 0
    meta_overhead_tok = 12

    def _est_tokens(text):
        return max(1, (len(str(text or '')) + 3) // 4)

    def _make_preview(raw_text, limit_chars):
        collapsed = ' '.join(str(raw_text or '').split())
        if len(collapsed) <= limit_chars:
            return collapsed, False
        cut = collapsed[:limit_chars]
        sp = cut.rfind(' ')
        if sp > limit_chars // 2:
            cut = cut[:sp]
        return cut.rstrip() + '…', True

    for idx, item in enumerate(results):
        row = dict(item)
        raw_content = str(row.get('content') or '')
        full_cost = meta_overhead_tok + _est_tokens(raw_content)
        if used_tok + full_cost <= budget_tok:
            row['estimated_tokens'] = full_cost
            packed.append(row)
            used_tok += full_cost
            continue
        # Try downgrading to preview if remaining budget can hold a preview
        rem_tok = budget_tok - used_tok - meta_overhead_tok
        if rem_tok >= 10:
            avail_chars = min(p_chars, rem_tok * 4)
            if avail_chars >= 40:
                prev_text, trunc = _make_preview(raw_content, avail_chars)
                prev_cost = meta_overhead_tok + _est_tokens(prev_text)
                row['content'] = prev_text
                row['content_truncated'] = True if trunc or len(prev_text) < len(raw_content) else bool(row.get('content_truncated'))
                row['estimated_tokens'] = prev_cost
                packed.append(row)
                used_tok += prev_cost
                continue
        if idx == 0 and not packed:
            # Always emit at least the top-1 hit, truncated to fit budget
            avail_chars = max(40, max(1, budget_tok - meta_overhead_tok) * 4)
            prev_text, trunc = _make_preview(raw_content, avail_chars)
            row['content'] = prev_text
            row['content_truncated'] = True if trunc or len(prev_text) < len(raw_content) else bool(row.get('content_truncated'))
            row['estimated_tokens'] = meta_overhead_tok + _est_tokens(prev_text)
            packed.append(row)
        break
    return packed


# ————————————————————————————————————————————————————————————————————————————————————————
