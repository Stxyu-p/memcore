"""Raw Hermes event journal and conservative admission bridge for MemCore.

The journal is append-only ingress. Raw turns are never recalled directly.
Only the analyzer may derive canonical MemCore memories, and automatic
derivations are private candidates by default.
"""
import hashlib
import json
import re

from . import core

EVENT_TYPES = {'turn', 'memory_write', 'delegation', 'session_end', 'manual'}
MAX_FIELD_CHARS = 65536
MAX_ANALYSIS_CANDIDATE_CHARS = 4000
SEMANTIC_VERDICTS = {'remember', 'ignore', 'defer'}
SEMANTIC_QUEUE_DECISIONS = {'semantic_review_required', 'semantic_deferred'}

_TRIVIAL_RE = re.compile(
    r'^(?:ok|okay|yes|no|thanks|thank you|hi|hey|hello|continue|next|done|test|'
    r'(?:โอเค|ขอบคุณ|สวัสดี|ทดสอบ|เทส|ต่อ|ต่อเลย|ได้|ดี)\s*(?:นะคะ|นะครับ|ค่ะ|ครับ|คะ)?|'
    r'ครับ|ค่ะ)[\s!?.…]*$', re.IGNORECASE
)
_EXPLICIT_MEMORY_RE = re.compile(
    r'(?:^|\b)(?:please\s+)?remember(?:\s+that)?\b|'
    r'\bfrom now on\b|\bi (?:prefer|always use|use)\b|\bwe (?:decided|agreed|chose)\b|'
    r'(?:^|\s)(?:จำไว้(?:ว่า)?|ช่วยจำ(?:ไว้)?(?:ว่า)?|จำว่า|ต่อไปนี้|'
    r'ฉันชอบ|ผมชอบ|ฉันใช้|ผมใช้|เราตกลง(?:กัน)?|เราตัดสินใจ(?:กัน)?|'
    r'ต่อไป(?=\s|ว่า|:|,|$))', re.IGNORECASE
)
_LEADING_REMEMBER_RE = re.compile(
    r'^\s*(?:(?:please\s+)?remember(?:\s+that)?|จำไว้(?:ว่า)?|ช่วยจำ(?:ไว้)?(?:ว่า)?|จำว่า)'
    r'[\s,:-]*', re.IGNORECASE
)

def _clean_text(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise core.MemCoreError('journal content must be a string')
    return value[:MAX_FIELD_CHARS]


def _bounded_session_id(value):
    raw = str(value or '')
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


def append_event(conn, project_id, agent_id, event_type, *, session_id='',
                 user_content='', assistant_content='', metadata=None):
    """Durably append one raw ingress event; retry-safe within its session."""
    if event_type not in EVENT_TYPES:
        raise core.MemCoreError(f'invalid ingest event type: {event_type}')
    raw_user_content = '' if user_content is None else user_content
    raw_assistant_content = '' if assistant_content is None else assistant_content
    if not isinstance(raw_user_content, str) or not isinstance(raw_assistant_content, str):
        raise core.MemCoreError('journal content must be a string')
    user_content = _clean_text(raw_user_content)
    assistant_content = _clean_text(raw_assistant_content)
    metadata = metadata if isinstance(metadata, dict) else {}
    memory_action = str(metadata.get('action') or '').strip().lower()
    builtin_metadata = metadata.get('builtin_metadata')
    old_text = metadata.get('old_text')
    if not old_text and isinstance(builtin_metadata, dict):
        old_text = builtin_metadata.get('old_text')
    has_remove_reference = (
        event_type == 'memory_write' and memory_action == 'remove'
        and isinstance(old_text, str) and bool(old_text.strip())
    )
    if not user_content and not assistant_content and not has_remove_reference:
        raise core.MemCoreError(
            'journal event must contain user or assistant content, or a memory remove old_text'
        )
    session_id = _bounded_session_id(session_id)
    # Hash the full source payload before bounded storage truncation. Otherwise
    # distinct long turns sharing the first MAX_FIELD_CHARS collapse together.
    content_hash = _payload_hash(
        event_type, raw_user_content, raw_assistant_content, metadata
    )
    event_id = 'evt-' + hashlib.sha256(
        f'{project_id}\0{agent_id}\0{session_id}\0{content_hash}'.encode('utf-8')
    ).hexdigest()[:24]
    conn.execute('BEGIN IMMEDIATE')
    try:
        core._require_membership(conn, project_id, agent_id)
        now = core._now()
        cur = conn.execute(
            'INSERT OR IGNORE INTO ingest_event '
            '(id, project_id, agent_id, session_id, event_type, user_content, '
            ' assistant_content, metadata, content_hash, status, created_at) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (event_id, project_id, agent_id, session_id, event_type,
             user_content, assistant_content,
             json.dumps(metadata, ensure_ascii=False, sort_keys=True),
             content_hash, now)
        )
        created = cur.rowcount == 1
        conn.execute('COMMIT')
        return event_id, created
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise


