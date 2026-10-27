"""MemCore journal — observability and retention: stats, age, autonomy, sweeps."""
from . import core
from . import store




def _journal_filters(project_id, agent_id):
    """WHERE fragments + params for an optional (project, agent) scope."""
    filters, params = [], []
    if project_id is not None:
        filters.append('project_id=?')
        params.append(project_id)
    if agent_id is not None:
        filters.append('agent_id=?')
        params.append(agent_id)
    return filters, params



def _journal_grouped(conn, filters, params, column, *, extra=''):
    """One GROUP BY census with the optional scope filters applied."""
    clauses = list(filters)
    if extra:
        clauses.append(extra)
    local_where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    return dict(conn.execute(
        f'SELECT {column}, COUNT(*) FROM ingest_event{local_where} '
        f'GROUP BY {column} ORDER BY {column}', list(params)
    ).fetchall())



def _journal_pending_counts(conn, pending_where, pending_params):
    """Pending-queue census: semantic backlog, builtin noise, oldest age."""
    semantic_pending = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + pending_where +
        " AND decision IN ('semantic_review_required','semantic_deferred')",
        pending_params
    ).fetchone()[0]
    # v0.7 hygiene (spec Part 5): old unresolved builtin rows are sweepable
    # via `journal-sweep`, not alarming. Only YOUNG (<7d) builtin noise
    # escalates to operator_attention.
    builtin_noise = (
        " AND decision LIKE 'builtin_memory_%' "
        "AND (decision LIKE '%_unresolved_target' "
        "OR decision LIKE '%_missing_old_text' "
        "OR decision='builtin_memory_replace_missing_content' "
        "OR decision LIKE '%_requires_review')"
    )
    unresolved_builtin = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + pending_where + builtin_noise,
        pending_params
    ).fetchone()[0]
    unresolved_builtin_young = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + pending_where + builtin_noise +
        " AND datetime(created_at) >= datetime('now', '-7 days')",
        pending_params
    ).fetchone()[0]
    oldest = conn.execute(
        'SELECT MIN(created_at) FROM ingest_event' + pending_where,
        pending_params
    ).fetchone()[0]
    oldest_age_seconds = None
    if oldest:
        age = conn.execute(
            "SELECT MAX(0, CAST(strftime('%s','now') AS INTEGER) - "
            "CAST(strftime('%s', ?) AS INTEGER))",
            (oldest,)
        ).fetchone()[0]
        oldest_age_seconds = int(age) if age is not None else None
    return {
        'semantic_review_pending': semantic_pending,
        'unresolved_builtin_mutations': unresolved_builtin,
        'unresolved_builtin_young': unresolved_builtin_young,
        'sweepable_builtin': unresolved_builtin - unresolved_builtin_young,
        'oldest_pending_at': oldest,
        'oldest_pending_age_seconds': oldest_age_seconds,
    }



def _journal_analysis_counts(conn, project_id, agent_id):
    """(total, by_verdict) for the ingest_analysis table under one scope."""
    filters, params = [], []
    if project_id is not None:
        filters.append('e.project_id=?')
        params.append(project_id)
    if agent_id is not None:
        filters.append('e.agent_id=?')
        params.append(agent_id)
    where = (' WHERE ' + ' AND '.join(filters)) if filters else ''
    total = conn.execute(
        'SELECT COUNT(*) FROM ingest_analysis a '
        'JOIN ingest_event e ON e.id=a.event_id' + where, params
    ).fetchone()[0]
    by_verdict = dict(conn.execute(
        'SELECT a.verdict, COUNT(*) FROM ingest_analysis a '
        'JOIN ingest_event e ON e.id=a.event_id' + where +
        ' GROUP BY a.verdict ORDER BY a.verdict', params
    ).fetchall())
    return total, by_verdict



