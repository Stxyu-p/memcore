"""
Local embedding client for MemCore with resilient circuit breaking.

Defaults to local in-process ONNX embeddings using the shared global model
(Xenova/multilingual-e5-small) across OS platforms. Supports OpenAI-compatible
and Ollama endpoints when explicitly configured. Fails closed to None so callers
can gracefully fall back to lexical FTS5 search without disrupting operations.
"""
import array
import json
import math
import operator
import os
import pathlib
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Any

def _find_global_e5_dir() -> pathlib.Path | None:
    env_dir = os.environ.get('HERMES_MODELS_DIR')
    candidates = []
    if env_dir:
        candidates.append(pathlib.Path(env_dir))

    hermes_home = os.environ.get('HERMES_HOME')
    if hermes_home:
        candidates.append(pathlib.Path(hermes_home) / 'models')

    if os.name == 'nt':
        local_app_data = os.environ.get('LOCALAPPDATA')
        if local_app_data:
            base = pathlib.Path(local_app_data) / 'hermes'
            candidates.append(base / 'models')
            candidates.append(base / 'plugin-data' / 'skill-proof' / 'models')
    else:
        xdg_data = os.environ.get('XDG_DATA_HOME')
        base = pathlib.Path(xdg_data) / 'hermes' if xdg_data else pathlib.Path.home() / '.local' / 'share' / 'hermes'
        candidates.append(base / 'models')
        candidates.append(base / 'plugin-data' / 'skill-proof' / 'models')

    candidates.append(pathlib.Path.home() / '.hermes' / 'models')
    for c in candidates:
        target = c / 'Xenova_multilingual-e5-small'
        if (target / 'tokenizer.json').is_file() and (target / 'onnx' / 'model_quantized.onnx').is_file():
            return target
    return None

DEFAULT_ENDPOINT = os.environ.get(
    'MEMCORE_EMBEDDING_URL',
    'in-process'
)
DEFAULT_MODEL = os.environ.get(
    'MEMCORE_EMBEDDING_MODEL',
    'Xenova/multilingual-e5-small'
)

PROVIDERS = {
    'openai': {
        'url': 'https://api.openai.com/v1/embeddings',
        'key_env': ('MEMCORE_EMBEDDING_KEY', 'OPENAI_API_KEY'),
        'default_model': 'text-embedding-3-small',
    },
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/embeddings',
        'key_env': ('MEMCORE_EMBEDDING_KEY', 'OPENROUTER_API_KEY'),
        'default_model': 'text-embedding-3-small',
    },
    'ollama': {
        'url': 'http://localhost:11434/v1/embeddings',
        'key_env': (),
        'default_model': 'nomic-embed-text',
    },
}


def _preset_key(preset):
    """First non-empty API key among the preset's key_env vars, else None."""
    for env_var in preset.get('key_env', ()):
        val = os.environ.get(env_var)
        if val:
            return val
    return None


def _preset_targets(preset, model):
    """(url, model) resolution: explicit flag > env > preset > default."""
    url = (os.environ.get('MEMCORE_EMBEDDING_URL')
           or preset.get('url')
           or DEFAULT_ENDPOINT)
    resolved_model = (model
                      or os.environ.get('MEMCORE_EMBEDDING_MODEL')
                      or preset.get('default_model')
                      or DEFAULT_MODEL)
    return url, resolved_model


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
    if p_name in ('local', 'onnx', 'e5', 'xenova', 'fastembed', 'model2vec') or not p_name:
        if not p_name and os.environ.get('MEMCORE_EMBEDDING_URL'):
            return (
                os.environ['MEMCORE_EMBEDDING_URL'],
                model or os.environ.get('MEMCORE_EMBEDDING_MODEL') or DEFAULT_MODEL,
                api_key or os.environ.get('MEMCORE_EMBEDDING_KEY'),
            )
        return 'in-process', model or os.environ.get('MEMCORE_EMBEDDING_MODEL') or 'Xenova/multilingual-e5-small', None

    preset = PROVIDERS.get(p_name, {})
    target_url, target_model = _preset_targets(preset, model)
    target_key = api_key or os.environ.get('MEMCORE_EMBEDDING_KEY')
    if not target_key and preset:
        target_key = _preset_key(preset)
    return target_url, target_model, target_key

CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_COOLDOWN_SECONDS = 30.0

#: Guards the circuit-breaker counters and the fastembed model cache; both are
#: process-global state written from worker threads (fleet hooks, tool calls).
_state_lock = threading.Lock()

_consecutive_failures = 0
_circuit_open_until = 0.0
_probe_in_flight = False


def is_circuit_open() -> bool:
    """Return True if circuit is open or a half-open probe is already in flight."""
    global _circuit_open_until, _probe_in_flight
    with _state_lock:
        if _circuit_open_until <= 0.0:
            return False
        if time.monotonic() >= _circuit_open_until:
            if _probe_in_flight:
                return True
            _probe_in_flight = True
            return False
        return True


