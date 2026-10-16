"""MemCore Ã¢â‚¬â€ core memory operations.

All writes: tombstone admission guard -> short transaction -> audit event.
All reads: scope enforced in SQL WHERE (never post-filtering).
"""

__version__ = "1.0.0"
import array
import hashlib
import json
import math
import operator
import pathlib
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone

from . import store
from . import ablation as _ablation
from . import embedding


class MemCoreError(Exception):
    pass


class TombstoneBlocked(MemCoreError):
    def __init__(self, fingerprint, reason):
        self.fingerprint = fingerprint
        self.reason = reason
        super().__init__(
            f'claim blocked by active tombstone ({fingerprint[:8]}...): {reason}'
        )


class ContradictionHold(MemCoreError):
    """An auto-accept was refused because the claim contradicts live memory.

    The row IS created — as ``conflict``, paired with the memory it disagrees
    with, and audited as ``contradiction-hold``. Nothing is resolved here:
    supersede or reject by an owner decides. Carries the new memory id so the
    caller can report it instead of losing the write silently.
    """
    def __init__(self, hits, memory_id=None):
        self.hits = list(hits)
        self.memory_id = memory_id
        first = self.hits[0] if self.hits else ('', 'unknown')
        super().__init__(
            'auto-accept held: claim contradicts live memory '
            f'{first[0]} ({first[1]})'
        )


class PermissionDenied(MemCoreError):
    pass


class NotFound(MemCoreError):
    pass


class AmbiguousTombstonePrefix(MemCoreError):
    """A fingerprint prefix matched more than one active refusal guard."""
    def __init__(self, prefix, candidates):
        self.prefix = prefix
        self.candidates = candidates
        super().__init__(
            f'ambiguous refusal-guard prefix {prefix!r}: '
            f'matches {len(candidates)} active guards; '
            'use a longer prefix or the tombstone id'
        )


#: High-precision secret patterns scrubbed before any disk write.
#: Deliberately narrow: env-var NAMES (DISCORD_BOT_TOKEN) and normal
#: prose ("password policy") must pass through untouched. Only shapes
#: that are unmistakably credential VALUES are redacted.
#: FREEZE (P Choke 2026-10-14): CI deleted — the live store is the only
#: verification lane. This list MUST NOT gain broad patterns (e.g. bare
#: hex blobs, "token" words, env-var names) without a RED test proving
#: the new shape fires AND guard tests proving prose/names survive.
# ponytail: regex list, one pass; add a pattern only with a RED test proving it fires + a guard test proving prose survives.
_SECRET_PATTERNS = (
    # OpenAI-style keys: sk-<20+ alnum> (also sk-proj-, sk-ant-).
    re.compile(r'sk-(?:proj|ant)-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}'),
    # GitHub PATs: ghp_/gho_/github_pat_ + long token.
    re.compile(r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'),
    # Slack tokens: xox[baprs]-... (+ xoxe.xox[bap]-...).
    re.compile(r'xoxe\.xox[barps]-[A-Za-z0-9-]+|xox[barps]-[A-Za-z0-9-]+'),
    # AWS access key IDs: AKIA + 16 uppercase alnum.
    re.compile(r'AKIA[0-9A-Z]{16}'),
    # AWS secret access keys: 40-char base64-ish after an explicit label.
    re.compile(r'(?i)\b(?:aws[_-]?secret[_-]?access[_-]?key)\b\s*[:=]\s*[A-Za-z0-9/+=]{40}'),
    # Generic bearer/API-key assignments with a long opaque value.
    re.compile(r'(?i)\b(?:bearer|api[_-]?key|secret[_-]?key|access[_-]?token)\b\s*[:=]\s*[A-Za-z0-9_\-.~+/=]{20,}'),
    # Discord bot tokens: 3 dot-separated base64url segments (~59+ chars).
    re.compile(r'[MN][A-Za-z\d]{23}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27}'),
    # PEM private-key blocks (header alone is enough to refuse the value).
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY(?: BLOCK)?-----'),
    # HuggingFace user access tokens: hf_ + long alnum tail.
    re.compile(r'\bhf_[A-Za-z0-9]{34,}\b'),
    # Google API keys: AIza + 35 chars of the documented alphabet.
    re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b'),
)
_SECRET_REDACTED = '«redacted:secret»'


def scrub_secrets(content: str) -> str:
    """Redact unmistakable credential values; names and prose survive."""
    if not isinstance(content, str) or not content:
        return content
    out = content
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_SECRET_REDACTED, out)
    return out


class RejectResult(dict):
    """Outcome of reject(): a dict that stays truthy-compatible.

    Existing callers treat reject() as a boolean (True = this call moved the
    row to rejected). RejectResult preserves that: bool(result) is True only
    when this call performed the transition. New callers read the counts.
    Keys: rejected, tombstone_created, tombstone_id (the guard this call
    created, or the pre-existing guard it reused), swept, swept_ids.
    """
    def __bool__(self):
        return bool(self.get('rejected'))


# ————— helpers ————————————————————————————————————————————————————————————————

def fingerprint(content: str) -> str:
    """Deterministic claim fingerprint: sha256 of normalized (whitespace-collapsed,
    lowercased, NFC-normalized) content, truncated to 16 hex chars — matches fixtures._fingerprint."""
    normalized = unicodedata.normalize('NFC', ' '.join(content.lower().strip().split()))
    return hashlib.sha256(normalized.encode('utf-8', errors='replace')).hexdigest()[:16]


def _new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def _source_metadata(source_file=None, source_commit=None, source_hash=None):
    """Normalize file provenance and bind its SHA-256 to the saved contents."""
    if source_file is None:
        if source_hash is not None:
            raise MemCoreError('source_hash requires source_file')
        return None, source_commit, None
    try:
        path = pathlib.Path(source_file).expanduser().resolve(strict=True)
        if not path.is_file():
            raise MemCoreError('source_file must be a regular file')
        current_hash = _sha256_file(path)
    except MemCoreError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MemCoreError(f'cannot read source_file: {exc}') from exc
    if source_hash is not None:
        if not isinstance(source_hash, str) or not re.fullmatch(
                r'[0-9a-fA-F]{64}', source_hash):
            raise MemCoreError('source_hash must be a 64-character SHA-256 hex digest')
        if source_hash.lower() != current_hash:
            raise MemCoreError('source_hash does not match source_file contents')
    return current_hash, source_commit, str(path)


