"""MemCore journal — semantic review queue: curation, verdicts, dismissals."""
import hashlib
import json

from . import core
from . import store

from .ingest_builtin import (
    _admit_journal_candidate,
)

from .ingest_text import (
    MAX_ANALYSIS_CANDIDATE_CHARS,
    SEMANTIC_QUEUE_DECISIONS,
    SEMANTIC_VERDICTS,
    _clean_text,
    classify_user_text,
)




def _selected_queue_decisions(decisions):
    """Validate + normalize the semantic queue decision filter."""
    if decisions is None:
        return sorted(SEMANTIC_QUEUE_DECISIONS)
    if isinstance(decisions, str) or not isinstance(decisions, (list, tuple, set, frozenset)):
        raise core.MemCoreError('semantic queue decisions must be a non-empty collection')
    selected = sorted(set(decisions))
    if not selected:
        raise core.MemCoreError('semantic queue decisions must be a non-empty collection')
    invalid = sorted(set(selected) - SEMANTIC_QUEUE_DECISIONS)
    if invalid:
        raise core.MemCoreError(
            'invalid semantic queue decision(s): ' + ', '.join(invalid)
        )
    return selected



def auto_curate_pending(conn, project_id, agent_id, *, limit=25, retention_days=7):
    """Local, zero-debt deterministic and vector auto-curator for ingest events.

    Drains stalled review queues in <10ms:
    1. Re-evaluates pending events with classify_user_text.
    2. Conversational chatter, questions, and non-durable turns are marked 'ignored' (auto_curated_transient).
    3. High-signal durable statements check for duplicate claims and admit as private candidate or accepted.
    4. Automatically purges ignored journal events older than retention_days.
    """
    core._require_membership(conn, project_id, agent_id)
    now = core._now()
    stats = {'examined': 0, 'ignored': 0, 'admitted': 0, 'purged': 0}

    with store.write_tx(conn):
        rows = conn.execute(
            'SELECT id, user_content, assistant_content, decision FROM ingest_event '
            'WHERE project_id=? AND agent_id=? AND status=\'pending\' '
            'AND decision IN (\'semantic_review_required\', \'semantic_deferred\', \'builtin_memory_replace_unresolved_target\') '
            'ORDER BY created_at ASC LIMIT ?',
            (project_id, agent_id, limit)
        ).fetchall()

        for event_id, user_content, assistant_content, orig_decision in rows:
            stats['examined'] += 1
            status, decision, candidate = classify_user_text(user_content)
            if status == 'candidate' and candidate:
                try:
                    _admit_journal_candidate(
                        conn, event_id, project_id, agent_id, candidate,
                        idempotency_key=f'smart_auto:{event_id}',
                        reason='smart-auto durable admission',
                        decision_created='private_candidate',
                        decision_accepted='private_accepted',
                        auto_accept_reason='smart-auto explicit signal',
                    )
                    stats['admitted'] += 1
                except Exception:
                    conn.execute(
                        "UPDATE ingest_event SET status='ignored', decision='auto_curated_error', "
                        'processed_at=? WHERE id=?', (now, event_id)
                    )
                    stats['ignored'] += 1
            else:
                conn.execute(
                    "UPDATE ingest_event SET status='ignored', decision='auto_curated_transient', "
                    'processed_at=? WHERE id=?', (now, event_id)
                )
                stats['ignored'] += 1

        # Purge stale ignored rows older than retention_days
        cur = conn.execute(
            "DELETE FROM ingest_event WHERE status='ignored' AND created_at < datetime('now', ?)",
            (f'-{int(retention_days)} days',)
        )
        stats['purged'] = cur.rowcount

    return stats



