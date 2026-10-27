"""
MemCore — recall & search engine.

Hybrid FTS5 BM25 + dense vector RRF search, Salience L1 previews,
freshness/decay ranking, and scope-enforced reads.
"""
from __future__ import annotations

import sqlite3

from . import store
from .write import MemCoreError
from .write import _eval_now_iso
from .write import _membership_role
from .write import _table_cols
from .write import check_source_staleness
from .write import fingerprint
from typing import Any

from .recall_decay import (
    _FRESHNESS_PROJECTION_SQL,
    _freshness_params,
    _has_recall_cols,
    _retention_order,
)

from .recall_lanes import (
    DISTINCT_OVERFETCH,
    _decay_ablated,
    _fetch_query_vector,
    _fuse_rrf_candidates,
    _recall_tombstone_guard,
    _run_fts_lane,
    _run_substring_lane,
    _run_summary_lane,
    _run_vector_lane,
    _search_detail_filter,
)

from .recall_text import (
    _expand_aliases,
    _extract_non_ascii_substr_terms,
    _fts_query,
    _query_tokens,
)

from .recall_decay import (
    _DECAY_LAMBDA_DEFAULT,
    _DECAY_SIGMA,
    _DECAY_MU,
    _DECAY_HALF_LIFE_DAYS_BY_TYPE,
    _DECAY_SALIENCE_BY_VERIFICATION,
    _DECAY_SALIENCE_BY_LIFECYCLE,
    _decay_lambda_sql,
    _decay_salience_sql,
    _DECAY_LAMBDA_SQL_M,
    _DECAY_SALIENCE_SQL_M,
    _RETENTION_AGE_SQL_M,
    _RETENTION_ACCESS_SQL_M,
    _RETENTION_EXPR_WITH_RECALL_M,
    _RETENTION_EXPR_NO_RECALL_M,
    _has_recall_cols,
    _retention_order,
    _FRESHNESS_PROJECTION_SQL,
    _freshness_params,
    _LIFECYCLE_ORDER,
    _VERIF_ORDER,
    FRESHNESS_AGING_DAYS,
    FRESHNESS_STALE_DAYS,
)

from .recall_lanes import (
    _recall_tombstone_guard,
    _run_substring_lane,
    cosine_similarity,
    _vector_search_lane,
    _search_detail_filter,
    _run_fts_lane,
    _fuse_rrf_candidates,
    _decay_ablated,
    _fetch_query_vector,
    _run_vector_lane,
    _run_summary_lane,
    DISTINCT_OVERFETCH,
)

from .recall_text import (
    _FLEET_ALIASES,
    _expand_aliases,
    _thai_bigrams,
    _query_tokens,
    _fts_query,
    _extract_non_ascii_substr_terms,
    MAX_SUBSTR_TERMS,
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
    v_cols = _table_cols(conn, 'memory_version')
    if 'valid_until' not in v_cols:
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