def _sha256_file(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    """ISO 8601 UTC with Z suffix Ã¢â‚¬â€ the timestamp contract (migration 0005)."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _audit(conn, action, actor, memory_id=None, project_id=None, detail=None,
           write_key=None):
    conn.execute(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, write_key, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (action, actor, memory_id, project_id, json.dumps(detail or {}), write_key, _now())
    )


def _private_tombstone_scope(project_id: str, agent_id: str) -> str:
    return f'private:{project_id}:{agent_id}'


def _tombstone_scope(project_id: str, memory_scope: str, owner_agent_id: str) -> str:
    if memory_scope == 'private':
        return _private_tombstone_scope(project_id, owner_agent_id)
    return project_id


def admission_allowed(conn, content: str, project_id: str,
                      scope: str = 'project', agent_id: str | None = None) -> bool:
    """Tombstone admission guard with project/private scope hierarchy."""
    blocked = _tombstone_active(
        conn, fingerprint(content), project_id, scope=scope, agent_id=agent_id
    )
    return blocked is None


def _active_tombstone_row(conn, claim_fp, project_id, scope='project', agent_id=None):
    """The active refusal guard a lookup would hit: (id, reason) or None."""
    scopes = [project_id, 'global']
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('private tombstone lookup requires agent_id')
        scopes.insert(0, _private_tombstone_scope(project_id, agent_id))
    elif scope != 'project':
        raise MemCoreError(f'invalid tombstone lookup scope: {scope}')
    marks = ','.join('?' for _ in scopes)
    cur = conn.execute(
        'SELECT id, reason FROM tombstone '
        f'WHERE claim_fingerprint = ? AND scope IN ({marks}) '
        'AND overridden_by IS NULL ORDER BY '
        "CASE scope WHEN 'global' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
        (claim_fp, *scopes)
    )
    return cur.fetchone()


def _tombstone_active(conn, claim_fp, project_id, scope='project', agent_id=None):
    row = _active_tombstone_row(
        conn, claim_fp, project_id, scope=scope, agent_id=agent_id)
    return (row[1],) if row else None


def _active_tombstone_id(conn, claim_fp, project_id, scope='project', agent_id=None):
    """Id of the active guard _tombstone_active would report, or None."""
    row = _active_tombstone_row(
        conn, claim_fp, project_id, scope=scope, agent_id=agent_id)
    return row[0] if row else None


def _require_nonempty_reason(reason):
    """A refusal guard stores no content; a non-empty reason is its identity."""
    if not isinstance(reason, str) or not reason.strip():
        raise MemCoreError(
            'reject requires a non-empty reason '
            '(the refusal guard stores no content; the reason is its identity)'
        )
    return reason


def _sweep_duplicate_claims(conn, project_id, claim_fp, refusal_scope,
                             actor_agent_id, reason, exclude_memory_id,
                             tombstone_id):
    """Reject live same-claim rows covered by one refusal guard.

    One indexed query on (project_id, scope, claim_fingerprint, lifecycle) —
    O(same-fingerprint), never a full-table scan. Only rows whose guard
    mapping equals refusal_scope are swept, so a project guard sweeps project
    rows and a private guard sweeps that owner's private rows; other lanes
    are left alone. Each swept row moves to rejected with its own 'reject'
    audit (swept:true, no write_key — recovery attribution stays on the
    primary row). Legacy NULL-fingerprint rows never match; they are already
    excluded from recall by the guard predicate. Returns [swept_ids].
    """
    if refusal_scope.startswith('private:'):
        parts = refusal_scope.split(':', 2)
        if len(parts) != 3 or parts[1] != project_id:
            raise MemCoreError(f'invalid private refusal scope: {refusal_scope}')
        sql = (
            'SELECT id FROM memory WHERE project_id=? AND scope=\'private\' '
            'AND owner_agent_id=? AND claim_fingerprint=? '
            'AND lifecycle IN (\'candidate\',\'accepted\',\'conflict\') '
        )
        args = [project_id, parts[2], claim_fp]
    else:
        if refusal_scope != project_id:
            raise MemCoreError(f'invalid project refusal scope: {refusal_scope}')
        sql = (
            'SELECT id FROM memory WHERE project_id=? AND scope=\'project\' '
            'AND claim_fingerprint=? '
            'AND lifecycle IN (\'candidate\',\'accepted\',\'conflict\') '
        )
        args = [project_id, claim_fp]
    if exclude_memory_id is not None:
        sql += 'AND id != ? '
        args.append(exclude_memory_id)
    sql += 'ORDER BY id'
    swept = []
    now = _now()
    for (dup_id,) in conn.execute(sql, args).fetchall():
        conn.execute(
            "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
            (now, dup_id)
        )
        _audit(conn, 'reject', actor_agent_id, dup_id, project_id,
               {'reason': reason, 'swept': True, 'tombstone_id': tombstone_id})
        swept.append(dup_id)
    return swept


def _membership_role(conn, project_id, agent_id):
    if not isinstance(project_id, str) or not isinstance(agent_id, str):
        return None
    row = conn.execute(
        'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
        (project_id, agent_id)
    ).fetchone()
    return row[0] if row else None


def _require_membership(conn, project_id, agent_id):
    role = _membership_role(conn, project_id, agent_id)
    if role is None:
        raise PermissionDenied(
            f'agent {agent_id} is not a member of project {project_id}'
        )
    return role


def _current_claim_identity(conn, memory_id):
    """Return (current_version_id, fingerprint), lazily repairing legacy NULLs."""
    if not isinstance(memory_id, str):
        raise NotFound(f'memory {memory_id} not found')
    row = conn.execute(
        'SELECT current_version_id, claim_fingerprint FROM memory WHERE id=?',
        (memory_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f'memory {memory_id} not found')
    version_id, claim_fp = row
    if claim_fp:
        return version_id, claim_fp
    content_row = conn.execute(
        'SELECT content FROM memory_version WHERE id=?', (version_id,)
    ).fetchone()
    if content_row is None:
        raise MemCoreError(f'current version {version_id} is missing for memory {memory_id}')
    claim_fp = fingerprint(content_row[0])
    # H3: lazy repair is best-effort — a read-only handle (mode=ro) must keep
    # serving the RAM-computed fingerprint instead of crashing on the UPDATE.
    try:
        conn.execute(
            'UPDATE memory SET claim_fingerprint=? '
            'WHERE id=? AND claim_fingerprint IS NULL',
            (claim_fp, memory_id)
        )
    except sqlite3.OperationalError:
        pass
    return version_id, claim_fp


def _require_memory_write_access(conn, memory_id, agent_id):
    """Return metadata only when the caller is a member of the memory project.

    The membership join deliberately makes nonexistent and inaccessible memory
    ids indistinguishable to non-members, avoiding a cross-project existence
    oracle at mutation boundaries.
    """
    if not isinstance(memory_id, str) or not isinstance(agent_id, str):
        raise PermissionDenied('memory is not accessible to this agent')
    mem = conn.execute(
        'SELECT m.project_id, m.scope, m.owner_agent_id, m.lifecycle, pm.role '
        'FROM memory m JOIN project_membership pm '
        'ON pm.project_id=m.project_id AND pm.agent_id=? '
        'WHERE m.id=?',
        (agent_id, memory_id)
    ).fetchone()
    if not mem:
        raise PermissionDenied('memory is not accessible to this agent')
    project_id, scope, owner, lifecycle, role = mem
    if scope == 'private' and agent_id != owner and role != 'owner':
        raise PermissionDenied(
            f'agent {agent_id} cannot modify private memory owned by {owner}'
        )
    return project_id, scope, owner, lifecycle, role


# Ã¢â€â‚¬Ã¢â€â‚¬ writes Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

#: Allowed scope_detail prefixes. A detail tag subdivides private scope
#: for filtering; it never widens read access (project -> members, anything
#: else -> owner only, unchanged).
SCOPE_DETAIL_PREFIXES = ('skill:', 'episode:', 'session:')


def _validate_scope_detail(scope, scope_detail):
    """Detail tags subdivide private scope only. Project memories stay
    untagged so shared recall never silently narrows."""
    if scope_detail is None:
        return None
    detail = str(scope_detail).strip()[:128]
    if not detail:
        return None
    if scope != 'private':
        raise MemCoreError(
            'scope_detail is only allowed on private-scope memories')
    if not detail.startswith(SCOPE_DETAIL_PREFIXES):
        raise MemCoreError(
            f'invalid scope_detail {detail!r}; '
            f'must start with one of {SCOPE_DETAIL_PREFIXES}')
    return detail


def _fetch_embedding_safe(content: str):
    """Best-effort embedding fetch for use OUTSIDE write transactions (H1).

    Network/model I/O must never run while a BEGIN IMMEDIATE write lock is
    held: with five agents on one store it blocks every writer and can blow
    the busy_timeout. Callers precompute the vector here and persist it
    inside the transaction with ``_store_embedding_safe`` (pure SQL).
    """
    try:
        vec = embedding.get_embedding(content, timeout=0.1)
        return vec if vec else None
    except Exception:
        return None


def _store_embedding_safe(conn, memory_id: str, version_id: str, vec) -> None:
    """Persist a precomputed embedding with SQL only (safe inside a write tx).

    Batched callers that already hold a transaction (``_manage_transaction=
    False``) pass ``vec=None`` and skip embedding instead of firing per-row
    HTTP inside the lock; ``memcore embed`` backfills those rows later.
    """
    if not vec:
        return
    if store._table_exists(conn, 'memory_embedding'):
        try:
            store.store_embedding(conn, memory_id, version_id,
                                  embedding.DEFAULT_MODEL, vec)
        except Exception:
            pass


def _prepare_memory_write(content, scope, lifecycle, reason, scope_detail,
                          source_file, source_commit, source_hash):
    """Validate + normalize one create payload (pure; no conn, no writes).

    Ordering is the compatibility contract: scope -> content shape ->
    lifecycle -> scrub -> source provenance -> scope detail. Returns every
    derived value the transactional section below needs.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    if not isinstance(content, str) or not content.strip():
        raise MemCoreError('content must be a non-empty string')
    try:
        content.encode('utf-8')
    except UnicodeEncodeError:
        raise MemCoreError('content contains unencodable surrogate characters')
    if lifecycle not in ('candidate', 'accepted', 'conflict'):
        raise MemCoreError(
            f'invalid initial lifecycle: {lifecycle}; use an explicit transition'
        )
    content = scrub_secrets(content)
    if not content.strip():
        raise MemCoreError('content must be a non-empty string')
    if '\x00' in content:
        raise MemCoreError('content must not contain null bytes')
    reason = scrub_secrets(reason) if isinstance(reason, str) else reason
    source_hash, source_commit, source_file = _source_metadata(
        source_file, source_commit, source_hash
    )
    return {
        'content': content,
        'reason': reason,
        'detail': _validate_scope_detail(scope, scope_detail),
        'source_hash': source_hash,
        'source_commit': source_commit,
        'source_file': source_file,
        'claim_fp': fingerprint(content),
    }


def _replay_idempotent_create(conn, idempotency_key, project_id, agent_id,
                              role, scope, claim_fp):
    """Resolve one idempotency replay; None when the key is new.

    Re-raises exactly the guards a fresh create would: cross-project keys,
    private-scope access, scope/content reuse, tombstone veto, conflict
    replay and terminal targets. Policy changes after the original request
    still apply to a replay — returning a stale success would undermine the
    refusal fingerprint or the recorded conflict without creating a row.
    """
    row = conn.execute(
        'SELECT ik.project_id, ik.memory_id, ik.version_id, '
        '       m.scope, m.owner_agent_id, m.lifecycle, '
        '       iv.content '
        'FROM idempotency_key ik '
        'JOIN memory m ON m.id=ik.memory_id '
        'JOIN memory_version iv ON iv.id=ik.version_id '
        'WHERE ik.key=?',
        (idempotency_key,)
    ).fetchone()
    if row is None:
        return None
    (existing_project, existing_memory, existing_version,
     existing_scope, existing_owner, existing_lifecycle,
     original_content) = row
    if existing_project != project_id:
        raise PermissionDenied(
            'idempotency key belongs to a different project'
        )
    if existing_scope == 'private' and agent_id != existing_owner and role != 'owner':
        raise PermissionDenied('idempotency replay cannot access private memory')
    if existing_scope != scope:
        raise MemCoreError(
            f'idempotency key reused with different scope '
            f'({existing_scope} != {scope})'
        )
    if fingerprint(original_content) != claim_fp:
        raise MemCoreError('idempotency key reused with different content')
    blocked = _tombstone_active(
        conn, claim_fp, project_id,
        scope=existing_scope, agent_id=existing_owner
    )
    if blocked:
        raise TombstoneBlocked(claim_fp, blocked[0])
    if existing_lifecycle == 'conflict':
        # A replay must not launder the refusal into a success. The first
        # attempt committed the conflict row and this key before raising, so
        # the retry took the replay path and returned OK for a row that can
        # never be recalled as accepted. Re-raise so the caller keeps seeing
        # the refusal; supersede/reject is the documented way to resolve it.
        raise ContradictionHold(
            [(existing_memory, 'replay_of_conflict')], existing_memory)
    if existing_lifecycle in ('disabled', 'rejected', 'superseded'):
        raise MemCoreError(
            f'idempotent target is terminal (lifecycle={existing_lifecycle})'
        )
    return existing_memory, existing_version


def _insert_memory_rows(conn, project_id, agent_id, content, scope, memory_type,
                        lifecycle, claim_fp, reason, detail, source_hash,
                        source_commit, source_file):
    """Insert the memory header + first immutable version; returns (mem, ver, now).

    Scope_detail is schema-conditional: the column only exists after
    migration 0016, and an explicit detail tag on a pre-0016 store must
    refuse loudly instead of being silently dropped.
    """
    mem_id = _new_id('mem')
    ver_id = _new_id('ver')
    now = _now()
    mem_cols = _table_cols(conn, 'memory')
    if detail is not None and 'scope_detail' not in mem_cols:
        raise MemCoreError(
            'scope_detail requires migration 0016; open the store normally first')
    if 'scope_detail' in mem_cols:
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id, claim_fingerprint, '
            '  created_at, updated_at, scope_detail) '
            "VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'current', ?, ?, ?, ?, ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, ver_id, claim_fp, now, now, detail)
        )
    else:
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id, claim_fingerprint, '
            '  created_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'current', ?, ?, ?, ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, ver_id, claim_fp, now, now)
        )
    conn.execute(
        'INSERT INTO memory_version (id, memory_id, content, reason, '
        '  created_by_agent_id, created_at, valid_from, source_hash, '
        '  source_commit, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (ver_id, mem_id, content, reason, agent_id, now, now,
         source_hash, source_commit, source_file)
    )
    return mem_id, ver_id, now


