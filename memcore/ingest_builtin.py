"""MemCore journal — admission bridge and builtin-memory mutation handling."""
import json
import sqlite3

from . import core
from . import store




def _private_claim_ids(conn, project_id, agent_id, claim_fp, *, active_only=False):
    """Indexed private-claim lookup with lazy repair for externally inserted legacy rows."""
    lifecycle_sql = (
        "m.lifecycle IN ('candidate','accepted','conflict')"
        if active_only else
        "m.lifecycle NOT IN ('rejected','disabled','superseded')"
    )
    rows = conn.execute(
        'SELECT m.id FROM memory m '
        "WHERE m.project_id=? AND m.scope='private' AND m.owner_agent_id=? "
        'AND m.claim_fingerprint=? AND ' + lifecycle_sql,
        (project_id, agent_id, claim_fp)
    ).fetchall()
    matches = [row[0] for row in rows]
    if matches:
        return matches

    # Migration 0010 backfills existing stores and all engine writes maintain the
    # column. This fallback only covers manual/external inserts that omitted it;
    # repair those rows once so future lookups stay on the indexed fast path.
    legacy = conn.execute(
        'SELECT m.id, v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
        "WHERE m.project_id=? AND m.scope='private' AND m.owner_agent_id=? "
        'AND m.claim_fingerprint IS NULL AND ' + lifecycle_sql,
        (project_id, agent_id)
    ).fetchall()
    repairs = []
    for memory_id, content in legacy:
        current_fp = core.fingerprint(content)
        repairs.append((current_fp, memory_id))
        if current_fp == claim_fp:
            matches.append(memory_id)
    if repairs:
        # H3: best-effort repair; a read-only handle (mode=ro) still returns
        # the RAM-computed matches instead of crashing on the UPDATE.
        try:
            conn.executemany(
                'UPDATE memory SET claim_fingerprint=? '
                'WHERE id=? AND claim_fingerprint IS NULL', repairs
            )
        except sqlite3.OperationalError:
            pass
    return matches



def _find_private_claim(conn, project_id, agent_id, claim_fp):
    matches = _private_claim_ids(conn, project_id, agent_id, claim_fp)
    return matches[0] if matches else None



def _admit_journal_candidate(conn, event_id, project_id, agent_id,
                             candidate_content, *, idempotency_key, reason,
                             decision_created, decision_accepted=None,
                             decision_duplicate='duplicate',
                             auto_accept_reason=None, extra_sweep=False,
                             analysis_id=None):
    """Duplicate-merge → create → auto-accept/hold → derivation → event update.

    The single admission bridge shared by the semantic verdict lane and the
    explicit durable-signal lane (C4). Runs inside the caller's transaction;
    the caller owns COMMIT/ROLLBACK. Returns the event result dict.

    - decision_created: the event decision when the memory stays a candidate
      (or the only decision, when no auto-accept ran).
    - decision_accepted: the decision when auto_accept transitioned the row.
    - auto_accept_reason: when set, run core.auto_accept (gate → hold →
      accept → sweep) and handle its hold outcome.
    - extra_sweep: run one maybe_auto_corrob sweep when auto_accept did NOT
      run (the low-confidence candidate path may complete a corroboration
      set started by other agents).
    """
    now = core._now()
    claim_fp = core.fingerprint(candidate_content)
    existing = _find_private_claim(conn, project_id, agent_id, claim_fp)
    if existing:
        conn.execute(
            "INSERT OR IGNORE INTO ingest_derivation "
            "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'duplicate', ?)",
            (event_id, existing, now)
        )
        core._audit(conn, 'duplicate-merge', agent_id, existing,
                    project_id, {'event_id': event_id})
        if analysis_id is not None:
            conn.execute('UPDATE ingest_analysis SET memory_id=? WHERE id=?',
                         (existing, analysis_id))
        conn.execute(
            "UPDATE ingest_event SET status='processed', decision=?, "
            'error=NULL, processed_at=? WHERE id=?',
            (decision_duplicate, now, event_id)
        )
        result = {'event_id': event_id, 'status': 'processed',
                  'decision': decision_duplicate, 'memory_id': existing}
        if analysis_id is not None:
            result['analysis_id'] = analysis_id
        return result

    memory_id, _version_id = core.create_memory(
        conn, project_id, agent_id, candidate_content,
        scope='private', memory_type='observation', lifecycle='candidate',
        idempotency_key=idempotency_key, reason=reason,
        _manage_transaction=False
    )
    decision = decision_created
    if auto_accept_reason is not None:
        outcome = core.auto_accept(conn, memory_id, agent_id,
                                   auto_accept_reason)
        if outcome['held']:
            conn.execute(
                "INSERT INTO ingest_derivation "
                "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'ignored', ?)",
                (event_id, memory_id, core._now())
            )
            if analysis_id is not None:
                conn.execute('UPDATE ingest_analysis SET memory_id=? WHERE id=?',
                             (memory_id, analysis_id))
            conn.execute(
                "UPDATE ingest_event SET status='processed', decision=?, "
                'error=NULL, processed_at=? WHERE id=?',
                ('contradiction_hold', now, event_id)
            )
            result = {'event_id': event_id, 'status': 'processed',
                      'decision': 'contradiction_hold',
                      'memory_id': memory_id, 'held': True}
            if analysis_id is not None:
                result['analysis_id'] = analysis_id
            return result
        decision = decision_accepted or decision_created
    elif extra_sweep:
        core.maybe_auto_corrob(conn, project_id, claim_fp, agent_id)
    conn.execute(
        "INSERT INTO ingest_derivation "
        "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'created', ?)",
        (event_id, memory_id, now)
    )
    if analysis_id is not None:
        conn.execute('UPDATE ingest_analysis SET memory_id=? WHERE id=?',
                     (memory_id, analysis_id))
    conn.execute(
        "UPDATE ingest_event SET status='processed', decision=?, "
        'error=NULL, processed_at=? WHERE id=?', (decision, now, event_id)
    )
    result = {'event_id': event_id, 'status': 'processed',
              'decision': decision, 'memory_id': memory_id}
    if analysis_id is not None:
        result['analysis_id'] = analysis_id
    return result



