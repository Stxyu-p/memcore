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
import os
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
_PRIVATE_TAG_RE = re.compile(r'<private>[\s\S]*?(?:</private>|$)', re.IGNORECASE)
_PRIVATE_REDACTED = '«redacted:private»'


def scrub_private_tags(content: str) -> str:
    """Redact explicit <private>...</private> blocks before storage or indexing."""
    if not isinstance(content, str) or '<' not in content:
        return content
    return _PRIVATE_TAG_RE.sub(_PRIVATE_REDACTED, content)


def scrub_secrets(content: str) -> str:
    """Redact unmistakable credential values and <private> tags; names and prose survive."""
    if not isinstance(content, str) or not content:
        return content
    out = scrub_private_tags(content)
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


_AUTO_SOURCE_EXTS = (
    '.py', '.sql', '.toml', '.yaml', '.yml', '.json', '.md', '.sh', '.ps1',
    '.ts', '.tsx', '.js', '.jsx', '.rs', '.go', '.c', '.cpp', '.h', '.java',
    '.rb', '.cfg', '.ini',
)
_AUTO_SOURCE_TOKEN_RE = re.compile(
    r'(?:`([^`\r\n]+)`|([A-Za-z0-9_.\-/\\:]+\.(?:py|sql|toml|ya?ml|json|md|sh|ps1|tsx?|jsx?|rs|go|c|cpp|h|java|rb|cfg|ini)(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)?))',
    re.IGNORECASE,
)
_AUTO_SOURCE_LINE_SUFFIX_RE = re.compile(r'(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$')
_MAX_AUTO_SOURCE_BYTES = 5 * 1024 * 1024


def _auto_detect_source_file(content: str, base_dir=None) -> str | None:
    """Deterministically detect an existing regular source file referenced in content."""
    if not isinstance(content, str) or len(content) < 4:
        return None
    c_lower = content.lower()
    if not any(ext in c_lower for ext in _AUTO_SOURCE_EXTS):
        return None

    root_raw = base_dir or os.environ.get('MEMCORE_WORKSPACE') or os.getcwd()
    try:
        base_root = pathlib.Path(root_raw).expanduser().resolve()
    except Exception:
        base_root = None

    candidates = []
    # 1. Backtick-enclosed tokens
    for part in content.split('`')[1::2]:
        if any(ext in part.lower() for ext in _AUTO_SOURCE_EXTS):
            candidates.append(part.strip())
    # 2. Whitespace separated tokens
    for tok in content.split():
        if '.' in tok and any(ext in tok.lower() for ext in _AUTO_SOURCE_EXTS):
            candidates.append(tok.strip('\'"()[]<>.,;'))

    for raw in candidates:
        if not raw or '://' in raw or raw.startswith(('http:', 'https:', 'mailto:')):
            continue
        cleaned = _AUTO_SOURCE_LINE_SUFFIX_RE.sub('', raw).strip()
        if not cleaned or not cleaned.lower().endswith(_AUTO_SOURCE_EXTS):
            continue
        try:
            candidate = pathlib.Path(cleaned).expanduser()
            if candidate.is_absolute():
                resolved = candidate.resolve(strict=True)
            else:
                if base_root is None:
                    continue
                resolved = (base_root / candidate).resolve(strict=True)
            if base_root is not None and not resolved.is_relative_to(base_root):
                continue
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if 0 < size <= _MAX_AUTO_SOURCE_BYTES:
                return str(resolved)
        except Exception:
            continue
    return None


def _source_metadata(source_file=None, source_commit=None, source_hash=None,
                     *, content=None, auto_source_file: bool = True):
    """Normalize file provenance and bind its SHA-256 to the saved contents."""
    if source_file is None:
        if source_hash is not None:
            raise MemCoreError('source_hash requires source_file')
        if auto_source_file and content:
            detected = _auto_detect_source_file(content)
            if detected is not None:
                try:
                    return _sha256_file(detected), source_commit, detected
                except Exception:
                    pass
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


#: Public alias for file hash calculation
compute_file_hash = _sha256_file


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
        vec = embedding.get_embedding(content, timeout=2.5)
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


def _validate_when_to_use(when_to_use) -> str | None:
    """Validate + scrub optional situational disclosure trigger."""
    if when_to_use is None:
        return None
    if not isinstance(when_to_use, str):
        raise MemCoreError('when_to_use must be a string')
    try:
        when_to_use.encode('utf-8')
    except UnicodeEncodeError:
        raise MemCoreError('when_to_use contains unencodable surrogate characters')
    if '\x00' in when_to_use:
        raise MemCoreError('when_to_use must not contain null bytes')
    cleaned = scrub_secrets(when_to_use).strip()
    return cleaned or None