def create_memory(conn, project_id, agent_id, content, scope='private',
                  memory_type='fact', lifecycle='candidate', idempotency_key=None,
                  reason=None, scope_detail=None, source_file=None,
                  source_commit=None, source_hash=None,
                  _manage_transaction=True):
    """Create a memory + first immutable version. Tombstone guard applies.

    Returns (memory_id, version_id) or existing ids if idempotency_key replays.
    """
    if not isinstance(memory_type, str) or not memory_type.strip():
        raise MemCoreError('memory_type must be a non-empty string')
    prepared = _prepare_memory_write(
        content, scope, lifecycle, reason, scope_detail,
        source_file, source_commit, source_hash
    )
    content = prepared['content']
    claim_fp = prepared['claim_fp']

    # H1: embedding fetch is network/model I/O and runs OUTSIDE the write
    # lock. Batched callers (manage=False) already hold a transaction and
    # skip embedding rather than firing HTTP inside it.
    embed_vec = None
    if _manage_transaction:
        try:
            if store._table_exists(conn, 'memory_embedding'):
                embed_vec = _fetch_embedding_safe(content)
        except Exception:
            embed_vec = None

    conflict_hits = []
    with store.write_tx(conn, manage=_manage_transaction):
        # Membership is the first project-boundary gate. Do not reveal
        # idempotency/tombstone state to a non-member by varying the error.
        role = _require_membership(conn, project_id, agent_id)

        if idempotency_key:
            replayed = _replay_idempotent_create(
                conn, idempotency_key, project_id, agent_id, role, scope,
                claim_fp
            )
            if replayed is not None:
                return replayed

        blocked = _tombstone_active(
            conn, claim_fp, project_id, scope=scope, agent_id=agent_id
        )
        if blocked:
            raise TombstoneBlocked(claim_fp, blocked[0])

        # Gate every auto-accept at the single choke point. Previously only the
        # ingest and semantic lanes called pre_accept_conflict_check, so the
        # explicit memory_remember lane created 'accepted' rows that could
        # silently disagree with live memory and were never marked conflict.
        # Candidates keep the old behaviour (a later accept runs the gate).
        if lifecycle == 'accepted':
            conflict_hits = pre_accept_conflict_check(conn, project_id, content)
            if conflict_hits:
                lifecycle = 'conflict'

        mem_id, ver_id, now = _insert_memory_rows(
            conn, project_id, agent_id, content, scope, memory_type,
            lifecycle, claim_fp, prepared['reason'], prepared['detail'],
            prepared['source_hash'], prepared['source_commit'],
            prepared['source_file']
        )
        _store_embedding_safe(conn, mem_id, ver_id, embed_vec)
        _audit(conn, 'create', agent_id, mem_id, project_id,
               {'memory_id': mem_id, 'version_id': ver_id,
                'scope': scope, 'content': content,
                'contradiction_hold': [list(h) for h in conflict_hits]},
               write_key=idempotency_key)
        if idempotency_key:
            conn.execute(
                'INSERT INTO idempotency_key (key, project_id, memory_id, version_id, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (idempotency_key, project_id, mem_id, ver_id, now)
            )
        if conflict_hits:
            # Demote ONLY the refused row. Touching the other side would let any
            # agent demote an established fact by writing something wrong: the
            # good row goes to 'conflict', and re-stating the truth then
            # contradicts the refused row and is held too, with no way back.
            # The ingest lane still marks both sides (both rows are its own
            # auto-accepts); this lane refused the write, so it owes nothing.
            _audit(conn, 'contradiction-hold', agent_id, mem_id, project_id,
                   {'hits': [list(h) for h in conflict_hits]})
    if conflict_hits:
        raise ContradictionHold(conflict_hits, mem_id)
    return mem_id, ver_id


def _validate_supersede_content(new_content):
    """Validate + scrub replacement content; same contract as create content."""
    if not isinstance(new_content, str) or not new_content.strip():
        raise MemCoreError('new_content must be a non-empty string')
    try:
        new_content.encode('utf-8')
    except UnicodeEncodeError:
        raise MemCoreError('new_content contains unencodable surrogate characters')
    new_content = scrub_secrets(new_content)
    if not new_content.strip():
        raise MemCoreError('new_content must be a non-empty string')
    if '\x00' in new_content:
        raise MemCoreError('new_content must not contain null bytes')
    return new_content


def _close_and_insert_version(conn, memory_id, agent_id, new_content, reason,
                              old_ver, new_fp, source_hash, source_commit,
                              source_file):
    """Close the old validity window, insert the new version, reset trust.

    The changed claim does not inherit acceptance/verification from the old
    version; it must earn trust again through feedback/evidence.
    """
    now = _now()
    new_ver = _new_id('ver')
    conn.execute(
        'UPDATE memory_version SET valid_until=? '
        'WHERE id=? AND valid_until IS NULL',
        (now, old_ver)
    )
    conn.execute(
        'INSERT INTO memory_version (id, memory_id, content, reason, '
        '  created_by_agent_id, supersedes_version_id, created_at, valid_from, '
        '  source_hash, source_commit, source_file) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (new_ver, memory_id, new_content, reason, agent_id, old_ver, now, now,
         source_hash, source_commit, source_file)
    )
    conn.execute(
        "UPDATE memory SET current_version_id=?, claim_fingerprint=?, lifecycle='candidate', "
        "verification='unverified', freshness='current', updated_at=? "
        'WHERE id=?',
        (new_ver, new_fp, now, memory_id)
    )
    return new_ver, now


def supersede(conn, memory_id, agent_id, new_content, reason=None, write_key=None,
              source_file=None, source_commit=None, source_hash=None):
    """Correct a memory in place while preserving immutable version history.

    The old claim receives a scope-appropriate refusal fingerprint, its
    validity interval closes, and the replacement returns to candidate /
    unverified so trust is never inherited across changed content.
    """
    new_content = _validate_supersede_content(new_content)
    reason = scrub_secrets(reason) if isinstance(reason, str) else reason
    source_hash, source_commit, source_file = _source_metadata(
        source_file, source_commit, source_hash
    )
    # H1: fetch the replacement's embedding before taking the write lock.
    embed_vec = None
    try:
        if store._table_exists(conn, 'memory_embedding'):
            embed_vec = _fetch_embedding_safe(new_content)
    except Exception:
        embed_vec = None
    with store.write_tx(conn):
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )

        if lifecycle in ('rejected', 'disabled', 'superseded'):
            raise MemCoreError(
                f'cannot correct terminal memory (lifecycle={lifecycle})'
            )

        new_fp = fingerprint(new_content)
        blocked = _tombstone_active(
            conn, new_fp, project_id, scope=scope, agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(new_fp, blocked[0])

        old_ver, old_fp = _current_claim_identity(conn, memory_id)
        if old_fp == new_fp:
            raise MemCoreError('new_content is equivalent to the current claim')

        new_ver, now = _close_and_insert_version(
            conn, memory_id, agent_id, new_content, reason, old_ver, new_fp,
            source_hash, source_commit, source_file)
        _store_embedding_safe(conn, memory_id, new_ver, embed_vec)

        tombstone_created = False
        if old_fp != new_fp and not _tombstone_active(
            conn, old_fp, project_id, scope=scope, agent_id=owner
        ):
            refusal_scope = _tombstone_scope(project_id, scope, owner)
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (_new_id('tomb'), old_fp, refusal_scope,
                 'corrected' if not reason else f'corrected: {reason}', now)
            )
            tombstone_created = True
        _audit(conn, 'supersede', agent_id, memory_id, project_id,
               {'new_version_id': new_ver, 'old_version_id': old_ver,
                'reason': reason, 'old_claim_tombstoned': tombstone_created,
                'lifecycle_reset': 'candidate',
                'verification_reset': 'unverified'}, write_key=write_key)
    return new_ver


def supersede_memory(conn, old_memory_id, agent_id, new_content, reason=None,
                     new_project_id=None, source_file=None,
                     source_commit=None, source_hash=None):
    """Correction model: supersede old memory in place.

    Cross-project moves are not part of the correction model. Reject an
    explicit different new_project_id instead of silently ignoring it.
    """
    if new_project_id is not None:
        project_id, _scope, _owner, _lifecycle, _role = _require_memory_write_access(
            conn, old_memory_id, agent_id
        )
        if new_project_id != project_id:
            raise MemCoreError(
                'supersede_memory cannot move a memory across projects'
            )
    return supersede(
        conn, old_memory_id, agent_id, new_content, reason,
        source_file=source_file, source_commit=source_commit,
        source_hash=source_hash
    )


