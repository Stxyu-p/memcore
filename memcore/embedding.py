"""
Local embedding client for MemCore with resilient circuit breaking.

Connects to 9Router or OpenAI-compatible embedding endpoint (default localhost:20128).
Uses standard library only (urllib.request). Fails closed to None so callers
can gracefully fall back to lexical FTS5 search without disrupting operations.
"""
import json
import math
import os
import struct
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_ENDPOINT = os.environ.get(
    'MEMCORE_EMBEDDING_URL', 'http://localhost:20128/v1/embeddings'
)
DEFAULT_MODEL = os.environ.get(
    'MEMCORE_EMBEDDING_MODEL', 'text-embedding-3-small'
)

PROVIDERS = {
    '9router': {
        'url': 'http://localhost:20128/v1/embeddings',
        'key_env': ('MEMCORE_EMBEDDING_KEY', 'HERMES_CUSTOM_LOCALHOST_20128_API_KEY'),
        'default_model': 'text-embedding-3-small',
    },
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/embeddings',
        'key_env': ('MEMCORE_EMBEDDING_KEY', 'OPENROUTER_API_KEY'),
        'default_model': 'text-embedding-3-small',
    },
    'openai': {
        'url': 'https://api.openai.com/v1/embeddings',
        'key_env': ('MEMCORE_EMBEDDING_KEY', 'OPENAI_API_KEY'),
        'default_model': 'text-embedding-3-small',
    },
    'ollama': {
        'url': 'http://localhost:11434/v1/embeddings',
        'key_env': (),
        'default_model': 'nomic-embed-text',
    },
}