def _memory_write_reference(metadata):
    """Return the strongest old-claim reference supplied by Hermes.

    Current native adapters may preserve the original hook payload under
    ``builtin_metadata``. Accept both the promoted and nested shapes so journal
    replay remains compatible across adapter versions.
    """
    sources = [metadata]
    nested = metadata.get('builtin_metadata')
    if isinstance(nested, dict):
        sources.append(nested)
    for source in sources:
        for key in ('matched_entry', 'old_text'):
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get('content') or value.get('text') or value.get('value')
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''



def _find_builtin_memory_target(conn, project_id, agent_id, old_text, target=''):
    """Resolve exactly one active private memory previously derived from the built-in hook.

    old_text is deliberately treated as a full-claim reference here. Hermes' built-in
    tool may use substring matching internally, but reproducing that fuzzy lookup in a
    second store can mutate the wrong memory. Uncertain matches remain pending.
    """
    claim_fp = core.fingerprint(old_text)
    candidate_ids = _private_claim_ids(
        conn, project_id, agent_id, claim_fp, active_only=True
    )
    if not candidate_ids:
        return None

    marks = ','.join('?' for _ in candidate_ids)
    origins = conn.execute(
        'SELECT d.memory_id, e.metadata FROM ingest_derivation d '
        'JOIN ingest_event e ON e.id=d.event_id '
        f"WHERE d.memory_id IN ({marks}) AND e.event_type='memory_write'",
        candidate_ids
    ).fetchall()
    matched = set()
    for memory_id, raw_metadata in origins:
        if not target:
            matched.add(memory_id)
            continue
        try:
            origin = json.loads(raw_metadata or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            origin = {}
        if str(origin.get('target') or '').strip().lower() == target:
            matched.add(memory_id)
    return next(iter(matched)) if len(matched) == 1 else None



def _mutation_audit_memory(conn, project_id, agent_id, event_id, action):
    """Recover a mutation that committed before its journal status update."""
    marker = f'ingest:{event_id}'
    row = conn.execute(
        'SELECT memory_id FROM audit_event '
        'WHERE write_key=? AND project_id=? AND actor_agent_id=? AND action=? LIMIT 1',
        (marker, project_id, agent_id, action)
    ).fetchone()
    if row:
        return row[0]
    rows = conn.execute(
        'SELECT memory_id, detail FROM audit_event '
        'WHERE project_id=? AND actor_agent_id=? AND action=? ORDER BY id DESC',
        (project_id, agent_id, action)
    ).fetchall()
    for memory_id, detail_raw in rows:
        try:
            detail = json.loads(detail_raw or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if detail.get('reason') == marker:
            return memory_id
    return None



def _finish_builtin_mutation(conn, event_id, memory_id, decision):
    now = core._now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO ingest_derivation "
            "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'corrected', ?)",
            (event_id, memory_id, now)
        )
        conn.execute(
            "UPDATE ingest_event SET status='processed', decision=?, error=NULL, processed_at=? "
            'WHERE id=?',
            (decision, now, event_id)
        )
    return {'event_id': event_id, 'status': 'processed',
            'decision': decision, 'memory_id': memory_id}



def _await_builtin_decision(conn, event_id, decision):
    """Record why a builtin mutation still waits; report it as pending."""
    conn.execute(
        'UPDATE ingest_event SET decision=? WHERE id=?', (decision, event_id))
    return {'event_id': event_id, 'status': 'pending', 'decision': decision}



def _resolve_builtin_target(conn, project_id, agent_id, event_id, metadata,
                            action):
    """Resolve which memory a builtin replace/remove targets.

    Returns (memory_id, decision, recovered): a recovered mutation carries
    its memory id + final decision; an unresolved target carries a pending
    decision and no memory id.
    """
    audit_action = 'supersede' if action == 'replace' else 'reject'
    recovered = _mutation_audit_memory(
        conn, project_id, agent_id, event_id, audit_action
    )
    if recovered:
        return recovered, f'builtin_memory_{action}d', True
    old_text = _memory_write_reference(metadata)
    if not old_text:
        return None, f'builtin_memory_{action}_missing_old_text', False
    target = str(metadata.get('target') or '').strip().lower()
    memory_id = _find_builtin_memory_target(
        conn, project_id, agent_id, old_text, target=target
    )
    if not memory_id:
        return None, f'builtin_memory_{action}_unresolved_target', False
    return memory_id, None, False



def _apply_builtin_action(conn, action, memory_id, agent_id, user_content,
                          marker):
    """Apply a resolved replace/remove mutation; returns the decision string."""
    if action == 'replace':
        new_content = (user_content or '').strip()
        if not new_content:
            return 'builtin_memory_replace_missing_content'
        core.supersede(
            conn, memory_id, agent_id, new_content,
            reason=marker, write_key=marker
        )
        return 'builtin_memory_replaced'
    # A durable remove is a refusal to retain this claim. Rejecting leaves only
    # its fingerprint tombstone, preventing later journal replay from resurrecting it.
    core.reject(conn, memory_id, agent_id, marker, write_key=marker)
    return 'builtin_memory_removed'



def _process_builtin_mutation(conn, event_id):
    row = conn.execute(
        'SELECT project_id, agent_id, user_content, metadata, status '
        'FROM ingest_event WHERE id=?', (event_id,)
    ).fetchone()
    if row is None:
        raise core.MemCoreError(f'ingest event not found: {event_id}')
    project_id, agent_id, user_content, metadata_raw, status = row
    metadata = _parse_metadata(metadata_raw)
    core._require_membership(conn, project_id, agent_id)
    if status != 'pending':
        return {'event_id': event_id, 'status': status}

    action = str(metadata.get('action') or '').strip().lower()
    if action not in ('replace', 'remove'):
        raise core.MemCoreError(f'not a built-in mutation event: {action or "unknown"}')
    if metadata.get('success') is False:
        now = core._now()
        conn.execute(
            "UPDATE ingest_event SET status='ignored', decision=?, processed_at=? WHERE id=?",
            ('builtin_memory_write_failed_upstream', now, event_id)
        )
        return {'event_id': event_id, 'status': 'ignored',
                'decision': 'builtin_memory_write_failed_upstream'}

    memory_id, decision, recovered = _resolve_builtin_target(
        conn, project_id, agent_id, event_id, metadata, action)
    if recovered:
        return _finish_builtin_mutation(conn, event_id, memory_id, decision)
    if memory_id is None:
        return _await_builtin_decision(conn, event_id, decision)

    marker = f'ingest:{event_id}'
    decision = _apply_builtin_action(
        conn, action, memory_id, agent_id, user_content, marker)
    if decision == 'builtin_memory_replace_missing_content':
        return _await_builtin_decision(conn, event_id, decision)
    return _finish_builtin_mutation(conn, event_id, memory_id, decision)



def _parse_metadata(raw):
    """Parse a stored JSON metadata blob; parse failures degrade to {}."""
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