def promote(conn, memory_id, agent_id):
    """Promote private -> project scope. Audited. Owner or project owner only."""
    with store.write_tx(conn):
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        _current_version_id, claim_fp = _current_claim_identity(conn, memory_id)
        if scope != 'private':
            raise MemCoreError('memory is not private')
        if lifecycle in ('rejected', 'disabled', 'superseded'):
            raise MemCoreError(
                f'cannot promote terminal memory (lifecycle={lifecycle})'
            )

        if agent_id != owner and role != 'owner':
            raise PermissionDenied(
                f'only the owner or a project owner may promote {memory_id}'
            )

        blocked = _tombstone_active(
            conn, claim_fp, project_id,
            scope='private', agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(claim_fp, blocked[0])

        conn.execute(
            "UPDATE memory SET scope='project', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        _audit(conn, 'promote', agent_id, memory_id, project_id,
               {'from_scope': 'private', 'to_scope': 'project'})


def deactivate(conn, memory_id, agent_id, reason=None):
    """Soft delete: lifecycle -> disabled. Audited. Reversible via restore."""
    reason = scrub_secrets(reason) if isinstance(reason, str) else reason
    with store.write_tx(conn):
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle in ('rejected', 'superseded'):
            raise MemCoreError(
                f'cannot deactivate terminal memory (lifecycle={lifecycle})'
            )
        if lifecycle == 'disabled':
            raise MemCoreError('memory is already disabled')
        conn.execute(
            "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        _audit(conn, 'deactivate', agent_id, memory_id, project_id,
               {'reason': reason, 'previous_lifecycle': lifecycle})


def restore(conn, memory_id, agent_id):
    """Undo disable, restoring the lifecycle that was disabled when known."""
    with store.write_tx(conn):
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle != 'disabled':
            raise MemCoreError(f'memory is not disabled (lifecycle={lifecycle})')
        cur_ver = conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        content = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (cur_ver,)
        ).fetchone()[0]
        blocked = _tombstone_active(
            conn, fingerprint(content), project_id,
            scope=scope, agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(fingerprint(content), blocked[0])
        target_lifecycle = 'candidate'
        event = conn.execute(
            "SELECT action, detail FROM audit_event WHERE memory_id=? "
            "AND action IN ('deactivate','disable','gc_disable') "
            "ORDER BY id DESC LIMIT 1",
            (memory_id,)
        ).fetchone()
        if event and event[0] in ('deactivate', 'disable'):
            try:
                previous = json.loads(event[1] or '{}').get('previous_lifecycle')
            except (TypeError, ValueError, json.JSONDecodeError):
                previous = None
            if previous in ('candidate', 'accepted', 'conflict'):
                target_lifecycle = previous
        conn.execute(
            'UPDATE memory SET lifecycle=?, updated_at=? WHERE id=?',
            (target_lifecycle, _now(), memory_id)
        )
        _audit(conn, 'restore', agent_id, memory_id, project_id,
               {'restored_lifecycle': target_lifecycle})


def reject(conn, memory_id, agent_id, reason, create_tombstone=True, write_key=None):
    """Reject a memory and always leave a refusal fingerprint.

    The refusal guard covers one scope lane; every OTHER live row in the
    same lane carrying the same claim is swept to rejected in the same
    transaction (a rejected value may not linger where recall or a later
    accept could resurrect it). Returns a RejectResult (truthy exactly
    when this call moved the row to rejected): rejected, tombstone_created,
    tombstone_id, swept, swept_ids.
    """
    if not create_tombstone:
        raise MemCoreError('rejection requires a tombstone refusal guard')
    _require_nonempty_reason(reason)
    reason = scrub_secrets(reason)
    with store.write_tx(conn):
        project_id, scope, owner, lifecycle, role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        _cur_ver, claim_fp = _current_claim_identity(conn, memory_id)

        if lifecycle == 'rejected':
            tombstone_id = _active_tombstone_id(
                conn, claim_fp, project_id, scope=scope, agent_id=owner
            )
            tombstone_created = False
            if tombstone_id is None:
                refusal_scope = _tombstone_scope(project_id, scope, owner)
                tombstone_id = _new_id('tomb')
                conn.execute(
                    'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (tombstone_id, claim_fp, refusal_scope, reason, _now())
                )
                tombstone_created = True
            else:
                refusal_scope = _tombstone_scope(project_id, scope, owner)
            swept = _sweep_duplicate_claims(
                conn, project_id, claim_fp, refusal_scope,
                agent_id, reason, memory_id, tombstone_id)
            if tombstone_created:
                _audit(conn, 'reject_tombstone_repair', agent_id, memory_id, project_id,
                       {'reason': reason, 'scope': refusal_scope,
                        'swept': len(swept), 'swept_ids': swept})
            elif swept:
                _audit(conn, 'reject', agent_id, memory_id, project_id,
                       {'reason': reason, 'tombstoned': False, 'swept': len(swept),
                        'swept_ids': swept, 'repair_sweep': True},
                       write_key=write_key)
            return RejectResult(rejected=False, tombstone_created=tombstone_created,
                                tombstone_id=tombstone_id, swept=len(swept),
                                swept_ids=swept)

        conn.execute(
            "UPDATE memory SET lifecycle='rejected', updated_at=? WHERE id=?",
            (_now(), memory_id)
        )
        tombstone_created = False
        tombstone_id = _active_tombstone_id(
            conn, claim_fp, project_id, scope=scope, agent_id=owner
        )
        if tombstone_id is None:
            refusal_scope = _tombstone_scope(project_id, scope, owner)
            tombstone_id = _new_id('tomb')
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (tombstone_id, claim_fp, refusal_scope, reason, _now())
            )
            tombstone_created = True
        else:
            refusal_scope = _tombstone_scope(project_id, scope, owner)
        swept = _sweep_duplicate_claims(
            conn, project_id, claim_fp, refusal_scope,
            agent_id, reason, memory_id, tombstone_id)
        _audit(conn, 'reject', agent_id, memory_id, project_id,
               {'reason': reason, 'tombstoned': tombstone_created,
                'tombstone_id': tombstone_id,
                'swept': len(swept), 'swept_ids': swept},
               write_key=write_key)
        return RejectResult(rejected=True, tombstone_created=tombstone_created,
                            tombstone_id=tombstone_id, swept=len(swept),
                            swept_ids=swept)


def reject_value(conn, project_id, agent_id, content, reason):
    """File a project-scope refusal guard for a value with no memory row.

    Pre-emptive form of reject: the claim need not be stored (or may
    already be gone). Inserts the project guard, sweeps live project rows
    carrying the same claim in the same transaction, and blocks later
    admission via the normal guard. Already-guarded values are idempotent
    (no duplicate guard row). Returns
    {'tombstone_id', 'tombstone_created', 'swept', 'swept_ids',
     'fingerprint'}. Project scope is deliberate: a value rejected without
    a row has no owner lane, so the guard must cover the whole project —
    the same lane a project-row reject would cover.
    """
    _require_nonempty_reason(reason)
    reason = scrub_secrets(reason)
    if not isinstance(content, str):
        raise MemCoreError('reject-value content must be a non-empty string')
    try:
        content.encode('utf-8')
    except UnicodeEncodeError:
        raise MemCoreError('reject-value content contains unencodable surrogate characters')
    claim_fp = fingerprint(content)
    if not unicodedata.normalize('NFC', ' '.join(content.lower().strip().split())):
        raise MemCoreError('reject-value content must be a non-empty string')
    _require_membership(conn, project_id, agent_id)
    with store.write_tx(conn):
        existing = _active_tombstone_row(conn, claim_fp, project_id)
        tombstone_created = False
        if existing is None:
            tombstone_id = _new_id('tomb')
            conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (tombstone_id, claim_fp, project_id, reason, _now())
            )
            tombstone_created = True
        else:
            tombstone_id = existing[0]
        swept = _sweep_duplicate_claims(
            conn, project_id, claim_fp, project_id,
            agent_id, reason, None, tombstone_id)
        _audit(conn, 'reject_value', agent_id, None, project_id,
               {'fingerprint': claim_fp, 'tombstone_id': tombstone_id,
                'tombstone_created': tombstone_created,
                'swept': len(swept), 'swept_ids': swept})
        return {'tombstone_id': tombstone_id,
                'tombstone_created': tombstone_created,
                'swept': len(swept), 'swept_ids': swept,
                'fingerprint': claim_fp}


def list_tombstones(conn, project_id, agent_id):
    """Active refusal guards visible in one project, newest first.

    Content-free rows: (id, claim_fingerprint, scope, reason, created_at).
    The guard stores no content, so there is nothing to leak; the
    fingerprint alone cannot reconstruct the claim. Overridden guards are
    excluded — override is the soft-delete and GC purges them after 90d.
    """
    _require_membership(conn, project_id, agent_id)
    scopes = [project_id, 'global', _private_tombstone_scope(project_id, agent_id)]
    marks = ','.join('?' for _ in scopes)
    return conn.execute(
        'SELECT id, claim_fingerprint, scope, reason, created_at FROM tombstone '
        f'WHERE scope IN ({marks}) AND overridden_by IS NULL '
        'ORDER BY datetime(created_at) DESC, id ASC',
        scopes,
    ).fetchall()