def resolve_config(
    provider: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[str | None, str, str | None]:
    """Resolve endpoint, model, and api_key from presets and environment variables."""
    if endpoint:
        target_model = model or os.environ.get('MEMCORE_EMBEDDING_MODEL') or DEFAULT_MODEL
        target_key = api_key or os.environ.get('MEMCORE_EMBEDDING_KEY')
        return endpoint, target_model, target_key

    p_name = (provider or os.environ.get('MEMCORE_EMBEDDING_PROVIDER', '')).lower()
    if p_name in ('none', 'off', 'disabled'):
        return None, '', None
    if p_name in ('local', 'fastembed'):
        return 'in-process', model or 'BAAI/bge-small-en-v1.5', None

    preset = PROVIDERS.get(p_name, {})
    target_url = (
        os.environ.get('MEMCORE_EMBEDDING_URL')
        or preset.get('url')
        or DEFAULT_ENDPOINT
    )
    target_model = (
        model
        or os.environ.get('MEMCORE_EMBEDDING_MODEL')
        or preset.get('default_model')
        or DEFAULT_MODEL
    )

    target_key = api_key or os.environ.get('MEMCORE_EMBEDDING_KEY')
    if not target_key and preset:
        for env_var in preset.get('key_env', ()):
            val = os.environ.get(env_var)
            if val:
                target_key = val
                break

    return target_url, target_model, target_key

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 30.0

_consecutive_failures = 0
_circuit_open_until = 0.0


def is_circuit_open() -> bool:
    """Return True if the circuit breaker is currently open (blocking calls)."""
    global _circuit_open_until
    if _circuit_open_until <= 0.0:
        return False
    if time.time() >= _circuit_open_until:
        _circuit_open_until = 0.0
        return False
    return True


def pack_vector(floats: list[float]) -> bytes:
    """Pack a list of float numbers into an IEEE 754 float32 little-endian binary BLOB."""
    return struct.pack(f'<{len(floats)}f', *floats)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack an IEEE 754 float32 little-endian binary BLOB into a list of floats."""
    count = len(blob) // 4
    return list(struct.unpack(f'<{count}f', blob))


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors.

    Returns float in [-1.0, 1.0]. Zero-vectors or mismatched dims return 0.0.
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for a, b in zip(vec_a, vec_b):
        dot += a * b
        norm_a += a * a
        norm_b += b * b
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


_LIFECYCLE_ORDER = {'accepted': 0, 'conflict': 1}
_VERIF_ORDER = {'user_authoritative': 0, 'runtime_verified': 1, 'source_backed': 2}


def query_vector_candidates(
    conn,
    project_id: str,
    agent_id: str,
    query_vec: list[float],
    limit: int = 20,
    *,
    tombstone_guard: str | None = None,
    freshness_sql: str | None = None,
    freshness_params: tuple = (),
    detail_filter: str = '',
    detail_params: tuple = (),
) -> list[tuple]:
    """Retrieve candidate memories having embeddings and rank by cosine similarity.

    Encapsulates binary vector deserialization, cosine similarity computation,
    and candidate sorting. Returns standard 9-tuple candidate rows:
    (id, scope, lifecycle, verification, freshness, content, owner, rank, fp)
    """
    if not query_vec:
        return []

    query_norm_sq = sum(x * x for x in query_vec)
    if query_norm_sq <= 0.0:
        return []
    query_norm = math.sqrt(query_norm_sq)

    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_embedding'"
        )
        if not cur.fetchone():
            return []
    except Exception:
        return []

    guard_sql = ('AND ' + tombstone_guard) if tombstone_guard else ''
    fresh_proj = (freshness_sql + ' AS freshness') if freshness_sql else "m.freshness AS freshness"

    sql = (
        'SELECT m.id, m.scope, m.lifecycle, m.verification, '
        '       ' + fresh_proj + ', '
        '       v.content, m.owner_agent_id, e.vector, '
        '       m.claim_fingerprint, m.pinned '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'JOIN memory_embedding e ON e.version_id = v.id AND e.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        + guard_sql + ' ' + detail_filter
    )
    params = tuple(freshness_params) + (project_id, agent_id) + tuple(detail_params)

    try:
        cur = conn.execute(sql, params)
        raw_rows = cur.fetchall()
    except Exception:
        return []

    candidates = []
    for row in raw_rows:
        mid, scope, lifecycle, verif, freshness, content, owner, blob, fp, pinned = row
        doc_vec = unpack_vector(blob)
        if len(doc_vec) != len(query_vec):
            continue
        dot = 0.0
        doc_norm_sq = 0.0
        for q, d in zip(query_vec, doc_vec):
            dot += q * d
            doc_norm_sq += d * d
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
    # Return standard 9-tuple shape (excluding pinned helper at index 9)
    return [c[:9] for c in candidates[:limit]]


def reset_circuit_breaker() -> None:
    """Reset circuit breaker failure counters and cooldown."""
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures = 0
    _circuit_open_until = 0.0


def _record_failure() -> None:
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = time.time() + CIRCUIT_COOLDOWN_SECONDS


def _record_success() -> None:
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures = 0
    _circuit_open_until = 0.0


def get_embedding(
    text: str,
    endpoint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    timeout: float = 0.5,
) -> list[float] | None:
    """Fetch an embedding vector for a single text.

    Returns list of floats on success, or None on failure/circuit open.
    """
    res = get_embeddings_batch(
        [text],
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        provider=provider,
        timeout=timeout,
    )
    if res and len(res) > 0:
        return res[0]
    return None


_fastembed_cache: dict[str, Any] = {}


def _get_fastembed_batch(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    # ponytail: lazy-load fastembed if installed, None if missing. Zero extra deps on core.
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None
    model_name = model or 'BAAI/bge-small-en-v1.5'
    if model_name not in _fastembed_cache:
        _fastembed_cache[model_name] = TextEmbedding(model_name=model_name)
    embedder = _fastembed_cache[model_name]
    return [list(vec) for vec in embedder.embed(texts)]


def get_embeddings_batch(
    texts: list[str],
    endpoint: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    timeout: float = 2.0,
) -> list[list[float]] | None:
    """Fetch embeddings for a batch of texts.

    Returns a list of float lists, or None on failure/circuit open.
    """
    if not texts:
        return []
    if is_circuit_open():
        return None

    target_endpoint, target_model, target_key = resolve_config(
        provider=provider, endpoint=endpoint, model=model, api_key=api_key
    )
    if not target_endpoint:
        return None
    if target_endpoint == 'in-process':
        return _get_fastembed_batch(texts, model=target_model)

    payload = {
        'input': texts,
        'model': target_model,
    }
    body = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'MemCore-Embed/0.8',
    }
    if target_key:
        headers['Authorization'] = f'Bearer {target_key}'

    req = urllib.request.Request(
        target_endpoint,
        data=body,
        headers=headers,
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                _record_failure()
                return None
            data = json.loads(resp.read().decode('utf-8'))
            items = data.get('data', [])
            # Sort items by 'index' to maintain order if backend shuffled
            items.sort(key=lambda x: x.get('index', 0))
            vectors = [item['embedding'] for item in items if 'embedding' in item]
            if len(vectors) != len(texts):
                _record_failure()
                return None
            _record_success()
            return vectors
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        _record_failure()
        return None