def _prepare_memory_write(content, scope, lifecycle, reason, scope_detail,
                          source_file, source_commit, source_hash,
                          when_to_use=None, auto_source_file: bool = True):
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
    clean_when = _validate_when_to_use(when_to_use)
    source_hash, source_commit, source_file = _source_metadata(
        source_file, source_commit, source_hash,
        content=content, auto_source_file=auto_source_file
    )
    return {
        'content': content,
        'reason': reason,
        'when_to_use': clean_when,
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



_TABLE_COLS_CACHE_MAX = 128
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
    if cols:
        if len(_CONN_TABLE_COLS) >= _TABLE_COLS_CACHE_MAX:
            _CONN_TABLE_COLS.clear()
        _CONN_TABLE_COLS[key] = (marker, cols)
    else:
        _CONN_TABLE_COLS.pop(key, None)
    return cols


def _eval_now_iso() -> str:
    """Simulated now as an ISO-8601 Z string: FAKE_NOW else real clock."""
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


REINFORCEMENT_WINDOW_DAYS = 14
FRESHNESS_AGING_DAYS = 30
FRESHNESS_STALE_DAYS = 90


def _insert_memory_rows(conn, project_id, agent_id, content, scope, memory_type,
                        lifecycle, claim_fp, reason, detail, source_hash,
                        source_commit, source_file, when_to_use=None):
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
    init_verif = 'source_backed' if source_file else 'unverified'
    if 'scope_detail' in mem_cols:
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id, claim_fingerprint, '
            '  created_at, updated_at, scope_detail) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?, ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, init_verif, ver_id, claim_fp, now, now, detail)
        )
    else:
        conn.execute(
            'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
            '  lifecycle, verification, freshness, current_version_id, claim_fingerprint, '
            '  created_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?)",
            (mem_id, project_id, scope, agent_id, memory_type,
             lifecycle, init_verif, ver_id, claim_fp, now, now)
        )
    ver_cols = _table_cols(conn, 'memory_version')
    if 'when_to_use' in ver_cols:
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, when_to_use, reason, '
            '  created_by_agent_id, created_at, valid_from, source_hash, '
            '  source_commit, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (ver_id, mem_id, content, when_to_use, reason, agent_id, now, now,
             source_hash, source_commit, source_file)
        )
    else:
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
                  source_commit=None, source_hash=None, when_to_use=None,
                  auto_source_file: bool = True, _manage_transaction=True):
    """Create a memory + first immutable version. Tombstone guard applies.

    Returns (memory_id, version_id) or existing ids if idempotency_key replays.
    """
    if not isinstance(memory_type, str) or not memory_type.strip():
        raise MemCoreError('memory_type must be a non-empty string')
    prepared = _prepare_memory_write(
        content, scope, lifecycle, reason, scope_detail,
        source_file, source_commit, source_hash,
        when_to_use=when_to_use, auto_source_file=auto_source_file
    )
    content = prepared['content']
    claim_fp = prepared['claim_fp']
    clean_when = prepared['when_to_use']

    # H1: embedding fetch is network/model I/O and runs OUTSIDE the write
    # lock. Batched callers (manage=False) already hold a transaction and
    # skip embedding rather than firing HTTP inside it.
    embed_vec = None
    if _manage_transaction:
        try:
            if store._table_exists(conn, 'memory_embedding'):
                embed_text = f'{clean_when}\n{content}' if clean_when else content
                embed_vec = _fetch_embedding_safe(embed_text)
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
            prepared['source_file'], when_to_use=clean_when
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
                              source_file, when_to_use=None):
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
    ver_cols = _table_cols(conn, 'memory_version')
    if 'when_to_use' in ver_cols:
        conn.execute(
            'INSERT INTO memory_version (id, memory_id, content, when_to_use, reason, '
            '  created_by_agent_id, supersedes_version_id, created_at, valid_from, '
            '  source_hash, source_commit, source_file) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (new_ver, memory_id, new_content, when_to_use, reason, agent_id, old_ver, now, now,
             source_hash, source_commit, source_file)
        )
    else:
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
              source_file=None, source_commit=None, source_hash=None,
              when_to_use=None, auto_source_file: bool = True):
    """Correct a memory in place while preserving immutable version history.

    The old claim receives a scope-appropriate refusal fingerprint, its
    validity interval closes, and the replacement returns to candidate /
    unverified so trust is never inherited across changed content.
    """
    new_content = _validate_supersede_content(new_content)
    reason = scrub_secrets(reason) if isinstance(reason, str) else reason
    clean_when = _validate_when_to_use(when_to_use) if when_to_use is not None else None
    source_hash, source_commit, source_file = _source_metadata(
        source_file, source_commit, source_hash,
        content=new_content, auto_source_file=auto_source_file
    )
    # H1: fetch the replacement's embedding before taking the write lock.
    embed_vec = None
    try:
        if store._table_exists(conn, 'memory_embedding'):
            embed_text = f'{clean_when}\n{new_content}' if clean_when else new_content
            embed_vec = _fetch_embedding_safe(embed_text)
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

        if when_to_use is None:
            effective_when = None
            if 'when_to_use' in _table_cols(conn, 'memory_version'):
                prev_row = conn.execute(
                    'SELECT when_to_use FROM memory_version WHERE id=?', (old_ver,)
                ).fetchone()
                effective_when = prev_row[0] if prev_row and prev_row[0] else None
        else:
            effective_when = clean_when

        new_ver, now = _close_and_insert_version(
            conn, memory_id, agent_id, new_content, reason, old_ver, new_fp,
            source_hash, source_commit, source_file, when_to_use=effective_when)
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
    if not isinstance(project_id, str):
        return []
    try:
        limit_pairs = int(limit_pairs)
        if limit_pairs < 1:
            return []
    except (TypeError, ValueError, OverflowError):
        return []
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
    if not isinstance(project_id, str) or not isinstance(content, str):
        return []
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