def unreject_tombstone(conn, tombstone_ref, agent_id, project_id=None):
    """Lift one refusal guard by exact id or unique fingerprint prefix.

    Exact id routes through the existing override path (same membership
    rules; global still fails closed). Otherwise the ref is a leading
    substring of the 16-char fingerprint, matched against the active
    guards in the caller's member projects: zero matches raise NotFound,
    more than one raises AmbiguousTombstonePrefix with content-free
    (id, scope) candidates. No hard delete — unreject is a soft override
    and GC still purges the row after 90d. Returns
    {'tombstone_id', 'overridden'}.
    """
    if not isinstance(tombstone_ref, str) or not tombstone_ref.strip():
        raise NotFound(f'tombstone {tombstone_ref!r} not found')
    ref = tombstone_ref.strip().lower()
    exact = conn.execute(
        'SELECT id FROM tombstone WHERE id=?', (tombstone_ref,),
    ).fetchone()
    if exact is not None:
        return {'tombstone_id': exact[0],
                'overridden': override_tombstone(conn, exact[0], agent_id)}
    if project_id is not None:
        _require_membership(conn, project_id, agent_id)
        scopes = [project_id, 'global',
                  _private_tombstone_scope(project_id, agent_id)]
    else:
        memberships = conn.execute(
            'SELECT project_id FROM project_membership WHERE agent_id=?',
            (agent_id,),
        ).fetchall()
        if not memberships:
            raise NotFound(f'tombstone {tombstone_ref!r} not found')
        scopes = ['global']
        for (pid,) in memberships:
            scopes.append(pid)
            scopes.append(_private_tombstone_scope(pid, agent_id))
    marks = ','.join('?' for _ in scopes)
    candidates = conn.execute(
        'SELECT id, scope FROM tombstone '
        'WHERE claim_fingerprint LIKE ? ESCAPE \'\\\' '
        f'AND scope IN ({marks}) AND overridden_by IS NULL '
        'ORDER BY id',
        (ref.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', *scopes),
    ).fetchall()
    if not candidates:
        raise NotFound(f'tombstone {tombstone_ref!r} not found')
    if len(candidates) > 1:
        raise AmbiguousTombstonePrefix(ref, [(c[0], c[1]) for c in candidates])
    resolved = candidates[0][0]
    return {'tombstone_id': resolved,
            'overridden': override_tombstone(conn, resolved, agent_id)}


def override_tombstone(conn, tombstone_id, agent_id):
    """Explicitly override one active refusal guard without resurrecting history.

    Project guards require a project owner. Private guards may be overridden by
    the private owner or a project owner. Global guards fail closed in v1
    because there is no global-admin identity model. Returns False if already
    overridden, True when this call performs the override.
    """
    with store.write_tx(conn):
        row = conn.execute(
            'SELECT claim_fingerprint, scope, reason, overridden_by '
            'FROM tombstone WHERE id=?', (tombstone_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f'tombstone {tombstone_id} not found')
        claim_fp, refusal_scope, reason, overridden_by = row
        if overridden_by is not None:
            return False
        if refusal_scope == 'global':
            raise PermissionDenied('global tombstone override requires an admin identity')
        if refusal_scope.startswith('private:'):
            parts = refusal_scope.split(':', 2)
            if len(parts) != 3:
                raise MemCoreError(f'invalid private tombstone scope: {refusal_scope}')
            project_id, owner_agent_id = parts[1], parts[2]
            role = _require_membership(conn, project_id, agent_id)
            if agent_id != owner_agent_id and role != 'owner':
                raise PermissionDenied(
                    'only the private owner or a project owner may override this tombstone'
                )
        else:
            project_id = refusal_scope
            role = _require_membership(conn, project_id, agent_id)
            if role != 'owner':
                raise PermissionDenied('only a project owner may override a project tombstone')
        cur = conn.execute(
            'UPDATE tombstone SET overridden_by=? WHERE id=? AND overridden_by IS NULL',
            (agent_id, tombstone_id)
        )
        if cur.rowcount != 1:
            raise MemCoreError('tombstone state changed during override')
        _audit(conn, 'tombstone_override', agent_id, None, project_id, {
            'tombstone_id': tombstone_id, 'fingerprint': claim_fp,
            'scope': refusal_scope, 'reason': reason})
        return True


def scan_contradictions(conn, project_id, limit_pairs: int = 200) -> list:
    """Find live claim pairs that disagree. Content-free report.

    Groups live memories by subject key (from contradiction.subject_key),
    then tests pairs within each group with is_contradiction_pair. Returns
    [(id_a, id_b, reason)] capped at limit_pairs. Never mutates: marking a
    pair as conflict is a separate governed step (mark_contradiction).

    Scales as O(groups x pairs-in-group); subject keys keep groups small.
    On the live fleet (87 fingerprints) this is trivial.
    """
    from memcore import contradiction as _cd
    rows = conn.execute(
        'SELECT m.id, v.content FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "AND m.lifecycle IN ('candidate','accepted') ",
        (project_id,),
    ).fetchall()
    groups = {}
    for mem_id, content in rows:
        key = _cd.subject_key(content)
        if key:
            groups.setdefault(key, []).append((mem_id, content))
    pairs = []
    for _key, members in groups.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                (a_id, a_text), (b_id, b_text) = members[i], members[j]
                hit, reason = _cd.is_contradiction_pair(a_text, b_text)
                if hit:
                    pairs.append((a_id, b_id, reason))
                    if len(pairs) >= limit_pairs:
                        return pairs
    return pairs


def mark_contradiction(conn, memory_id_a, memory_id_b, agent_id,
                       reason, _manage_transaction=True):
    """Mark two disagreeing memories as conflict. Audited. Reversible.

    Both rows must be live (candidate/accepted/conflict) in the same project
    and owned-writable by agent_id. Does NOT resolve: resolution is supersede
    or reject by a human or governed tool. Returns True.
    """
    reason = scrub_secrets(reason) if isinstance(reason, str) else reason
    with store.write_tx(conn, manage=_manage_transaction):
        if memory_id_a == memory_id_b:
            raise MemCoreError(
                'contradiction pair requires two distinct memories')
        projects = set()
        for mem_id in (memory_id_a, memory_id_b):
            project_id, _scope, _owner, lifecycle, _role = (
                _require_memory_write_access(conn, mem_id, agent_id))
            if lifecycle in ('rejected', 'disabled', 'superseded'):
                raise MemCoreError(
                    f'cannot mark terminal memory as conflict: {mem_id}')
            projects.add(project_id)
        if len(projects) != 1:
            raise MemCoreError(
                'contradiction pair must belong to the same project')
        conn.execute(
            "UPDATE memory SET lifecycle='conflict', updated_at=? WHERE id IN (?, ?)",
            (_now(), memory_id_a, memory_id_b),
        )
        for mem_id in (memory_id_a, memory_id_b):
            _audit(conn, 'mark_conflict', agent_id, mem_id, project_id,
                   {'reason': reason, 'pair': [memory_id_a, memory_id_b]})
    return True


def pre_accept_conflict_check(conn, project_id, content, exclude_memory_id=None):
    """Read-only contradiction pre-check for auto-accept gates.

    Returns list of (live_memory_id, reason) where reason in {'polarity', 'numeric'}.
    Empty list = clean. Empty subject key -> fails closed with ('__empty_subject__', 'empty_subject_hold').
    Only checks live rows (candidate/accepted/conflict) in the project, excluding exclude_memory_id.
    """
    from memcore import contradiction as _cd

    key = _cd.subject_key(content)
    if not key:
        # No extractable subject (emoji-only, punctuation-only, bare digits,
        # stopwords) means there is nothing to contradict anything about. The
        # synthetic hold this replaced turned every such write into a permanent
        # conflict, changing behaviour for content the caller never asked to gate
        # -- measured: emoji, "12345" and "!!!" were all refused. Fail open: an
        # un-gated write is recoverable, a silently held one is not visible.
        return []

    # SQL prefilter on the first subject token before the Python pair test.
    # Every live row must be Python-tested for equivalence, but only rows that
    # literally contain that token can share a subject key (the key is built
    # from the content's own words), so this is a strict narrowing.
    #
    # Case folding is REQUIRED for that to hold: subject_key() lowercases and
    # NFC-normalises, so a stored row that capitalises the token ('Gateway
    # daemon port ...') shares the subject key but was invisible to a
    # case-sensitive instr() — the Python pair test then never ran and the
    # contradicting claim was admitted as 'accepted'.
    token = key.split(' ', 1)[0]
    rows = conn.execute(
        'SELECT m.id, v.content FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "AND m.lifecycle IN ('candidate','accepted','conflict') "
        'AND (m.id != ? OR ? IS NULL) '
        'AND instr(lower(v.content), lower(?)) > 0',
        (project_id, exclude_memory_id, exclude_memory_id, token),
    ).fetchall()

    hits = []
    for mem_id, mem_content in rows:
        hit, reason = _cd.is_contradiction_pair(content, mem_content)
        if hit:
            hits.append((mem_id, reason))
    return hits


# ── autonomy: corroboration → accept → Golden Rule (ADR-0018/0019) ──

#: Distinct corroborating agents required to auto-accept a claim.
CORROBORATE_ACCEPT_N = 3
#: Distinct corroborating agents required to crown a claim a Golden Rule.
#: ponytail: unreachable on a 4-agent fleet — the owner (pchoke) is a human and
#: only wrote 2 memories, so no claim can ever reach 5 distinct writers.
#: Measured 2026-10-07: dropping this to 4 changes recall output by ZERO bytes
#: (build_recall_block's PINNED_MAX_SHARE already caps how much of the block
#: pinned rows may use). Leave it at 5 unless the fleet grows; if a sixth
#: independent agent joins, 5 becomes meaningful again.
GOLDEN_N = 5
#: Semantic confidence at or above which a `remember` verdict self-accepts.
HIGH_CONFIDENCE_ACCEPT = 0.95


def corroboration_members(conn, project_id, claim_fp):
    """Memories carrying one fingerprint: (id, scope, owner, lifecycle, created_at).

    Only live, non-terminal rows count toward corroboration. The caller
    decides whether the count is sufficient; tombstone-blocked fingerprints
    must be filtered by the caller via ``admission_allowed`` semantics.
    """
    return conn.execute(
        'SELECT m.id, m.scope, m.owner_agent_id, m.lifecycle, m.created_at '
        'FROM memory m '
        'WHERE m.project_id=? AND m.claim_fingerprint=? '
        "  AND m.lifecycle IN ('candidate','accepted','conflict') "
        'ORDER BY (m.scope = \'project\') DESC, datetime(m.created_at) ASC, m.id ASC',
        (project_id, claim_fp),
    ).fetchall()


def accept_memory(conn, memory_id, agent_id, reason, _manage_transaction=True):
    """Promote a memory candidate/conflict → accepted. Audited. Reversible.

    Used by the autonomy paths (corroboration, explicit durable signals,
    high-confidence semantic verdicts). Human-equivalent trust: verification
    is left untouched here — callers set it explicitly. Terminal lifecycles
    refuse; the tombstone veto is checked BEFORE the already-accepted early
    return, so re-accepting a row whose fingerprint was tombstoned elsewhere
    still refuses loudly instead of silently succeeding.
    """
    with store.write_tx(conn, manage=_manage_transaction):
        project_id, scope, owner, lifecycle, _role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        if lifecycle in ('rejected', 'disabled', 'superseded'):
            raise MemCoreError(
                f'cannot accept terminal memory (lifecycle={lifecycle})'
            )
        _cur_ver, claim_fp = _current_claim_identity(conn, memory_id)
        blocked = _tombstone_active(
            conn, claim_fp, project_id, scope=scope, agent_id=owner
        )
        if blocked:
            raise TombstoneBlocked(claim_fp, blocked[0])
        if lifecycle == 'accepted':
            return False
        conn.execute(
            "UPDATE memory SET lifecycle='accepted', updated_at=? WHERE id=?",
            (_now(), memory_id),
        )
        _audit(conn, 'auto_accept', agent_id, memory_id, project_id,
               {'reason': reason, 'previous_lifecycle': lifecycle})
    return True


def auto_accept(conn, memory_id, agent_id, reason):
    """One deep promotion step: conflict gate -> hold -> accept -> sweep.

    Owns the whole auto-accept sequence every promotion lane used to
    re-assemble by hand: fetch the live row, run pre_accept_conflict_check,
    audit-only hold on hits (never resolve, never touch the opposing row),
    accept_memory on clean, then the maybe_auto_corrob sweep. Joins the
    caller's already-open transaction: no BEGIN/COMMIT here, so a missing
    transaction is a caller bug, not this function's to mask.
    Returns {'held': bool, 'hits': [...], 'accepted': bool,
    'transitioned': bool, 'sweep': {...}}: 'transitioned' is False when the
    row was already accepted (accept_memory no-op); 'sweep' is present only
    on the clean path.
    """
    row = conn.execute(
        'SELECT m.project_id, v.content, m.claim_fingerprint '
        'FROM memory m JOIN memory_version v '
        'ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.id=?',
        (memory_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f'memory {memory_id} not found')
    project_id, content, claim_fp = row
    if not claim_fp:
        # Legacy rows predate the stored fingerprint; the lanes always swept
        # on the content-derived print, so fall back to it (accept_memory
        # repairs the stored NULL when it runs).
        claim_fp = fingerprint(content)
    hits = pre_accept_conflict_check(
        conn, project_id, content, exclude_memory_id=memory_id)
    if hits:
        _audit(conn, 'contradiction-hold', agent_id, memory_id, project_id,
               {'hits': [list(h) for h in hits]})
        return {'held': True, 'hits': hits, 'accepted': False,
                'transitioned': False}
    transitioned = accept_memory(
        conn, memory_id, agent_id, reason, _manage_transaction=False)
    sweep = maybe_auto_corrob(conn, project_id, claim_fp, agent_id)
    return {'held': False, 'hits': [], 'accepted': True,
            'transitioned': transitioned, 'sweep': sweep}


def set_golden(
conn, memory_id, agent_id, golden=True, _manage_transaction=True):
    """Pin/unpin a memory as Golden Rule (pinned+critical). Audited."""
    with store.write_tx(conn, manage=_manage_transaction):
        project_id, _scope, _owner, _lifecycle, _role = _require_memory_write_access(
            conn, memory_id, agent_id
        )
        conn.execute(
            'UPDATE memory SET pinned=?, critical=?, updated_at=? WHERE id=?',
            (1 if golden else 0, 1 if golden else 0, _now(), memory_id),
        )
        _audit(conn, 'auto_golden_promote' if golden else 'auto_golden_demote',
               agent_id, memory_id, project_id, {'golden': golden})
    return True


def _corrob_conflict_hits(conn, project_id, member_ids):
    """Task-2 gate: a live contradiction against any member holds promotion."""
    for mid in member_ids:
        row = conn.execute(
            'SELECT v.content FROM memory m '
            'JOIN memory_version v ON v.id = m.current_version_id '
            'AND v.memory_id = m.id WHERE m.id=?',
            (mid,),
        ).fetchone()
        if not row:
            continue
        hits = pre_accept_conflict_check(
            conn, project_id, row[0], exclude_memory_id=mid)
        if hits:
            return hits
    return []


def _corrob_veto(conn, project_id, claim_fp, members):
    """Tombstone veto: the project guard plus every member's private guard.

    A private rejection must also stop a later project-wide coronation of
    the same claim, so each member lane is checked before promotion.
    """
    veto = _tombstone_active(conn, claim_fp, project_id)
    if veto:
        return veto[0]
    for _mid, _scope, owner, _lc, _ts in members:
        priv = _tombstone_active(
            conn, claim_fp, project_id, scope='private', agent_id=owner)
        if priv:
            return priv[0]
    return None


def _promote_canonical(conn, canonical_id, actor_agent_id, project_id,
                       claim_fp, members, agents):
    """Accept (and maybe crown) the canonical copy; returns the action name."""
    row = conn.execute(
        'SELECT scope, lifecycle, verification FROM memory WHERE id=?',
        (canonical_id,),
    ).fetchone()
    scope, lifecycle, verification = row
    if scope != 'project':
        conn.execute(
            "UPDATE memory SET scope='project', updated_at=? WHERE id=?",
            (_now(), canonical_id),
        )
    if lifecycle != 'accepted':
        conn.execute(
            "UPDATE memory SET lifecycle='accepted', updated_at=? WHERE id=?",
            (_now(), canonical_id),
        )
    if verification not in ('source_backed', 'runtime_verified',
                             'user_authoritative'):
        conn.execute(
            "UPDATE memory SET verification='source_backed', updated_at=? "
            'WHERE id=?',
            (_now(), canonical_id),
        )
    _audit(conn, 'auto_corrob_accept', actor_agent_id, canonical_id,
           project_id, {'fingerprint': claim_fp, 'sources': agents,
                        'member_ids': [m[0] for m in members]})
    if len(agents) < GOLDEN_N:
        return 'accepted'
    conn.execute(
        'UPDATE memory SET pinned=1, critical=1, updated_at=? WHERE id=?',
        (_now(), canonical_id),
    )
    _audit(conn, 'auto_golden_promote', actor_agent_id, canonical_id,
           project_id, {'fingerprint': claim_fp, 'sources': agents})
    return 'golden'


def maybe_auto_corrob(conn, project_id, claim_fp, actor_agent_id):
    """Corroboration sweep for one fingerprint. Returns a result dict.

    Counts DISTINCT owner agents holding the claim. Tombstone-blocked →
    no-op (veto wins). >=3 → canonical copy accepted project-wide;
    >=5 → canonical additionally crowned Golden (pinned+critical).
    Runs in the caller's transaction when one is open, else its own.
    """
    members = corroboration_members(conn, project_id, claim_fp)
    agents = sorted({m[2] for m in members})
    if not members:
        return {'fingerprint': claim_fp, 'sources': 0, 'action': 'none'}
    gate_hits = _corrob_conflict_hits(
        conn, project_id, [m[0] for m in members])
    if gate_hits:
        _audit(conn, 'contradiction-hold', actor_agent_id, members[0][0],
               project_id, {'fingerprint': claim_fp,
                            'hits': [list(h) for h in gate_hits]})
        if not conn.in_transaction:
            conn.commit()
        return {'fingerprint': claim_fp, 'sources': len(agents),
                'action': 'contradiction_hold', 'hits': gate_hits}
    veto_by = _corrob_veto(conn, project_id, claim_fp, members)
    if veto_by:
        return {'fingerprint': claim_fp, 'sources': len(agents),
                'action': 'vetoed', 'tombstone_id': veto_by}
    if len(agents) < CORROBORATE_ACCEPT_N:
        return {'fingerprint': claim_fp, 'sources': len(agents),
                'action': 'none'}
    result = {'fingerprint': claim_fp, 'sources': len(agents),
              'canonical': members[0][0]}
    with store.write_tx(conn, manage=not conn.in_transaction):
        result['action'] = _promote_canonical(
            conn, members[0][0], actor_agent_id, project_id, claim_fp,
            members, agents)
    return result


def _scan_freshness_decay(conn, now, aging_days, stale_days):
    """Shared decay scan used by both dry-run and apply (C1 parity).

    Evaluates the exact same conditions — including the last_recalled
    reinforcement window — so the two callers can never drift apart.
    """
    cols = {r[1] for r in conn.execute('PRAGMA table_info(memory)')}
    reinforced = (
        "AND (last_recalled IS NULL OR datetime(last_recalled) < "
        "datetime(?, '-' || ? || ' days')) "
        if 'last_recalled' in cols else ''
    )
    params_aging = (now, aging_days) + (
        (now, REINFORCEMENT_WINDOW_DAYS) if 'last_recalled' in cols else ())
    aged = [r[0] for r in conn.execute(
        "SELECT id FROM memory WHERE freshness='current' "
        "AND datetime(updated_at) < datetime(?, '-' || ? || ' days') "
        + reinforced,
        params_aging,
    ).fetchall()]
    params_stale = (now, stale_days) + (
        (now, REINFORCEMENT_WINDOW_DAYS) if 'last_recalled' in cols else ())
    staled = [r[0] for r in conn.execute(
        "SELECT id FROM memory WHERE freshness='aging' "
        "AND datetime(updated_at) < datetime(?, '-' || ? || ' days') "
        + reinforced,
        params_stale,
    ).fetchall()]
    return aged, staled


def apply_freshness_decay(conn, aging_days=30, stale_days=90, now=None, *,
                          dry_run=False):
    """Age-based freshness decay: current → aging → stale by updated_at.

    Decay never changes lifecycle, never tombstones, and never hides rows —
    it only lowers recall rank via the existing CASE ordering. Returns
    (aged_ids, staled_ids). Fully reversible: any write refreshes updated_at.

    now: optional override for deterministic tests — a datetime, an ISO-8601
    Z string (simulated-time protocol), or None for the simulated clock
    (FAKE_NOW else real time). CLI behaviour is unchanged when unset.

    dry_run=True (C1): evaluates the exact same scan with no write
    transaction and no mutations, so the dry-run and --apply lists are
    identical by construction (they used to drift on reinforced rows).
    """
    if aging_days < 0:
        raise MemCoreError('aging_days must be >= 0')
    if stale_days < aging_days:
        raise MemCoreError('stale_days must be >= aging_days')
    now = _coerce_now(now)
    if dry_run:
        return _scan_freshness_decay(conn, now, aging_days, stale_days)
    with store.write_tx(conn):
        aged, staled = _scan_freshness_decay(conn, now, aging_days, stale_days)
        for mem_id in aged:
            conn.execute(
                "UPDATE memory SET freshness='aging', updated_at=? WHERE id=?",
                (now, mem_id),
            )
            _audit(conn, 'auto_decay_aging', None, mem_id, None,
                   {'aging_days': aging_days})
        for mem_id in staled:
            conn.execute(
                "UPDATE memory SET freshness='stale', updated_at=? WHERE id=?",
                (now, mem_id),
            )
            _audit(conn, 'auto_decay_stale', None, mem_id, None,
                   {'stale_days': stale_days})
    return aged, staled


def _changed_source_rows(conn, ids):
    """Rowed tracked-source files whose content changed; best-effort per row."""
    marks = ','.join('?' for _ in ids)
    rows = conn.execute(
        'SELECT m.id, m.current_version_id, v.source_hash, v.source_file '
        'FROM memory m JOIN memory_version v '
        'ON v.id = m.current_version_id AND v.memory_id = m.id '
        f"WHERE m.id IN ({marks}) AND m.freshness != 'stale' "
        'AND v.source_hash IS NOT NULL AND v.source_file IS NOT NULL',
        ids,
    ).fetchall()
    changed = []
    for mem_id, version_id, saved_hash, source_file in rows:
        try:
            path = pathlib.Path(source_file).expanduser()
            if not path.is_absolute():
                continue
            if not path.is_file():
                continue
            current_hash = _sha256_file(path)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if str(saved_hash).lower() != current_hash:
            changed.append((mem_id, version_id, source_file,
                            str(saved_hash), current_hash))
    return changed


def check_source_staleness(conn, memory_ids, get_head=None, repo_root=None):
    """Mark recalled memories stale when their tracked file content changed.

    Git commit is provenance context only; unrelated commits do not change this
    fact's source. Rows without a saved file hash or with an unavailable source
    are skipped. Freshness changes without touching updated_at. Best-effort:
    returns flipped ids and never raises. ``get_head`` and ``repo_root`` are
    retained as ignored compatibility arguments; file content is authoritative.
    """
    ids = [m for m in dict.fromkeys(memory_ids or []) if m]
    if not ids:
        return []
    try:
        changed = _changed_source_rows(conn, ids)
        if not changed:
            return []

        flipped = []
        with store.write_tx(conn):
            for mem_id, version_id, source_file, saved_hash, current_hash in changed:
                cur = conn.execute(
                    "UPDATE memory SET freshness='stale' "
                    "WHERE id=? AND current_version_id=? AND freshness != 'stale'",
                    (mem_id, version_id),
                )
                if cur.rowcount:
                    _audit(conn, 'auto_stale_source_changed', None, mem_id,
                           None, {'source_file': source_file,
                                  'source_hash': saved_hash,
                                  'current_source_hash': current_hash})
                    flipped.append(mem_id)
        return flipped
    except Exception:
        return []

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


def _eval_now_iso() -> str:
    """Simulated now as an ISO-8601 Z string: FAKE_NOW else real clock.

    Junk FAKE_NOW falls back to the real clock inside ablation.eval_now(),
    so this never raises.
    """
    try:
        now = _ablation.eval_now()
        if isinstance(now, datetime):
            dt = now
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        pass
    return _now()


def _coerce_now(now) -> str:
    """Normalize an apply_freshness_decay now override to an ISO Z string."""
    if now is None:
        return _eval_now_iso()
    if isinstance(now, datetime):
        dt = now
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    text = str(now).strip()
    if text:
        return text
    return _eval_now_iso()


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
_TABLE_COLS_CACHE_MAX = 128

#: (id(conn), table) -> ((db path, schema_version), columns). Every hit is
#: validated against the connection's live (path, schema_version) marker:
#: SQLite bumps schema_version on ANY schema change, and the path separates
#: distinct databases, so a recycled id(conn) or a migrated schema can never
#: serve stale columns.
_CONN_TABLE_COLS: dict[tuple[int, str], tuple[tuple[str, int], set[str]]] = {}


def _clear_table_cols_cache() -> None:
    """Drop the schema cache (called after migrations change the schema)."""
    _CONN_TABLE_COLS.clear()


def _conn_schema_marker(conn) -> tuple[str, int]:
    """Cheap identity marker: (main db file path, schema_version)."""
    path = ''
    try:
        row = conn.execute('PRAGMA database_list').fetchone()
        if row is not None and len(row) > 2 and row[2]:
            path = str(row[2])
    except Exception:
        return ('', -1)
    try:
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
    except Exception:
        version = -1
    return (path, int(version))


def _table_cols(conn, table: str = 'memory') -> set[str]:
    key = (id(conn), table)
    marker = _conn_schema_marker(conn)
    entry = _CONN_TABLE_COLS.get(key)
    if entry is not None and entry[0] == marker:
        return entry[1]
    try:
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
    except Exception:
        cols = set()
    # H4: never cache an empty/missing result — it may only mean the schema
    # has not been applied yet — and keep the cache bounded.
    if cols:
        if len(_CONN_TABLE_COLS) >= _TABLE_COLS_CACHE_MAX:
            _CONN_TABLE_COLS.clear()
        _CONN_TABLE_COLS[key] = (marker, cols)
    else:
        _CONN_TABLE_COLS.pop(key, None)
    return cols


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
REINFORCEMENT_WINDOW_DAYS = 14


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
                     detail_filter, detail_params):
    """Vector lane wrapper: never raises — degraded search beats no search."""
    if not query_vec:
        return []
    try:
        return _vector_search_lane(
            conn, project_id, agent_id, query_vec,
            limit=limit * DISTINCT_OVERFETCH,
            detail_filter=detail_filter, detail_params=detail_params
        )
    except Exception:
        return []


def search(conn, project_id, agent_id, query, limit=20,
           scope_detail=None, query_vec=None):
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
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        raise MemCoreError('search limit must be an integer')
    if limit < 1:
        raise MemCoreError('search limit must be >= 1')
    limit = min(limit, 500)
    user_query = str(query or '').strip()
    if not user_query:
        return []
    # Retention ranking: MEMCORE_ABLATE_DECAY=1 neutralizes exactly the
    # retention term (legacy CASE+bm25 ordering). MEMCORE_FAKE_NOW feeds the
    # simulated clock (fake else real) for age computation only; it never
    # reorders except through that age term. Junk FAKE_NOW falls back to the
    # real clock and never raises.
    now_iso = _eval_now_iso()
    retention_expr, retention_params = _retention_order(
        conn, not _decay_ablated(), alias='m')
    retention_tail = (
        (', ' + retention_expr + ' DESC') if retention_expr else '')
    # Lane 3.2: expand fleet aliases before any matching (FTS lane).
    # The exact-fallback below keeps using user_query (pre-expansion) so
    # appended alias values can never break the verbatim substring check.
    raw_query = _expand_aliases(user_query)
    if _membership_role(conn, project_id, agent_id) is None:
        return []
    detail_filter, detail_params = _search_detail_filter(conn, scope_detail)
    if detail_filter is None:
        return []
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
    # incompatibility check never re-tokenizes the query per row (C2). The
    # ordered token list feeds the phrase-negation signal; the set feeds
    # anchor/choice checks.
    from memcore import contradiction as _qd
    query_tokens = _qd._tokens(user_query)
    query_tok = set(query_tokens)
    query_numbers = _qd.numbers(user_query)

    if not vec_rows:
        return _merge_exact_fallback(
            fts_rows, exact_rows, limit, user_query,
            query_tok=query_tok, query_numbers=query_numbers,
            query_tokens=query_tokens)
    fused = _fuse_rrf_candidates(fts_rows, vec_rows)
    return _merge_exact_fallback(
        fused, exact_rows, limit, user_query,
        query_tok=query_tok, query_numbers=query_numbers,
        query_tokens=query_tokens)


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
           scope_detail=None, query_vec=None, get_head=None, repo_root=None):
    """The unified, deep memory recall lifecycle.

    Encapsulates candidate search, access reinforcement, source staleness
    verification, and final freshness resolution into a single interface.
    Returns list of candidate tuples with finalized freshness state. The legacy
    ``get_head`` and ``repo_root`` keywords are accepted for call compatibility;
    source-file hashes now determine freshness.
    """
    rows = search(
        conn, project_id, agent_id, query, limit=limit,
        scope_detail=scope_detail, query_vec=query_vec
    )
    return finalize_recall(conn, rows)


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


# ————————————————————————————————————————————————————————————————————————————————————————

def _cutoff(conn, days):
    """Cutoff timestamp in sqlite ISO 8601 UTC with Z suffix (migration 0005 contract)."""
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)", (f'-{days} days',)
    ).fetchone()[0]


