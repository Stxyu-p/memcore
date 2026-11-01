"""MemCore recall — retrieval lanes: substring, FTS5 BM25, summary, retention."""
from __future__ import annotations

from . import ablation as _ablation
from . import store
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






def _decay_ablated() -> bool:
    """MEMCORE_ABLATE_DECAY flag (ablation is eval-only); never raises."""
    try:
        return bool(_ablation.is_decay_ablated())
    except Exception:
        return False









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
