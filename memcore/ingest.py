"""Raw Hermes event journal and conservative admission bridge for MemCore.

The journal is append-only ingress. Raw turns are never recalled directly.
Only the analyzer may derive canonical MemCore memories. Automatic
derivations are private candidates by default, except the two auto-accept
lanes (ADR-0019): explicit durable signals and high-confidence semantic
verdicts (confidence >= 0.95).
"""
import hashlib
import json

from . import core
from . import store

from .ingest_builtin import (
    _admit_journal_candidate,
    _parse_metadata,
    _process_builtin_mutation,
)

from .ingest_text import (
    EVENT_TYPES,
    _clean_text,
    _has_remove_reference,
    classify_user_text,
)

from .ingest_builtin import (
    _private_claim_ids,
    _find_private_claim,
    _admit_journal_candidate,
    _memory_write_reference,
    _find_builtin_memory_target,
    _mutation_audit_memory,
    _finish_builtin_mutation,
    _await_builtin_decision,
    _resolve_builtin_target,
    _apply_builtin_action,
    _process_builtin_mutation,
    _parse_metadata,
)

from .ingest_journal import (
    _journal_filters,
    _journal_grouped,
    _journal_pending_counts,
    _journal_analysis_counts,
    _journal_health,
    journal_stats,
    AUTONOMY_AUDIT_ACTIONS,
    journal_age,
    autonomy_per_day,
    _STALE_BUILTIN_DECISIONS,
    _scan_stale_builtin,
    auto_dismiss_stale_builtin,
    _scan_defer_cap,
    auto_resolve_defer_cap,
    sweep,
    prune_journal,
)

from .ingest_semantic import (
    _selected_queue_decisions,
    auto_curate_pending,
    pending_semantic_events,
    _semantic_analysis_id,
    _coerce_confidence,
    _validate_semantic_input,
    _finalized_event_result,
    apply_semantic_analysis,
    semantic_analysis_history,
    _dismissable_pending_decision,
    dismiss_unresolved_event,
)

from .ingest_text import (
    EVENT_TYPES,
    MAX_FIELD_CHARS,
    MAX_ANALYSIS_CANDIDATE_CHARS,
    SEMANTIC_VERDICTS,
    SEMANTIC_QUEUE_DECISIONS,
    _TRIVIAL_RE,
    _EXPLICIT_MEMORY_RE,
    _NEGATIVE_MEMORY_RE,
    _LEADING_REMEMBER_RE,
    _QUESTION_OR_CHAT_RE,
    _NAVIGATION_PREFIX_RE,
    _clean_text,
    _has_remove_reference,
    classify_user_text,
)




def _bounded_session_id(value):
    raw = str(value or '').replace('\x00', '')
    if len(raw) <= 512:
        return raw
    suffix = '~' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return raw[:512 - len(suffix)] + suffix