def _journal_health(by_status, unresolved_builtin_young, semantic_pending,
                    unresolved_builtin):
    """Health ladder: failed > young builtin noise > semantic > sweep > ok."""
    if by_status.get('failed', 0):
        return 'failed'
    if unresolved_builtin_young:
        return 'operator_attention'
    if semantic_pending:
        return 'review_pending'
    if unresolved_builtin:
        return 'sweepable'
    return 'ok'



def journal_stats(conn, project_id=None, agent_id=None):
    """Return a content-free operational snapshot of the ingest journal.

    The snapshot deliberately aggregates metadata only. It never returns raw user,
    assistant, candidate, rationale, or event metadata content, making it suitable
    for health checks and dashboards that should not expose historical prompts.
    """
    filters, params = _journal_filters(project_id, agent_id)
    if project_id is not None and agent_id is not None:
        core._require_membership(conn, project_id, agent_id)

    where = (' WHERE ' + ' AND '.join(filters)) if filters else ''
    total = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + where, params
    ).fetchone()[0]
    by_status = _journal_grouped(conn, filters, params, 'status')
    pending_by_decision = _journal_grouped(
        conn, filters, params, "COALESCE(decision, 'unclassified')",
        extra="status='pending'"
    )
    pending_by_event_type = _journal_grouped(
        conn, filters, params, 'event_type', extra="status='pending'")

    pending_where = ' WHERE ' + ' AND '.join(
        list(filters) + ["status='pending'"])
    pending = _journal_pending_counts(conn, pending_where, list(params))
    analysis_total, analysis_by_verdict = _journal_analysis_counts(
        conn, project_id, agent_id)
    health = _journal_health(
        by_status, pending['unresolved_builtin_young'],
        pending['semantic_review_pending'],
        pending['unresolved_builtin_mutations'])

    return {
        'health': health,
        'total_events': total,
        'by_status': by_status,
        'pending_by_decision': pending_by_decision,
        'pending_by_event_type': pending_by_event_type,
        'semantic_review_pending': pending['semantic_review_pending'],
        'unresolved_builtin_mutations': pending['unresolved_builtin_mutations'],
        'unresolved_builtin_young': pending['unresolved_builtin_young'],
        'sweepable_builtin': pending['sweepable_builtin'],
        'oldest_pending_at': pending['oldest_pending_at'],
        'oldest_pending_age_seconds': pending['oldest_pending_age_seconds'],
        'analysis': {
            'total': analysis_total,
            'by_verdict': analysis_by_verdict,
        },
    }



#: Audit actions counted by the autonomy observability surface (Task 4).
#: Content-free: only action names and day buckets, never memory text.
AUTONOMY_AUDIT_ACTIONS = (
    'auto_corrob_accept',
    'auto_golden_promote',
    'auto_accept',
    'duplicate-merge',
    'contradiction-hold',
)



def journal_age(conn, project_id=None, agent_id=None):
    """Content-free age of the oldest pending journal row, in whole days.

    Returns {'oldest_pending_days': int | None} — None when nothing is
    pending. Whole days, not seconds: the seed-week question is whether
    anything has been stuck for days, and seconds-level precision would
    make the doctor line flap between runs.
    """
    filters = ["status='pending'"]
    params = []
    if project_id is not None:
        filters.append('project_id=?')
        params.append(project_id)
    if agent_id is not None:
        filters.append('agent_id=?')
        params.append(agent_id)
    where = ' WHERE ' + ' AND '.join(filters)
    oldest = conn.execute(
        'SELECT MIN(created_at) FROM ingest_event' + where, params
    ).fetchone()[0]
    if not oldest:
        return {'oldest_pending_days': None}
    days = conn.execute(
        "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)",
        (oldest,),
    ).fetchone()[0]
    return {'oldest_pending_days': max(0, int(days))}