def pack_vector(floats: list[float]) -> bytes:
    """Pack a list of float numbers into an IEEE 754 float32 little-endian binary BLOB."""
    if not floats:
        return b''
    return struct.pack(f'<{len(floats)}f', *floats)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack an IEEE 754 float32 little-endian binary BLOB into a list of floats.

    Returns [] on None, non-bytes, or truncated/corrupted buffer.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)) or len(blob) < 4:
        return []
    count = len(blob) // 4
    try:
        return list(struct.unpack(f'<{count}f', blob[:count * 4]))
    except struct.error:
        return []


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two float vectors.

    Returns float in [-1.0, 1.0]. Zero-vectors or mismatched dims return 0.0.
    Uses C-level array/operator math (no Python-level per-element loop).
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    try:
        arr_a = array.array('f', vec_a)
        arr_b = array.array('f', vec_b)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    dot = sum(map(operator.mul, arr_a, arr_b))
    norm_a = sum(map(operator.mul, arr_a, arr_a))
    norm_b = sum(map(operator.mul, arr_b, arr_b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    sim = dot / math.sqrt(norm_a * norm_b)
    if math.isnan(sim) or math.isinf(sim):
        return 0.0
    return max(-1.0, min(1.0, sim))


def reset_circuit_breaker() -> None:
    """Reset circuit breaker failure counters and cooldown."""
    global _consecutive_failures, _circuit_open_until, _probe_in_flight
    with _state_lock:
        _consecutive_failures = 0
        _circuit_open_until = 0.0
        _probe_in_flight = False


def _record_failure() -> None:
    global _consecutive_failures, _circuit_open_until, _probe_in_flight
    with _state_lock:
        _probe_in_flight = False
        _consecutive_failures += 1
        if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
            _circuit_open_until = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS


def _record_success() -> None:
    global _consecutive_failures, _circuit_open_until, _probe_in_flight
    with _state_lock:
        _consecutive_failures = 0
        _circuit_open_until = 0.0
        _probe_in_flight = False


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


_onnx_cache: dict[str, Any] = {}


def _get_onnx_e5_batch(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    target_dir = _find_global_e5_dir()
    if not target_dir:
        return None
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError:
        return None

    state = _onnx_cache.get('e5')
    if state is None:
        with _state_lock:
            state = _onnx_cache.get('e5')
            if state is None:
                try:
                    tok = Tokenizer.from_file(str(target_dir / 'tokenizer.json'))
                    tok.enable_truncation(max_length=512)
                    tok.enable_padding(pad_id=0, pad_token='<pad>')
                    sess_opts = ort.SessionOptions()
                    sess_opts.intra_op_num_threads = 2
                    sess = ort.InferenceSession(
                        str(target_dir / 'onnx' / 'model_quantized.onnx'),
                        sess_options=sess_opts,
                        providers=['CPUExecutionProvider']
                    )
                    state = (tok, sess)
                    _onnx_cache['e5'] = state
                except Exception:
                    return None

    tok, sess = state
    try:
        import numpy as np
        encodings = tok.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        feed = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'token_type_ids': np.zeros_like(input_ids, dtype=np.int64),
        }
        outs = sess.run(None, feed)
        last_hidden = outs[0]
        mask_exp = np.expand_dims(attention_mask, -1).astype(float)
        sum_emb = np.sum(last_hidden * mask_exp, axis=1)
        sum_mask = np.clip(mask_exp.sum(axis=1), a_min=1e-9, a_max=None)
        pooled = sum_emb / sum_mask
        norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), a_min=1e-9, a_max=None)
        normed = pooled / norms
        return [list(float(v) for v in row) for row in normed]
    except Exception:
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


_model2vec_cache: dict[str, Any] = {}


def _get_model2vec_batch(texts: list[str], model: str | None = None) -> list[list[float]] | None:
    try:
        from model2vec import StaticModel
    except ImportError:
        return None
    model_name = model or 'minishlab/potion-base-8M'
    embedder = _model2vec_cache.get(model_name)
    if embedder is None:
        with _state_lock:
            embedder = _model2vec_cache.get(model_name)
            if embedder is None:
                embedder = StaticModel.from_pretrained(model_name)
                _model2vec_cache[model_name] = embedder
    try:
        vecs = embedder.encode(texts)
        return [list(vec) for vec in vecs]
    except Exception:
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
    if target_endpoint == 'in-process':
        res = _get_onnx_e5_batch(texts, model=target_model)
        if res is None:
            res = _get_fastembed_batch(texts, model=target_model)
        if res is None:
            res = _get_model2vec_batch(texts, model=target_model)
        return res

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
