"""Raw Hermes event journal and conservative admission bridge for MemCore.

The journal is append-only ingress. Raw turns are never recalled directly.
Only the analyzer may derive canonical MemCore memories. Automatic
derivations are private candidates by default, except the two auto-accept
lanes (ADR-0019): explicit durable signals and high-confidence semantic
verdicts (confidence >= 0.95).
"""
import hashlib
import json
import re
import sqlite3

from . import core
from . import store

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
_NEGATIVE_MEMORY_RE = re.compile(
    r'\b(?:without|don\'t|dont|do not|never|not|no need to)\s+(?:explicit\s+)?remember\b|'
    r'(?:ไม่ต้อง|อย่า|ห้าม|ไม่ได้)\s*(?:จำ|ช่วยจำ)',
    re.IGNORECASE
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


def _has_remove_reference(event_type, metadata):
    """True when a memory_write/remove event carries its old_text reference."""
    memory_action = str(metadata.get('action') or '').strip().lower()
    builtin_metadata = metadata.get('builtin_metadata')
    old_text = metadata.get('old_text')
    if not old_text and isinstance(builtin_metadata, dict):
        old_text = builtin_metadata.get('old_text')
    return (
        event_type == 'memory_write' and memory_action == 'remove'
        and isinstance(old_text, str) and bool(old_text.strip())
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


def classify_user_text(text):
    """Deterministic first-stage gate. It never trusts assistant output."""
    text = (text or '').strip()
    if not text or text.startswith('/') or '<memory-context>' in text.lower():
        return 'ignore', 'non_user_signal', ''
    if _TRIVIAL_RE.fullmatch(text):
        return 'ignore', 'trivial', ''
    if _NEGATIVE_MEMORY_RE.search(text):
        return 'review', 'semantic_review_required', ''
    if _EXPLICIT_MEMORY_RE.search(text):
        candidate = _LEADING_REMEMBER_RE.sub('', text).strip() or text
        if len(candidate) > MAX_ANALYSIS_CANDIDATE_CHARS or '```' in candidate:
            return 'review', 'semantic_review_required', ''
        return 'candidate', 'explicit_durable_signal', candidate
    return 'review', 'semantic_review_required', ''


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
        result = _admit_journal_candidate(
            conn, event_id, project_id, agent_id, candidate,
            idempotency_key=f'ingest:{event_id}',
            reason='native provider journal analysis',
            decision_created='private_accepted',
            decision_accepted='private_accepted',
            auto_accept_reason='explicit durable signal auto-accept',
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