def gc_scan(conn, candidate_days=30, tombstone_days=90):
    """List retention candidates WITHOUT touching anything.

    a) inactive-looking candidate memories (old updated_at, no evidence,
       not pinned/critical) eligible for reversible disable
    b) explicitly overridden tombstones older than tombstone_days
    Age alone is never grounds for truth rejection or a refusal fingerprint.
    Active tombstones are durable rejection guards and are never age-purged.
    Returns (candidates, tombstones); each row starts with the id.
    """
    if candidate_days < 0 or tombstone_days < 0:
        raise MemCoreError('GC retention days must be >= 0')
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    candidates = conn.execute(
        'SELECT m.id, m.project_id, m.owner_agent_id, v.content, m.updated_at '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        "WHERE m.lifecycle = 'candidate' "
        '  AND m.pinned = 0 AND m.critical = 0 '
        '  AND datetime(m.updated_at) < datetime(?) '
        '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
        '                  WHERE el.memory_version_id = m.current_version_id) '
        'ORDER BY datetime(m.updated_at), m.id',
        (cutoff_c,)
    ).fetchall()
    tombstones = conn.execute(
        'SELECT t.id, t.claim_fingerprint, t.scope, t.reason, t.created_at, '
        '       t.overridden_by '
        'FROM tombstone t WHERE t.overridden_by IS NOT NULL '
        'AND datetime(t.created_at) < datetime(?) ORDER BY t.created_at',
        (cutoff_t,)
    ).fetchall()
    return candidates, tombstones