def pending_semantic_events(conn, project_id, agent_id, *, limit=20, decisions=None):
    """Return this agent's pending semantic-review queue without cross-agent leakage."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
        raise core.MemCoreError('semantic review limit must be an integer from 1 to 100')
    selected_decisions = _selected_queue_decisions(decisions)
    core._require_membership(conn, project_id, agent_id)
    rows = conn.execute(
        'SELECT id, event_type, user_content, assistant_content, metadata, decision, created_at '
        'FROM ingest_event WHERE project_id=? AND agent_id=? AND status=\'pending\' '
        'AND decision IN (' + ','.join('?' for _ in selected_decisions) + ') '
        'ORDER BY created_at, id LIMIT ?',
        (project_id, agent_id, *selected_decisions, limit)
    ).fetchall()
    results = []
    for event_id, event_type, user_content, assistant_content, metadata_raw, decision, created_at in rows:
        try:
            metadata = json.loads(metadata_raw or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        results.append({
            'event_id': event_id,
            'event_type': event_type,
            'user_content': user_content,
            'assistant_content': assistant_content,
            'metadata': metadata,
            'decision': decision,
            'created_at': created_at,
        })
    return results



def _semantic_analysis_id(event_id, analyzer, verdict, candidate_content, confidence, rationale, metadata):
    payload = json.dumps({
        'event_id': event_id,
        'analyzer': analyzer,
        'verdict': verdict,
        'candidate_content': candidate_content,
        'confidence': confidence,
        'rationale': rationale,
        'metadata': metadata,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'ana-' + hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]



def _coerce_confidence(confidence):
    """Validate/normalize a semantic confidence value (None stays None)."""
    if confidence is None:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise core.MemCoreError('semantic confidence must be a number from 0 to 1')
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise core.MemCoreError('semantic confidence must be a number from 0 to 1')
    return confidence



def _validate_semantic_input(analyzer, verdict, candidate_content, confidence,
                             rationale, metadata):
    """Normalize + validate one semantic verdict payload (no conn, no writes)."""
    analyzer = str(analyzer or '').strip()[:128]
    verdict = str(verdict or '').strip().lower()
    candidate_content = core.scrub_secrets(_clean_text(candidate_content).strip())
    if len(candidate_content) > MAX_ANALYSIS_CANDIDATE_CHARS:
        raise core.MemCoreError('semantic candidate exceeds 4000 characters; submit a complete atomic fact')
    rationale = core.scrub_secrets(_clean_text(rationale).strip()[:4000])
    metadata = metadata if isinstance(metadata, dict) else {}
    if not analyzer:
        raise core.MemCoreError('semantic analyzer name is required')
    if verdict not in SEMANTIC_VERDICTS:
        raise core.MemCoreError('semantic verdict must be remember|ignore|defer')
    if verdict == 'remember' and not candidate_content:
        raise core.MemCoreError('remember verdict requires candidate_content')
    confidence = _coerce_confidence(confidence)
    return analyzer, verdict, candidate_content, confidence, rationale, metadata



def _finalized_event_result(conn, event_id, status, decision):
    """Result dict for an event another path already finalized (+ its memory)."""
    if not str(decision or '').startswith('semantic_'):
        raise core.MemCoreError(
            f'event is already finalized by another path (decision={decision or "none"})'
        )
    linked = conn.execute(
        'SELECT memory_id FROM ingest_derivation WHERE event_id=? '
        'ORDER BY created_at DESC LIMIT 1', (event_id,)
    ).fetchone()
    result = {'event_id': event_id, 'status': status, 'decision': decision}
    if linked:
        result['memory_id'] = linked[0]
    return result



def apply_semantic_analysis(conn, event_id, agent_id, *, analyzer, verdict,
                            candidate_content='', confidence=None, rationale='', metadata=None):
    """Apply an external semantic verdict while MemCore keeps governance authority.

    The analyzer may only say remember/ignore/defer. A remember verdict can create only
    a private candidate owned by the event's agent; scope, lifecycle, tombstones,
    duplicate handling, and durable derivation remain engine-controlled.
    """
    if not isinstance(event_id, str) or not isinstance(agent_id, str):
        raise core.MemCoreError('event_id and agent_id must be strings')
    analyzer, verdict, candidate_content, confidence, rationale, metadata = (
        _validate_semantic_input(
            analyzer, verdict, candidate_content, confidence, rationale,
            metadata))

    analysis_id = _semantic_analysis_id(
        event_id, analyzer, verdict, candidate_content, confidence, rationale, metadata
    )
    with store.write_tx(conn):
        row = conn.execute(
            'SELECT project_id, agent_id, status, decision FROM ingest_event WHERE id=?',
            (event_id,)
        ).fetchone()
        if row is None:
            raise core.MemCoreError(f'ingest event not found: {event_id}')
        project_id, event_agent_id, status, decision = row
        core._require_membership(conn, project_id, agent_id)
        if event_agent_id != agent_id:
            raise core.PermissionDenied('semantic review event belongs to another agent')
        if status != 'pending':
            return _finalized_event_result(conn, event_id, status, decision)
        if decision not in ('semantic_review_required', 'semantic_deferred'):
            raise core.MemCoreError(
                f'event is not awaiting semantic review (decision={decision or "none"})'
            )

        now = core._now()
        conn.execute(
            'INSERT OR IGNORE INTO ingest_analysis '
            '(id,event_id,analyzer,verdict,candidate_content,confidence,rationale,metadata,created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (analysis_id, event_id, analyzer, verdict, candidate_content, confidence,
             rationale, json.dumps(metadata, ensure_ascii=False, sort_keys=True), now)
        )
        if verdict == 'defer':
            conn.execute(
                "UPDATE ingest_event SET decision='semantic_deferred', error=NULL WHERE id=?",
                (event_id,)
            )
            return {'event_id': event_id, 'status': 'pending',
                    'decision': 'semantic_deferred', 'analysis_id': analysis_id}
        if verdict == 'ignore':
            conn.execute(
                "UPDATE ingest_event SET status='ignored', decision='semantic_ignored', "
                'error=NULL, processed_at=? WHERE id=?', (now, event_id)
            )
            return {'event_id': event_id, 'status': 'ignored',
                    'decision': 'semantic_ignored', 'analysis_id': analysis_id}

        # ADR-0019 high-confidence lane: confidence >= 0.95 self-accepts.
        # Below stays candidate. Tombstone-blocked claims refuse loudly.
        # core.auto_accept owns gate -> hold -> accept -> sweep; the lane
        # keeps only the event/analysis writes and its decision strings.
        high_confidence = (
            confidence is not None
            and float(confidence) >= core.HIGH_CONFIDENCE_ACCEPT
        )
        return _admit_journal_candidate(
            conn, event_id, project_id, agent_id, candidate_content,
            idempotency_key=f'semantic:{event_id}',
            reason=f'semantic analysis by {analyzer}',
            analysis_id=analysis_id,
            decision_created='semantic_private_candidate',
            decision_duplicate='semantic_duplicate',
            decision_accepted=(
                'semantic_private_accepted' if high_confidence else None),
            auto_accept_reason=(
                'high-confidence semantic auto-accept'
                if high_confidence else None),
            extra_sweep=not high_confidence,
        )



def semantic_analysis_history(conn, event_id, agent_id):
    """Return semantic verdict history for one event, enforcing raw-event ownership."""
    if not isinstance(event_id, str):
        raise core.MemCoreError(f'ingest event not found: {event_id}')
    row = conn.execute(
        'SELECT project_id, agent_id FROM ingest_event WHERE id=?', (event_id,)
    ).fetchone()
    if row is None:
        raise core.MemCoreError(f'ingest event not found: {event_id}')
    project_id, event_agent_id = row
    core._require_membership(conn, project_id, agent_id)
    if event_agent_id != agent_id:
        raise core.PermissionDenied('semantic review event belongs to another agent')
    results = []
    rows = conn.execute(
        'SELECT id,analyzer,verdict,candidate_content,confidence,rationale,metadata,memory_id,created_at '
        'FROM ingest_analysis WHERE event_id=? ORDER BY created_at,id', (event_id,)
    ).fetchall()
    for row in rows:
        try:
            item_metadata = json.loads(row[6] or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            item_metadata = {}
        results.append({
            'analysis_id': row[0], 'analyzer': row[1], 'verdict': row[2],
            'candidate_content': row[3], 'confidence': row[4], 'rationale': row[5],
            'metadata': item_metadata, 'memory_id': row[7], 'created_at': row[8],
        })
    return results



def _dismissable_pending_decision(decision):
    if decision in SEMANTIC_QUEUE_DECISIONS:
        return True
    value = str(decision or '')
    return value.startswith('builtin_memory_') and (
        value.endswith('_unresolved_target')
        or value.endswith('_missing_old_text')
        or value.endswith('_requires_review')
        or value == 'builtin_memory_replace_missing_content'
    )



def dismiss_unresolved_event(conn, event_id, actor_agent_id, rationale='operator_dismissed'):
    """Dismiss an owned/operator-approved pending review event, with audit."""
    if not isinstance(event_id, str):
        raise core.NotFound(f'ingest event not found: {event_id}')
    with store.write_tx(conn):
        row = conn.execute(
            'SELECT project_id, agent_id, status, decision FROM ingest_event WHERE id=?',
            (event_id,)
        ).fetchone()
        if row is None:
            raise core.NotFound(f'ingest event not found: {event_id}')
        project_id, event_agent_id, status, decision = row
        if status != 'pending':
            raise core.MemCoreError(f'event {event_id} is not pending (status: {status})')
        role = core._require_membership(conn, project_id, actor_agent_id)
        if actor_agent_id != event_agent_id and role != 'owner':
            raise core.PermissionDenied(
                'only the event owner or a project owner may dismiss this event'
            )
        if not _dismissable_pending_decision(decision):
            raise core.MemCoreError(
                f'event {event_id} is not awaiting dismissable review '
                f'(decision={decision or "none"})'
            )
        dismissed_decision = (
            'semantic_review_dismissed'
            if decision in SEMANTIC_QUEUE_DECISIONS
            else 'builtin_memory_mutation_dismissed'
        )
        now = core._now()
        conn.execute(
            "UPDATE ingest_event SET status='ignored', decision=?, processed_at=? WHERE id=?",
            (dismissed_decision, now, event_id)
        )
        core._audit(
            conn, 'journal_dismiss', actor_agent_id, project_id=project_id,
            detail={'event_id': event_id, 'event_agent_id': event_agent_id,
                    'previous_decision': decision,
                    'rationale': core.scrub_secrets(str(rationale or ''))[:1000]}
        )
        return {
            'event_id': event_id,
            'status': 'ignored',
            'decision': dismissed_decision
        }