def classify_user_text(text):
    """Deterministic first-stage gate. It never trusts assistant output."""
    text = (text or '').strip()
    if not text or text.startswith('/') or '<memory-context>' in text.lower():
        return 'ignore', 'non_user_signal', ''
    if _TRIVIAL_RE.fullmatch(text):
        return 'ignore', 'trivial', ''
    if _EXPLICIT_MEMORY_RE.search(text):
        candidate = _LEADING_REMEMBER_RE.sub('', text).strip() or text
        if len(candidate) > MAX_ANALYSIS_CANDIDATE_CHARS or '```' in candidate:
            return 'review', 'semantic_review_required', ''
        return 'candidate', 'explicit_durable_signal', candidate
    return 'review', 'semantic_review_required', ''


def pending_semantic_events(conn, project_id, agent_id, *, limit=20, decisions=None):
    """Return this agent's pending semantic-review queue without cross-agent leakage."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > 100:
        raise core.MemCoreError('semantic review limit must be an integer from 1 to 100')
    if decisions is None:
        selected_decisions = sorted(SEMANTIC_QUEUE_DECISIONS)
    else:
        if isinstance(decisions, str) or not isinstance(decisions, (list, tuple, set, frozenset)):
            raise core.MemCoreError('semantic queue decisions must be a non-empty collection')
        selected_decisions = sorted(set(decisions))
        if not selected_decisions:
            raise core.MemCoreError('semantic queue decisions must be a non-empty collection')
        invalid = sorted(set(selected_decisions) - SEMANTIC_QUEUE_DECISIONS)
        if invalid:
            raise core.MemCoreError(
                'invalid semantic queue decision(s): ' + ', '.join(invalid)
            )
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


def apply_semantic_analysis(conn, event_id, agent_id, *, analyzer, verdict,
                            candidate_content='', confidence=None, rationale='', metadata=None):
    """Apply an external semantic verdict while MemCore keeps governance authority.

    The analyzer may only say remember/ignore/defer. A remember verdict can create only
    a private candidate owned by the event's agent; scope, lifecycle, tombstones,
    duplicate handling, and durable derivation remain engine-controlled.
    """
    analyzer = str(analyzer or '').strip()[:128]
    verdict = str(verdict or '').strip().lower()
    candidate_content = _clean_text(candidate_content).strip()
    if len(candidate_content) > MAX_ANALYSIS_CANDIDATE_CHARS:
        raise core.MemCoreError('semantic candidate exceeds 4000 characters; submit a complete atomic fact')
    rationale = _clean_text(rationale).strip()[:4000]
    metadata = metadata if isinstance(metadata, dict) else {}
    if not analyzer:
        raise core.MemCoreError('semantic analyzer name is required')
    if verdict not in SEMANTIC_VERDICTS:
        raise core.MemCoreError('semantic verdict must be remember|ignore|defer')
    if verdict == 'remember' and not candidate_content:
        raise core.MemCoreError('remember verdict requires candidate_content')
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise core.MemCoreError('semantic confidence must be a number from 0 to 1')
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise core.MemCoreError('semantic confidence must be a number from 0 to 1')

    analysis_id = _semantic_analysis_id(
        event_id, analyzer, verdict, candidate_content, confidence, rationale, metadata
    )
    conn.execute('BEGIN IMMEDIATE')
    try:
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
            if not str(decision or '').startswith('semantic_'):
                raise core.MemCoreError(
                    f'event is already finalized by another path (decision={decision or "none"})'
                )
            linked = conn.execute(
                'SELECT memory_id FROM ingest_derivation WHERE event_id=? '
                'ORDER BY created_at DESC LIMIT 1', (event_id,)
            ).fetchone()
            conn.execute('ROLLBACK')
            result = {'event_id': event_id, 'status': status, 'decision': decision}
            if linked:
                result['memory_id'] = linked[0]
            return result
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
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'pending',
                    'decision': 'semantic_deferred', 'analysis_id': analysis_id}
        if verdict == 'ignore':
            conn.execute(
                "UPDATE ingest_event SET status='ignored', decision='semantic_ignored', "
                'error=NULL, processed_at=? WHERE id=?', (now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'ignored',
                    'decision': 'semantic_ignored', 'analysis_id': analysis_id}

        claim_fp = core.fingerprint(candidate_content)
        existing = _find_private_claim(conn, project_id, agent_id, claim_fp)
        if existing:
            conn.execute(
                "INSERT OR IGNORE INTO ingest_derivation "
                "(event_id,memory_id,relation,created_at) VALUES (?,?,'duplicate',?)",
                (event_id, existing, now)
            )
            conn.execute('UPDATE ingest_analysis SET memory_id=? WHERE id=?',
                         (existing, analysis_id))
            conn.execute(
                "UPDATE ingest_event SET status='processed', decision='semantic_duplicate', "
                'error=NULL, processed_at=? WHERE id=?', (now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'processed',
                    'decision': 'semantic_duplicate', 'analysis_id': analysis_id,
                    'memory_id': existing}

        memory_id, _version_id = core.create_memory(
            conn, project_id, agent_id, candidate_content,
            scope='private', memory_type='observation', lifecycle='candidate',
            idempotency_key=f'semantic:{event_id}',
            reason=f'semantic analysis by {analyzer}', _manage_transaction=False
        )
        conn.execute(
            "INSERT INTO ingest_derivation "
            "(event_id,memory_id,relation,created_at) VALUES (?,?,'created',?)",
            (event_id, memory_id, now)
        )
        conn.execute('UPDATE ingest_analysis SET memory_id=? WHERE id=?',
                     (memory_id, analysis_id))
        conn.execute(
            "UPDATE ingest_event SET status='processed', decision='semantic_private_candidate', "
            'error=NULL, processed_at=? WHERE id=?', (now, event_id)
        )
        conn.execute('COMMIT')
        return {'event_id': event_id, 'status': 'processed',
                'decision': 'semantic_private_candidate', 'analysis_id': analysis_id,
                'memory_id': memory_id}
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise


def semantic_analysis_history(conn, event_id, agent_id):
    """Return semantic verdict history for one event, enforcing raw-event ownership."""
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
        conn.executemany(
            'UPDATE memory SET claim_fingerprint=? '
            'WHERE id=? AND claim_fingerprint IS NULL', repairs
        )
    return matches


def _find_private_claim(conn, project_id, agent_id, claim_fp):
    matches = _private_claim_ids(conn, project_id, agent_id, claim_fp)
    return matches[0] if matches else None


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
    conn.execute('BEGIN IMMEDIATE')
    try:
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
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
    return {'event_id': event_id, 'status': 'processed',
            'decision': decision, 'memory_id': memory_id}


def _process_builtin_mutation(conn, event_id):
    row = conn.execute(
        'SELECT project_id, agent_id, user_content, metadata, status '
        'FROM ingest_event WHERE id=?', (event_id,)
    ).fetchone()
    if row is None:
        raise core.MemCoreError(f'ingest event not found: {event_id}')
    project_id, agent_id, user_content, metadata_raw, status = row
    try:
        metadata = json.loads(metadata_raw or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
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

    audit_action = 'supersede' if action == 'replace' else 'reject'
    recovered = _mutation_audit_memory(
        conn, project_id, agent_id, event_id, audit_action
    )
    if recovered:
        return _finish_builtin_mutation(
            conn, event_id, recovered, f'builtin_memory_{action}d'
        )

    old_text = _memory_write_reference(metadata)
    if not old_text:
        decision = f'builtin_memory_{action}_missing_old_text'
        conn.execute('UPDATE ingest_event SET decision=? WHERE id=?', (decision, event_id))
        return {'event_id': event_id, 'status': 'pending', 'decision': decision}
    target = str(metadata.get('target') or '').strip().lower()
    memory_id = _find_builtin_memory_target(
        conn, project_id, agent_id, old_text, target=target
    )
    if not memory_id:
        decision = f'builtin_memory_{action}_unresolved_target'
        conn.execute('UPDATE ingest_event SET decision=? WHERE id=?', (decision, event_id))
        return {'event_id': event_id, 'status': 'pending', 'decision': decision}

    marker = f'ingest:{event_id}'
    if action == 'replace':
        new_content = (user_content or '').strip()
        if not new_content:
            decision = 'builtin_memory_replace_missing_content'
            conn.execute('UPDATE ingest_event SET decision=? WHERE id=?', (decision, event_id))
            return {'event_id': event_id, 'status': 'pending', 'decision': decision}
        core.supersede(
            conn, memory_id, agent_id, new_content,
            reason=marker, write_key=marker
        )
        decision = 'builtin_memory_replaced'
    else:
        # A durable remove is a refusal to retain this claim. Rejecting leaves only
        # its fingerprint tombstone, preventing later journal replay from resurrecting it.
        core.reject(conn, memory_id, agent_id, marker, write_key=marker)
        decision = 'builtin_memory_removed'
    return _finish_builtin_mutation(conn, event_id, memory_id, decision)


def process_event(conn, event_id):
    """Analyze one pending event after it is durable in the journal.

    Explicit durable user signals become private candidate memories. Ambiguous
    events stay pending for a future semantic analyzer or operator review.
    """
    mutation_path = False
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute(
            'SELECT project_id, agent_id, event_type, user_content, metadata, status '
            'FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        if row is None:
            raise core.MemCoreError(f'ingest event not found: {event_id}')
        project_id, agent_id, event_type, user_content, metadata_raw, status = row
        try:
            metadata = json.loads(metadata_raw or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if status != 'pending':
            conn.execute('ROLLBACK')
            return {'event_id': event_id, 'status': status}
        if event_type == 'memory_write':
            action = str(metadata.get('action') or '').strip().lower()
            if action in ('replace', 'remove'):
                # Built-in mutations call core write APIs that own their own
                # transactions. Release this event-classification transaction
                # before entering that path.
                conn.execute('ROLLBACK')
                mutation_path = True
                return _process_builtin_mutation(conn, event_id)
        core._require_membership(conn, project_id, agent_id)

        if event_type == 'memory_write' and metadata.get('success') is False:
            decision, reason, candidate = (
                'ignore', 'builtin_memory_write_failed_upstream', ''
            )
        elif event_type == 'memory_write' and user_content.strip():
            action = str(metadata.get('action') or 'add').strip().lower()
            if action == 'add':
                decision, reason, candidate = (
                    'candidate', 'explicit_memory_write_add', user_content.strip()[:4000]
                )
            else:
                decision, reason, candidate = (
                    'review', f'builtin_memory_{action or "unknown"}_requires_review', ''
                )
        else:
            decision, reason, candidate = classify_user_text(user_content)
        now = core._now()
        if decision == 'ignore':
            conn.execute(
                "UPDATE ingest_event SET status='ignored', decision=?, processed_at=? WHERE id=?",
                (reason, now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'ignored', 'decision': reason}
        if decision == 'review':
            conn.execute(
                'UPDATE ingest_event SET decision=? WHERE id=?', (reason, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'pending', 'decision': reason}
        claim_fp = core.fingerprint(candidate)
        existing = _find_private_claim(conn, project_id, agent_id, claim_fp)
        if existing:
            conn.execute(
                "INSERT OR IGNORE INTO ingest_derivation "
                "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'duplicate', ?)",
                (event_id, existing, now)
            )
            conn.execute(
                "UPDATE ingest_event SET status='processed', decision='duplicate', processed_at=? "
                'WHERE id=?', (now, event_id)
            )
            conn.execute('COMMIT')
            return {'event_id': event_id, 'status': 'processed',
                    'decision': 'duplicate', 'memory_id': existing}

        memory_id, _version_id = core.create_memory(
            conn, project_id, agent_id, candidate,
            scope='private', memory_type='observation', lifecycle='candidate',
            idempotency_key=f'ingest:{event_id}',
            reason='native provider journal analysis', _manage_transaction=False
        )
        conn.execute(
            "INSERT INTO ingest_derivation "
            "(event_id, memory_id, relation, created_at) VALUES (?, ?, 'created', ?)",
            (event_id, memory_id, now)
        )
        conn.execute(
            "UPDATE ingest_event SET status='processed', decision='private_candidate', processed_at=? "
            'WHERE id=?', (now, event_id)
        )
        conn.execute('COMMIT')
        return {'event_id': event_id, 'status': 'processed',
                'decision': 'private_candidate', 'memory_id': memory_id}
    except Exception as exc:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        if mutation_path:
            raise
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "UPDATE ingest_event SET status='failed', error=?, processed_at=? WHERE id=?",
                (f'{type(exc).__name__}: {exc}'[:2000], core._now(), event_id)
            )
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
        raise


def journal_stats(conn, project_id=None, agent_id=None):
    """Return a content-free operational snapshot of the ingest journal.

    The snapshot deliberately aggregates metadata only. It never returns raw user,
    assistant, candidate, rationale, or event metadata content, making it suitable
    for health checks and dashboards that should not expose historical prompts.
    """
    filters = []
    params = []
    if project_id is not None:
        filters.append('project_id=?')
        params.append(project_id)
    if agent_id is not None:
        filters.append('agent_id=?')
        params.append(agent_id)
    if project_id is not None and agent_id is not None:
        core._require_membership(conn, project_id, agent_id)

    where = (' WHERE ' + ' AND '.join(filters)) if filters else ''

    def grouped(column, *, extra=''):
        clauses = list(filters)
        local_params = list(params)
        if extra:
            clauses.append(extra)
        local_where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        return dict(conn.execute(
            f'SELECT {column}, COUNT(*) FROM ingest_event{local_where} '
            f'GROUP BY {column} ORDER BY {column}', local_params
        ).fetchall())

    total = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + where, params
    ).fetchone()[0]
    by_status = grouped('status')
    pending_by_decision = grouped(
        "COALESCE(decision, 'unclassified')", extra="status='pending'"
    )
    pending_by_event_type = grouped('event_type', extra="status='pending'")

    pending_clauses = list(filters) + ["status='pending'"]
    pending_where = ' WHERE ' + ' AND '.join(pending_clauses)
    pending_params = list(params)
    semantic_pending = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + pending_where +
        " AND decision IN ('semantic_review_required','semantic_deferred')",
        pending_params
    ).fetchone()[0]
    unresolved_builtin = conn.execute(
        'SELECT COUNT(*) FROM ingest_event' + pending_where +
        " AND decision LIKE 'builtin_memory_%' "
        "AND (decision LIKE '%_unresolved_target' "
        "OR decision LIKE '%_missing_old_text' "
        "OR decision='builtin_memory_replace_missing_content' "
        "OR decision LIKE '%_requires_review')",
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

    analysis_filters = []
    analysis_params = []
    if project_id is not None:
        analysis_filters.append('e.project_id=?')
        analysis_params.append(project_id)
    if agent_id is not None:
        analysis_filters.append('e.agent_id=?')
        analysis_params.append(agent_id)
    analysis_where = (
        ' WHERE ' + ' AND '.join(analysis_filters) if analysis_filters else ''
    )
    analysis_total = conn.execute(
        'SELECT COUNT(*) FROM ingest_analysis a '
        'JOIN ingest_event e ON e.id=a.event_id' + analysis_where,
        analysis_params
    ).fetchone()[0]
    analysis_by_verdict = dict(conn.execute(
        'SELECT a.verdict, COUNT(*) FROM ingest_analysis a '
        'JOIN ingest_event e ON e.id=a.event_id' + analysis_where +
        ' GROUP BY a.verdict ORDER BY a.verdict',
        analysis_params
    ).fetchall())

    if by_status.get('failed', 0):
        health = 'failed'
    elif unresolved_builtin:
        health = 'operator_attention'
    elif semantic_pending:
        health = 'review_pending'
    else:
        health = 'ok'

    return {
        'health': health,
        'total_events': total,
        'by_status': by_status,
        'pending_by_decision': pending_by_decision,
        'pending_by_event_type': pending_by_event_type,
        'semantic_review_pending': semantic_pending,
        'unresolved_builtin_mutations': unresolved_builtin,
        'oldest_pending_at': oldest,
        'oldest_pending_age_seconds': oldest_age_seconds,
        'analysis': {
            'total': analysis_total,
            'by_verdict': analysis_by_verdict,
        },
    }


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
    conn.execute('BEGIN IMMEDIATE')
    try:
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
                    'rationale': str(rationale or '')[:1000]}
        )
        conn.execute('COMMIT')
        return {
            'event_id': event_id,
            'status': 'ignored',
            'decision': dismissed_decision
        }
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise


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

    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(sql, params)
        event_ids = [row[0] for row in cur.fetchall()]
        if not event_ids:
            conn.execute('COMMIT')
            return 0
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i:i+500]
            marks = ','.join('?' for _ in chunk)
            conn.execute(f'DELETE FROM ingest_derivation WHERE event_id IN ({marks})', chunk)
            conn.execute(f'DELETE FROM ingest_analysis WHERE event_id IN ({marks})', chunk)
            conn.execute(f'DELETE FROM ingest_event WHERE id IN ({marks})', chunk)
        conn.execute('COMMIT')
        return len(event_ids)
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass
        raise