def autonomy_per_day(conn, *, days=30):
    """Content-free per-day counts of autonomy audit actions.

    Returns {YYYY-MM-DD: {action: count}}. Only the AUTONOMY_AUDIT_ACTIONS
    strings are counted; no memory text, no detail payloads.
    """
    marks = ','.join('?' for _ in AUTONOMY_AUDIT_ACTIONS)
    rows = conn.execute(
        'SELECT date(created_at), action, COUNT(*) FROM audit_event '
        f"WHERE action IN ({marks}) "
        "AND datetime(created_at) >= datetime('now', '-' || ? || ' days') "
        'GROUP BY 1, 2 ORDER BY 1, 2',
        (*AUTONOMY_AUDIT_ACTIONS, int(days)),
    ).fetchall()
    per_day = {}
    for day, action, count in rows:
        per_day.setdefault(day, {})[action] = count
    return per_day



#: Pending decisions a builtin stale-mutation sweep may auto-dismiss.
_STALE_BUILTIN_DECISIONS = (
    'builtin_memory_replace_unresolved_target',
    'builtin_memory_remove_unresolved_target',
    'builtin_memory_replace_missing_old_text',
    'builtin_memory_remove_missing_old_text',
    'builtin_memory_replace_missing_content',
)



def _scan_stale_builtin(conn, days):
    """Shared scan for stale builtin mutations (dry-run and apply, C1)."""
    if days < 0:
        raise core.MemCoreError('builtin stale days must be >= 0')
    marks = ','.join('?' for _ in _STALE_BUILTIN_DECISIONS)
    return conn.execute(
        'SELECT id, project_id, agent_id, decision FROM ingest_event '
        "WHERE status='pending' AND decision IN (" + marks + ') '
        "AND datetime(created_at) < datetime('now', '-' || ? || ' days') "
        'ORDER BY created_at, id',
        (*_STALE_BUILTIN_DECISIONS, days),
    ).fetchall()



def auto_dismiss_stale_builtin(conn, days=7, actor_agent_id=None, *,
                               dry_run=False):
    """Auto-dismiss builtin mutation events stuck longer than `days`.

    Old unresolved builtin targets are almost always orphaned hook payloads
    (the target memory never existed or was already handled). Dismissing
    them keeps `journal_stats` honest without human clicks. Audited per
    event; returns dismissed event ids. dry_run=True evaluates the same scan
    and only reports the ids — it never writes (C1 parity with --apply).
    """
    rows = _scan_stale_builtin(conn, days)
    dismissed = []
    for event_id, project_id, event_agent_id, decision in rows:
        if dry_run:
            dismissed.append(event_id)
            continue
        now = core._now()
        with store.write_tx(conn):
            cur = conn.execute(
                "UPDATE ingest_event SET status='ignored', "
                "decision='builtin_memory_mutation_auto_dismissed', "
                'processed_at=? WHERE id=? AND status=\'pending\'',
                (now, event_id),
            )
            if cur.rowcount != 1:
                continue
            core._audit(
                conn, 'journal_auto_dismiss', actor_agent_id,
                project_id=project_id,
                detail={'event_id': event_id,
                        'event_agent_id': event_agent_id,
                        'previous_decision': decision,
                        'rationale': f'stale builtin unresolved > {days}d'})
        dismissed.append(event_id)
    return dismissed



def _scan_defer_cap(conn, max_defers):
    """Shared scan for defer-cap candidates (dry-run and apply, C1)."""
    if not isinstance(max_defers, int) or max_defers < 1:
        raise core.MemCoreError('max_defers must be an integer >= 1')
    return conn.execute(
        'SELECT e.id, e.project_id, e.agent_id, e.decision, '
        '  (SELECT COUNT(*) FROM ingest_analysis a '
        "   WHERE a.event_id=e.id AND a.verdict='defer') AS defers "
        'FROM ingest_event e '
        "WHERE e.status='pending' AND e.decision='semantic_deferred' "
        'ORDER BY e.created_at, e.id',
    ).fetchall()