def gc_apply(conn, candidate_days=30, tombstone_days=90):
    """Run retention cleanup without turning age into a truth judgment.

    Old unevidenced, unpinned, non-critical candidates are disabled so a human
    can restore them later. Only explicitly overridden tombstones are purged.
    Returns (disabled_ids, purged_tombstone_ids).
    """
    candidates, tombstones = gc_scan(conn, candidate_days, tombstone_days)
    cutoff_c = _cutoff(conn, candidate_days)
    cutoff_t = _cutoff(conn, tombstone_days)
    disabled, purged = [], []
    for stale_row in candidates:
        mem_id = stale_row[0]
        with store.write_tx(conn):
            # Re-evaluate every destructive predicate under the write lock.
            # A memory may gain evidence, be corrected, or age across the
            # scan/apply gap; GC must act on current state/content only.
            row = conn.execute(
                'SELECT m.project_id, v.content '
                'FROM memory m '
                'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
                'WHERE m.id=? AND m.lifecycle=\'candidate\' '
                '  AND m.pinned=0 AND m.critical=0 '
                '  AND datetime(m.updated_at) < datetime(?) '
                '  AND NOT EXISTS (SELECT 1 FROM evidence_link el '
                '                  WHERE el.memory_version_id=m.current_version_id)',
                (mem_id, cutoff_c)
            ).fetchone()
            if not row:
                continue
            project_id, content = row
            conn.execute(
                "UPDATE memory SET lifecycle='disabled', updated_at=? WHERE id=?",
                (_now(), mem_id)
            )
            _audit(conn, 'gc_disable', None, mem_id, project_id,
                   {'reason': 'retention', 'content': content})
            disabled.append(mem_id)
    for stale_row in tombstones:
        tomb_id = stale_row[0]
        with store.write_tx(conn):
            row = conn.execute(
                'SELECT claim_fingerprint, scope, reason FROM tombstone '
                'WHERE id=? AND overridden_by IS NOT NULL '
                'AND datetime(created_at) < datetime(?)',
                (tomb_id, cutoff_t)
            ).fetchone()
            if not row:
                continue
            claim_fp, scope, reason = row
            conn.execute('DELETE FROM tombstone WHERE id=?', (tomb_id,))
            _audit(conn, 'gc_purge_tombstone', None, None, None,
                   {'tombstone_id': tomb_id, 'claim_fingerprint': claim_fp,
                    'scope': scope, 'reason': reason})
            purged.append(tomb_id)
    return disabled, purged


def _tombstone_scope_reasons(claim_fp, scope, project_ids, agent_ids):
    """Reasons one tombstone ref/scope pair is malformed; [] when valid."""
    reasons = []
    if len(claim_fp) != 16 or any(ch not in '0123456789abcdef' for ch in claim_fp):
        reasons.append('invalid_fingerprint')
    if scope == 'global' or scope in project_ids:
        return reasons
    if scope.startswith('private:'):
        parts = scope.split(':', 2)
        if len(parts) != 3 or parts[1] not in project_ids or parts[2] not in agent_ids:
            reasons.append('invalid_private_scope')
    else:
        reasons.append('invalid_scope')
    return reasons


def _tombstone_scope_violations(conn):
    """Validate tombstone.scope as an encoded reference (not an FK).

    Both the reference shape and the fingerprint are checked so a typo can
    never silently disable a refusal guard.
    """
    project_ids = {row[0] for row in conn.execute('SELECT id FROM project')}
    agent_ids = {row[0] for row in conn.execute('SELECT id FROM agent')}
    violations = []
    for tomb_id, claim_fp, scope in conn.execute(
        'SELECT id, claim_fingerprint, scope FROM tombstone ORDER BY id'
    ):
        reasons = _tombstone_scope_reasons(claim_fp, scope, project_ids, agent_ids)
        if reasons:
            violations.append((tomb_id, scope, reasons))
    return violations


def inspect_governance_health(conn) -> dict:
    """Governance-layer health report for one open store (doctor's deep seam).

    Claim fingerprints, refusal guards, memberships and identity collisions —
    everything that depends on domain rules rather than SQLite structure.
    Storage/file concerns live in ``store.inspect_storage_health``. Read-only;
    never mutates; the CLI adapter decides which findings gate the exit.
    """
    report = {}
    report['memory_fingerprint_violations'] = []
    for memory_id, claim_fp, content in conn.execute(
        'SELECT m.id, m.claim_fingerprint, v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
        'ORDER BY m.id'
    ):
        expected_fp = fingerprint(content)
        if claim_fp != expected_fp:
            report['memory_fingerprint_violations'].append(
                (memory_id, claim_fp, expected_fp)
            )
    report['tombstones'] = {
        'active': conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NULL'
        ).fetchone()[0],
        'overridden': conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE overridden_by IS NOT NULL'
        ).fetchone()[0],
    }
    report['unguarded_rejected_memories'] = []
    rejected_rows = conn.execute(
        'SELECT m.id, m.project_id, m.scope, m.owner_agent_id, v.content '
        'FROM memory m JOIN memory_version v ON v.id=m.current_version_id '
        "WHERE m.lifecycle='rejected' ORDER BY m.id"
    ).fetchall()
    for memory_id, project_id, scope, owner_agent_id, content in rejected_rows:
        if _tombstone_active(
            conn, fingerprint(content), project_id,
            scope=scope, agent_id=owner_agent_id
        ) is None:
            report['unguarded_rejected_memories'].append(memory_id)
    report['tombstone_violations'] = _tombstone_scope_violations(conn)
    report['memberships'] = conn.execute(
        'SELECT p.name, a.name, pm.role FROM project_membership pm '
        'JOIN project p ON p.id = pm.project_id '
        'JOIN agent a ON a.id = pm.agent_id ORDER BY p.name, a.name'
    ).fetchall()
    report['agent_name_collisions'] = conn.execute(
        'SELECT p.name, a.name, COUNT(*) FROM project_membership pm '
        'JOIN project p ON p.id=pm.project_id '
        'JOIN agent a ON a.id=pm.agent_id '
        'GROUP BY pm.project_id, a.name HAVING COUNT(*) > 1 '
        'ORDER BY p.name, a.name'
    ).fetchall()
    report['project_name_collisions'] = conn.execute(
        'SELECT name, COUNT(*), GROUP_CONCAT(id, ",") FROM project '
        'GROUP BY name HAVING COUNT(*) > 1 ORDER BY name'
    ).fetchall()
    report['corroboration'] = store.corroboration_funnel(
        conn, accept_n=CORROBORATE_ACCEPT_N, golden_n=GOLDEN_N)
    return report