def _payload_hash(event_type, user_content, assistant_content, metadata):
    payload = json.dumps({
        'event_type': event_type,
        'user_content': user_content,
        'assistant_content': assistant_content,
        'metadata': metadata or {},
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()



def _scrub_journal_pair(user_content, assistant_content):
    """Validate + scrub both raw journal fields through one gate (R1/H6)."""
    raw_user_content = '' if user_content is None else user_content
    raw_assistant_content = '' if assistant_content is None else assistant_content
    if not isinstance(raw_user_content, str) or not isinstance(raw_assistant_content, str):
        raise core.MemCoreError('journal content must be a string')
    return (
        core.scrub_secrets(raw_user_content),
        core.scrub_secrets(raw_assistant_content),
    )



def _event_identity(project_id, agent_id, session_id, content_hash):
    """Deterministic event id over (project, agent, session, payload hash)."""
    return 'evt-' + hashlib.sha256(
        f'{project_id}\0{agent_id}\0{session_id}\0{content_hash}'.encode('utf-8')
    ).hexdigest()[:24]



def _normalize_event_input(project_id, agent_id, event_type, session_id,
                           user_content, assistant_content, metadata):
    """Validate + scrub + hash one event payload; returns the prepared tuple.

    R1/H6 ordering is the contract: secrets are scrubbed BEFORE the payload
    hash so a retry of the same input keeps the same content_hash/event_id
    (idempotent) and raw secrets never reach disk or the semantic-review
    payload. The hash covers the full source payload before bounded storage
    truncation, so distinct long turns sharing the first MAX_FIELD_CHARS
    never collapse together.
    """
    if event_type not in EVENT_TYPES:
        raise core.MemCoreError(f'invalid ingest event type: {event_type}')
    raw_user_content, raw_assistant_content = _scrub_journal_pair(
        user_content, assistant_content)
    user_content = _clean_text(raw_user_content)
    assistant_content = _clean_text(raw_assistant_content)
    metadata = metadata if isinstance(metadata, dict) else {}
    if not user_content and not assistant_content and not _has_remove_reference(
            event_type, metadata):
        raise core.MemCoreError(
            'journal event must contain user or assistant content, or a memory remove old_text'
        )
    session_id = _bounded_session_id(session_id)
    content_hash = _payload_hash(
        event_type, raw_user_content, raw_assistant_content, metadata
    )
    event_id = _event_identity(project_id, agent_id, session_id, content_hash)
    return (event_id, session_id, user_content, assistant_content, metadata,
            content_hash)



def append_event(conn, project_id, agent_id, event_type, *, session_id='',
                 user_content='', assistant_content='', metadata=None):
    """Durably append one raw ingress event; retry-safe within its session."""
    (event_id, session_id, user_content, assistant_content, metadata,
     content_hash) = _normalize_event_input(
        project_id, agent_id, event_type, session_id, user_content,
        assistant_content, metadata)
    with store.write_tx(conn):
        core._require_membership(conn, project_id, agent_id)
        now = core._now()
        # Phase 6a provenance seal: HMAC over the attribution triple with the
        # store-local key. Detects post-write tampering with who-wrote-what.
        # Pre-migration stores without the column accept unsealed rows; the
        # column check keeps old databases readable until migration 0014 runs.
        cols = {r[1] for r in conn.execute('PRAGMA table_info(ingest_event)')}
        if 'provenance_seal' in cols:
            _key_id, _key_material = store._active_provenance_key(conn)
            _seal = store.seal_event(
                project_id, agent_id, content_hash, _key_material)
        else:
            _seal = None
        cur = conn.execute(
            'INSERT OR IGNORE INTO ingest_event '
            '(id, project_id, agent_id, session_id, event_type, user_content, '
            ' assistant_content, metadata, content_hash, status, created_at'
            + (', provenance_seal' if _seal else '') + ') '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?"
            + (', ?' if _seal else '') + ')',
            (event_id, project_id, agent_id, session_id, event_type,
             user_content, assistant_content,
             json.dumps(metadata, ensure_ascii=False, sort_keys=True),
             content_hash, now) + ((_seal,) if _seal else ())
        )
        created = cur.rowcount == 1
    return event_id, created



def _classify_event(event_type, user_content, metadata):
    """Deterministic event classification: (decision, reason, candidate)."""
    if event_type == 'delegation':
        # Delegation task/result payloads are operational history, not trusted
        # user instructions. Keep the raw journal row but never admit it.
        return 'ignore', 'delegation_operational_event', ''
    if event_type == 'memory_write' and metadata.get('success') is False:
        return 'ignore', 'builtin_memory_write_failed_upstream', ''
    if event_type == 'memory_write' and user_content.strip():
        action = str(metadata.get('action') or 'add').strip().lower()
        if action == 'add':
            return ('candidate', 'explicit_memory_write_add',
                    user_content.strip()[:4000])
        return ('review',
                f'builtin_memory_{action or "unknown"}_requires_review', '')
    return classify_user_text(user_content)



def _mark_event_failed(conn, event_id, exc):
    """Record a processing failure on the event row (best-effort, own tx)."""
    try:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE ingest_event SET status='failed', error=?, processed_at=? WHERE id=?",
                (f'{type(exc).__name__}: {exc}'[:2000], core._now(), event_id)
            )
    except Exception:
        pass



def _process_pending_event(conn, event_id):
    """Classify + admit one pending event inside one transaction.

    Returns (result, needs_builtin_mutation). Built-in replace/remove events
    return (None, True) with the classification transaction released: they
    call core write APIs that own their own lifetime.
    """
    with store.write_tx(conn):
        row = conn.execute(
            'SELECT project_id, agent_id, event_type, user_content, metadata, status, decision '
            'FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            raise core.MemCoreError(f'ingest event not found: {event_id}')
        project_id, agent_id, event_type, user_content, metadata_raw, status, decision = row
        metadata = _parse_metadata(metadata_raw)
        if status != 'pending':
            linked = conn.execute(
                'SELECT memory_id FROM ingest_derivation WHERE event_id=? '
                'ORDER BY CASE relation WHEN \'created\' THEN 0 WHEN \'duplicate\' THEN 1 '
                'ELSE 2 END, created_at DESC LIMIT 1', (event_id,)
            ).fetchone()
            result = {'event_id': event_id, 'status': status}
            if decision:
                result['decision'] = decision
            if linked:
                result['memory_id'] = linked[0]
            return result, False
        if event_type == 'memory_write':
            action = str(metadata.get('action') or '').strip().lower()
            if action in ('replace', 'remove'):
                # Built-in mutations call core write APIs that own their own
                # transactions. Leave this classification transaction first.
                return None, True
        core._require_membership(conn, project_id, agent_id)
        decision, reason, candidate = _classify_event(
            event_type, user_content, metadata)
        now = core._now()
        if decision == 'ignore':
            conn.execute(
                "UPDATE ingest_event SET status='ignored', decision=?, processed_at=? WHERE id=?",
                (reason, now, event_id)
            )
            return {'event_id': event_id, 'status': 'ignored',
                    'decision': reason}, False
        if decision == 'review':
            conn.execute(
                'UPDATE ingest_event SET decision=? WHERE id=?', (reason, event_id)
            )
            return {'event_id': event_id, 'status': 'pending',
                    'decision': reason}, False
        # ADR-0019 explicit lane: a deliberate durable signal self-accepts
        # instead of parking in candidate. Tombstone-blocked claims refuse
        # loudly. core.auto_accept owns gate -> hold -> accept -> sweep;
        # the lane keeps only the event writes and its decision strings.
        # ponytail: hold is audit-only on every lane; the removed
        # mark_contradiction demoted the established row from an explicit
        # write, so one bad signal could bury a good fact with no recovery.
        is_tainted = bool(metadata.get('tainted'))
        result = _admit_journal_candidate(
            conn, event_id, project_id, agent_id, candidate,
            idempotency_key=f'ingest:{event_id}',
            reason='native provider journal analysis' + (' [tainted]' if is_tainted else ''),
            decision_created='private_candidate' if is_tainted else 'private_accepted',
            decision_accepted=None if is_tainted else 'private_accepted',
            auto_accept_reason=None if is_tainted else 'explicit durable signal auto-accept',
            extra_sweep=is_tainted,
        )
        return result, False



def process_event(conn, event_id):
    """Analyze one pending event after it is durable in the journal.

    Explicit durable user signals become private candidate memories. Ambiguous
    events stay pending for a future semantic analyzer or operator review.
    """
    mutation_path = False
    try:
        result, mutation_path = _process_pending_event(conn, event_id)
        if mutation_path:
            return _process_builtin_mutation(conn, event_id)
    except Exception as exc:
        if mutation_path:
            raise
        _mark_event_failed(conn, event_id, exc)
        raise
    return result



def record(conn, project_id, agent_id, event_type, *, session_id='',
           user_content='', assistant_content='', metadata=None,
           auto_process=True):
    """Unified journal recording and automatic classification.

    Appends an ingress event durably to the journal and immediately triggers
    deterministic classification/processing when auto_process is True.
    Returns status dictionary augmented with 'event_id' and 'created'.
    """
    event_id, created = append_event(
        conn, project_id, agent_id, event_type,
        session_id=session_id, user_content=user_content,
        assistant_content=assistant_content, metadata=metadata
    )
    if not auto_process and created:
        return {'event_id': event_id, 'created': True, 'status': 'pending'}
    if not auto_process:
        row = conn.execute(
            'SELECT status, decision FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            raise core.MemCoreError(f'ingest event not found: {event_id}')
        result = {'event_id': event_id, 'created': False, 'status': row[0]}
        if row[1]:
            result['decision'] = row[1]
        linked = conn.execute(
            'SELECT memory_id FROM ingest_derivation WHERE event_id=? '
            'ORDER BY CASE relation WHEN \'created\' THEN 0 WHEN \'duplicate\' THEN 1 '
            'ELSE 2 END, created_at DESC LIMIT 1', (event_id,)
        ).fetchone()
        if linked:
            result['memory_id'] = linked[0]
        return result
    proc = process_event(conn, event_id)
    proc['created'] = created
    return proc