def auto_resolve_defer_cap(conn, max_defers=3, actor_agent_id=None, *,
                           dry_run=False):
    """Auto-ignore semantic events deferred `max_defers` times or more.

    A repeatedly deferred event is the analyzer saying "never sure" —
    keeping it pending forever only inflates the review queue. Returns
    dismissed event ids. dry_run=True evaluates the same scan and only
    reports the ids — it never writes (C1 parity with --apply).
    """
    rows = _scan_defer_cap(conn, max_defers)
    dismissed = []
    for event_id, project_id, event_agent_id, decision, defers in rows:
        if (defers or 0) < max_defers:
            continue
        if dry_run:
            dismissed.append(event_id)
            continue
        now = core._now()
        with store.write_tx(conn):
            cur = conn.execute(
                "UPDATE ingest_event SET status='ignored', "
                "decision='semantic_defer_cap_reached', "
                'processed_at=? WHERE id=? AND status=\'pending\'',
                (now, event_id),
            )
            if cur.rowcount != 1:
                continue
            core._audit(
                conn, 'journal_auto_dismiss', actor_agent_id,
                project_id=project_id,
                detail={'event_id': event_id,
                        'event_agent_id': event_agent_id,
                        'previous_decision': decision,
                        'defer_count': defers,
                        'rationale': f'defer cap reached ({defers}>={max_defers})'})
        dismissed.append(event_id)
    return dismissed



def sweep(conn, *, builtin_days=7, max_defers=3, actor_agent_id=None,
          dry_run=False):
    """Run full automated journal maintenance sweep in one call.

    Auto-dismisses stale unresolved built-in mutations older than builtin_days
    and auto-resolves semantic events deferred max_defers or more times.
    Returns summary dict of dismissed event IDs. dry_run=True routes both
    phases through their identical scan queries and reports the ids that
    WOULD be dismissed without writing (C1 parity with --apply).
    """
    stale_builtin = auto_dismiss_stale_builtin(
        conn, days=builtin_days, actor_agent_id=actor_agent_id,
        dry_run=dry_run
    )
    defer_cap = auto_resolve_defer_cap(
        conn, max_defers=max_defers, actor_agent_id=actor_agent_id,
        dry_run=dry_run
    )
    return {
        'stale_builtin': stale_builtin,
        'defer_cap': defer_cap,
    }



def prune_journal(conn, days=30, statuses=('ignored', 'processed'), project_id=None):
    """Safely prune historical journal events older than retention cutoff.

    Only events with safe terminal statuses (by default: 'ignored' and 'processed')
    are pruned. Under no circumstances are 'pending' events pruned.
    Cascades removal across ingest_derivation and ingest_analysis tables.
    Returns the number of pruned events.
    """
    if days < 0:
        raise core.MemCoreError('journal retention days must be >= 0')
    if not statuses:
        return 0
    safe_statuses = {'ignored', 'processed', 'failed'}
    invalid = set(statuses) - safe_statuses
    if invalid:
        raise core.MemCoreError(
            f'cannot prune journal events with status: {", ".join(sorted(invalid))}'
        )
    cutoff = core._cutoff(conn, days)
    placeholders = ','.join('?' for _ in statuses)
    sql = (
        f'SELECT id FROM ingest_event '
        f'WHERE status IN ({placeholders}) AND created_at <= ?'
    )
    params = list(statuses) + [cutoff]
    if project_id is not None:
        sql += ' AND project_id = ?'
        params.append(project_id)

    with store.write_tx(conn):
        cur = conn.execute(sql, params)
        event_ids = [row[0] for row in cur.fetchall()]
        if not event_ids:
            return 0
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i+500]
            marks = ','.join('?' for _ in chunk)
            conn.execute(f'DELETE FROM ingest_derivation WHERE event_id IN ({marks})', chunk)
            conn.execute(f'DELETE FROM ingest_analysis WHERE event_id IN ({marks})', chunk)
            conn.execute(f'DELETE FROM ingest_event WHERE id IN ({marks})', chunk)
    return len(event_ids)
