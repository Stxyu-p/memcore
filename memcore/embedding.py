"""
Local embedding client for MemCore with resilient circuit breaking.

Connects to 9Router or OpenAI-compatible embedding endpoint (default localhost:20128).
Uses standard library only (urllib.request). Fails closed to None so callers
can gracefully fall back to lexical FTS5 search without disrupting operations.
"""
import array
import json
import math
import operator
import os
import struct
import threading
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

#: Guards the circuit-breaker counters and the fastembed model cache; both are
#: process-global state written from worker threads (fleet hooks, tool calls).
_state_lock = threading.Lock()

_consecutive_failures = 0
_circuit_open_until = 0.0


def is_circuit_open() -> bool:
    """Return True if the circuit breaker is currently open (blocking calls)."""
    global _circuit_open_until
    with _state_lock:
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
    Uses C-level array/operator math (no Python-level per-element loop).
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    arr_a = array.array('f', vec_a)
    arr_b = array.array('f', vec_b)
    dot = sum(map(operator.mul, arr_a, arr_b))
    norm_a = sum(map(operator.mul, arr_a, arr_a))
    norm_b = sum(map(operator.mul, arr_b, arr_b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def reset_circuit_breaker() -> None:
    """Reset circuit breaker failure counters and cooldown."""
    global _consecutive_failures, _circuit_open_until
    with _state_lock:
        _consecutive_failures = 0
        _circuit_open_until = 0.0


def _record_failure() -> None:
    global _consecutive_failures, _circuit_open_until
    with _state_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            _circuit_open_until = time.time() + CIRCUIT_COOLDOWN_SECONDS


def _record_success() -> None:
    global _consecutive_failures, _circuit_open_until
    with _state_lock:
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
    embedder = _fastembed_cache.get(model_name)
    if embedder is None:
        # Double-checked locking: exactly one thread performs the costly ONNX
        # model load; the fast path above avoids the lock afterwards.
        with _state_lock:
            embedder = _fastembed_cache.get(model_name)
            if embedder is None:
                embedder = TextEmbedding(model_name=model_name)
                _fastembed_cache[model_name] = embedder
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