def stats(conn):
    """Operational stats: lifecycle/scope counts, top authors, avg summary
    length, FTS drift check. Dict out, no printing (CLI renders)."""
    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    by_lifecycle = dict(conn.execute(
        'SELECT lifecycle, COUNT(*) FROM memory GROUP BY lifecycle').fetchall())
    by_scope = dict(conn.execute(
        'SELECT scope, COUNT(*) FROM memory GROUP BY scope').fetchall())
    top_agents = conn.execute(
        'SELECT a.name, COUNT(*) AS n FROM memory m '
        'JOIN agent a ON a.id = m.owner_agent_id '
        'GROUP BY m.owner_agent_id ORDER BY n DESC LIMIT 5'
    ).fetchall()
    avg_len = conn.execute(
        'SELECT AVG(LENGTH(v.content)) FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id'
    ).fetchone()[0]
    fts_rows = one('SELECT COUNT(*) FROM memory_version_fts')
    ver_rows = one('SELECT COUNT(*) FROM memory_version')
    has_journal = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_event'"
    ).fetchone() is not None
    journal = dict(conn.execute(
        'SELECT status, COUNT(*) FROM ingest_event GROUP BY status'
    ).fetchall()) if has_journal else {}
    return {
        'memories_total': one('SELECT COUNT(*) FROM memory'),
        'by_lifecycle': by_lifecycle,
        'by_scope': by_scope,
        'top_agents': [{'agent': n, 'memories': c} for n, c in top_agents],
        'avg_summary_length': round(avg_len, 1) if avg_len is not None else 0.0,
        'fts': {'fts_rows': fts_rows, 'version_rows': ver_rows,
                'in_sync': fts_rows == ver_rows},
        'journal': journal,
    }


def _valid_evidence_entry(ev):
    """True when one evidence dict has a known kind and string-ish fields."""
    kind = ev.get('kind')
    if kind is not None and kind not in ('file', 'commit', 'test', 'observation',
                                         'user_input', 'external', 'source'):
        return False
    for field in ('source_uri', 'source_label'):
        value = ev.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True


def _import_item_summary(item):
    """Validate one import item without mutating the store."""
    if not isinstance(item, dict):
        return None, 'invalid_item'
    summary = item.get('summary')
    if not isinstance(summary, str) or not summary.strip():
        return None, 'empty_summary'
    evidence = item.get('evidence') or []
    if not isinstance(evidence, list) or any(
            not isinstance(ev, dict) for ev in evidence):
        return None, 'invalid_evidence'
    for ev in evidence:
        if not _valid_evidence_entry(ev):
            return None, 'invalid_evidence'
    return summary, None


def _claim_already_present(conn, project_id, claim_fp, scope='project', agent_id=None):
    """Check current non-rejected memories using the indexed claim fingerprint.

    Migration 0010 backfilled fingerprints and engine writes maintain them. A
    narrow legacy fallback scans only externally inserted rows whose fingerprint
    is still NULL, preserving compatibility without rescanning the whole scope.
    """
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    sql = (
        'SELECT 1 FROM memory m '
        'WHERE m.project_id=? AND m.scope=? AND m.claim_fingerprint=? '
        "AND m.lifecycle != 'rejected' "
    )
    args = [project_id, scope, claim_fp]
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('agent_id is required when planning private import')
        sql += 'AND m.owner_agent_id=? '
        args.append(agent_id)
    if conn.execute(sql + 'LIMIT 1', args).fetchone():
        return True

    legacy_sql = (
        'SELECT v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
        'WHERE m.project_id=? AND m.scope=? AND m.claim_fingerprint IS NULL '
        "AND m.lifecycle != 'rejected' "
    )
    legacy_args = [project_id, scope]
    if scope == 'private':
        legacy_sql += 'AND m.owner_agent_id=? '
        legacy_args.append(agent_id)
    return any(
        fingerprint(content) == claim_fp
        for (content,) in conn.execute(legacy_sql, legacy_args)
    )


def _import_idempotency_key(project_id, claim_fp, scope='project', agent_id=None):
    if scope == 'project':
        # Preserve the Phase-1 key shape for existing project imports.
        return f'import:{project_id}:{claim_fp}'
    if scope == 'private':
        if not agent_id:
            raise MemCoreError('agent_id is required for private import idempotency')
        return f'import:{project_id}:private:{agent_id}:{claim_fp}'
    raise MemCoreError(f'invalid scope: {scope}')


def _classify_import_item(conn, item, project_id, scope, agent_id, seen):
    """Classify one import item read-only: (fingerprint, reason_or_None)."""
    summary, reason = _import_item_summary(item)
    fp = fingerprint(summary) if summary is not None else None
    if reason is None and fp in seen:
        reason = 'duplicate_input'
    if reason is None:
        ikey = _import_idempotency_key(
            project_id, fp, scope=scope, agent_id=agent_id
        )
        if conn.execute('SELECT 1 FROM idempotency_key WHERE key=?', (ikey,)).fetchone():
            reason = 'already_imported'
        elif _tombstone_active(conn, fp, project_id, scope=scope, agent_id=agent_id):
            reason = 'tombstone_blocked'
        elif _claim_already_present(conn, project_id, fp, scope=scope, agent_id=agent_id):
            reason = 'already_present'
    return fp, reason


def plan_import(conn, items, project_id, scope='project', agent_id=None):
    """Read-only import preview: classify every item and perform zero writes."""
    if not isinstance(items, list):
        raise MemCoreError('items must be a list')
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    if scope == 'private' and not agent_id:
        raise MemCoreError('agent_id is required when planning private import')
    plan = {'total': len(items), 'would_add': 0, 'skipped': 0,
            'reasons': {}, 'items': []}
    seen = set()
    for index, item in enumerate(items):
        fp, reason = _classify_import_item(
            conn, item, project_id, scope, agent_id, seen)
        if reason is None:
            seen.add(fp)
            plan['would_add'] += 1
            status = 'would_add'
        else:
            if reason == 'already_imported' and fp is not None:
                seen.add(fp)
            plan['skipped'] += 1
            plan['reasons'][reason] = plan['reasons'].get(reason, 0) + 1
            status = reason
        plan['items'].append({'index': index, 'status': status,
                              'fingerprint': fp})
    return plan


class _SkipImport(Exception):
    """Internal: one import item is ineligible; the batch continues."""

    def __init__(self, fp=None):
        super().__init__(fp or '')
        self.fingerprint = fp


def _insert_import_evidence(conn, version_id, ev):
    """Insert one evidence row plus its support link for an imported version."""
    raw_kind = ev.get('kind')
    kind = 'external' if raw_kind in (None, 'source') else raw_kind
    metadata = {'original_kind': raw_kind} if raw_kind == 'source' else {}
    ev_id = _new_id('ev')
    conn.execute(
        'INSERT INTO evidence (id, kind, source_uri, source_label, metadata, captured_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (ev_id, kind, ev.get('source_uri'), ev.get('source_label'),
         json.dumps(metadata), _now())
    )
    conn.execute(
        'INSERT INTO evidence_link (evidence_id, memory_version_id, relation, created_at) '
        "VALUES (?, ?, 'supports', ?)",
        (ev_id, version_id, _now())
    )


def _import_one_item(conn, item, summary, fp, project_id, agent_id, scope):
    """Create one imported memory + its evidence inside the caller's savepoint.

    Returns (memory_id, version_id); raises _SkipImport for ineligible items
    (already imported / tombstone-blocked / claim already present) and lets
    real write errors propagate for the caller's atomicity decision.
    """
    ikey = _import_idempotency_key(
        project_id, fp, scope=scope, agent_id=agent_id
    )
    already = conn.execute(
        'SELECT 1 FROM idempotency_key WHERE key = ?', (ikey,)
    ).fetchone()
    if already:
        raise _SkipImport(fp)
    if _tombstone_active(
        conn, fp, project_id, scope=scope, agent_id=agent_id
    ):
        raise _SkipImport(fp)
    if _claim_already_present(
        conn, project_id, fp, scope=scope, agent_id=agent_id
    ):
        raise _SkipImport(fp)
    raw_type = item.get('type')
    mem_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else 'fact'
    mem_id, ver_id = create_memory(
        conn, project_id, agent_id,
        summary, scope=scope,
        memory_type=mem_type,
        idempotency_key=ikey,
        _manage_transaction=False,
    )
    for ev in item.get('evidence') or []:
        _insert_import_evidence(conn, ver_id, ev)
    return mem_id, ver_id


def import_memories(conn, items, project_id, agent_id, scope='project',
                    batch_size=100):
    """Bulk import candidate memories with per-item atomicity.

    Each memory, audit/idempotency row, and all of its evidence links form an
    atomic item unit via savepoints, committed in bounded batches to eliminate
    per-row disk sync overhead. If evidence insertion fails, the whole item rolls
    back. Re-imports are idempotent by ``import:<project>:<fingerprint>`` and exact
    claims already present in the same visibility scope are not duplicated.
    """
    if not isinstance(items, list):
        raise MemCoreError('items must be a list')
    if scope not in ('project', 'private'):
        raise MemCoreError(f'invalid scope: {scope}')
    if scope == 'private' and not agent_id:
        raise MemCoreError('agent_id is required when importing private memories')
    added, skipped, created = 0, 0, []
    seen = set()
    in_batch = 0
    with store.write_tx(conn):
        for item in items:
            summary, invalid_reason = _import_item_summary(item)
            if invalid_reason is not None:
                skipped += 1
                continue
            fp = fingerprint(summary)
            if fp in seen:
                skipped += 1
                continue
            try:
                with store._savepoint(conn, 'item_sp'):
                    mem_id, ver_id = _import_one_item(
                        conn, item, summary, fp, project_id, agent_id, scope)
            except _SkipImport as skip:
                if skip.fingerprint:
                    seen.add(skip.fingerprint)
                skipped += 1
                continue
            except (TombstoneBlocked, PermissionDenied, MemCoreError):
                skipped += 1
                continue
            seen.add(fp)
            added += 1
            created.append((mem_id, ver_id))
            in_batch += 1
            if batch_size and in_batch >= batch_size:
                conn.execute('COMMIT')
                conn.execute('BEGIN IMMEDIATE')
                in_batch = 0
    return {'added': added, 'skipped': skipped, 'created': created}
