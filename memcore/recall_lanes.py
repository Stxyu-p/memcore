"""MemCore recall — retrieval lanes: substring, FTS5 BM25, dense vector, summary, RRF fusion."""
from __future__ import annotations

import array
import math
import operator

from . import ablation as _ablation
from . import embedding
from . import store
from .store_vectors import pack_binary_vector, hamming_distance
from .write import _eval_now_iso
from .write import _table_cols

from .recall_decay import (
    _FRESHNESS_PROJECTION_SQL,
    _LIFECYCLE_ORDER,
    _VERIF_ORDER,
    _freshness_params,
)

from .recall_text import (
    _query_tokens,
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
    fresh_params = _freshness_params(now_iso)
    q_blob = q_arr.tobytes()

    # Fast path: in-process SIMD vector distance via sqlite-vec when available
    try:
        sql_simd = (
            'SELECT m.id, m.scope, m.lifecycle, m.verification, '
            '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
            '       v.content, m.owner_agent_id, '
            '       (vec_distance_cosine(e.vector, ?) - 1.0) AS rank, '
            '       m.claim_fingerprint, m.pinned '
            'FROM memory m '
            'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
            'JOIN memory_embedding e ON e.version_id = v.id AND e.memory_id = m.id '
            'WHERE m.project_id = ? '
            "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
            "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
            '  AND ' + _recall_tombstone_guard('m') + ' ' + detail_filter
        )
        params_simd = fresh_params + (q_blob, project_id, agent_id) + detail_params
        candidates = conn.execute(sql_simd, params_simd).fetchall()
    except Exception:
        # Fallback: pure Python scan with Stage-1 Hamming pre-filter when available
        has_bin = 'binary_vector' in _table_cols(conn, 'memory_embedding')
        bin_col = ', e.binary_vector' if has_bin else ''
        sql = (
            'SELECT m.id, m.scope, m.lifecycle, m.verification, '
            '       ' + _FRESHNESS_PROJECTION_SQL + ' AS freshness, '
            '       v.content, m.owner_agent_id, e.vector, '
            '       m.claim_fingerprint, m.pinned' + bin_col + ' '
            'FROM memory m '
            'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
            'JOIN memory_embedding e ON e.version_id = v.id AND e.memory_id = m.id '
            'WHERE m.project_id = ? '
            "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
            "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
            '  AND ' + _recall_tombstone_guard('m') + ' ' + detail_filter
        )
        params = fresh_params + (project_id, agent_id) + detail_params
        cur = conn.execute(sql, params)
        raw_rows = cur.fetchall()

        prune_cutoff = max(limit * 4, 30)
        if has_bin and len(raw_rows) > prune_cutoff:
            q_bin = pack_binary_vector(query_vec)
            scored_rows = []
            for r in raw_rows:
                bin_blob = r[10] if len(r) > 10 else None
                h_dist = hamming_distance(q_bin, bin_blob) if bin_blob else 9999
                scored_rows.append((h_dist, r))
            scored_rows.sort(key=lambda x: x[0])
            eval_rows = [x[1] for x in scored_rows[:prune_cutoff]]
        else:
            eval_rows = raw_rows

        candidates = []
        for row in eval_rows:
            mid, scope, lifecycle, verif, freshness, content, owner, blob, fp, pinned = row[:10]
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

    _FRESH_ORDER = {'current': 0, 'aging': 1, 'stale': 2}

    def _fused_sort_key(c):
        lifecycle_order = _LIFECYCLE_ORDER.get(c[2], 2)
        verif_order = _VERIF_ORDER.get(c[3], 3)
        fresh_order = _FRESH_ORDER.get(c[4], 2)
        return (lifecycle_order, verif_order, fresh_order, c[7], fts_rank_map.get(c[0], 999))

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



#: How much deeper than ``limit`` search() reads before collapsing duplicate
#: claim copies. The fleet corroborates by re-writing one claim from several
#: agents, so a shallow window can be entirely copies of a single fact; reading
#: a few times deeper lets distinct claims fill the freed slots.
#: Measured sweep on the live-store copy: 1 -> mean result overlap 0.86 and
#: SLOWER (fewer distinct rows to sort); 2 -> identical results on every probe;
#: 3 -> no gain over 2. So 2 is the value: same recall, less work.
DISTINCT_OVERFETCH = 2
