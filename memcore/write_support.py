"""MemCore writes — shared primitives: errors, secret scrubbing, ids/hashes, sqlite introspection, time."""
import hashlib
import json
import os
import pathlib
import re
import unicodedata
import uuid

from . import ablation as _ablation
from . import embedding
from . import store
from datetime import datetime
from datetime import timezone



__version__ = "2.5.0"  # keep in step with pyproject; read via importlib.metadata



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



def _membership_role(conn, project_id, agent_id):
    if not isinstance(project_id, str) or not isinstance(agent_id, str):
        return None
    row = conn.execute(
        'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
        (project_id, agent_id)
    ).fetchone()
    return row[0] if row else None



# Ã¢â€â‚¬Ã¢â€â‚¬ writes Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬

#: Allowed scope_detail prefixes. A detail tag subdivides private scope
#: for filtering; it never widens read access (project -> members, anything
#: else -> owner only, unchanged).
SCOPE_DETAIL_PREFIXES = ('skill:', 'episode:', 'session:')



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
