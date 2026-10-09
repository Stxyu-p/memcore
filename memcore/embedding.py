"""
Local embedding client for MemCore with resilient circuit breaking.

Connects to 9Router or OpenAI-compatible embedding endpoint (default localhost:20128).
Uses standard library only (urllib.request). Fails closed to None so callers
can gracefully fall back to lexical FTS5 search without disrupting operations.
"""
import json
import os
import time
import urllib.error
import urllib.request

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
    # ponytail: lightweight provider dict; add plugin registry only if non-OpenAI protocols needed
    p_name = (provider or os.environ.get('MEMCORE_EMBEDDING_PROVIDER', '')).lower()
    if p_name in ('none', 'off', 'disabled'):
        return None, '', None

    preset = PROVIDERS.get(p_name, {})
    target_url = (
        endpoint
        or os.environ.get('MEMCORE_EMBEDDING_URL')
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
