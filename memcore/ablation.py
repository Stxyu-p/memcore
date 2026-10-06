"""MemCore — eval-only ablation switches.

Controlled-experiment flags that neutralize exactly ONE recall mechanism
each, so causal contribution can be measured in isolation.

They are NOT a production configuration surface:
  - no config-file equivalents, deliberately;
  - undocumented in user-facing help;
  - semantics may change with the experiment design.
Production behaviour with all flags unset is byte-identical to before
this module existed.

Flags (set to '1' or 'true'):
  MEMCORE_ABLATE_ALIAS_EXPANSION  _expand_aliases returns query unchanged.
  MEMCORE_ABLATE_THAI_BIGRAM      _thai_bigrams returns [].
  MEMCORE_ABLATE_DECAY            reserved for a future decay lane; parsed
                                  but currently a no-op (must not affect
                                  ranking today).
  MEMCORE_FAKE_NOW                timestamp for simulated-time protocols
                                  (future lanes). Must be the exact
                                  Date.toISOString() form
                                  (YYYY-MM-DDTHH:mm:ss.sssZ); validated by
                                  round-trip, so junk falls back to None
                                  (real clock). Parsed but currently unused
                                  by ranking.

Env-cache pattern: read once per process (ponytail: plain dict, not a
framework). Tests that mutate these env vars MUST call
_reset_ablation_cache() in BOTH setUp AND tearDown.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


_CACHE: dict | None = None


def _is_on(name: str) -> bool:
    value = os.environ.get(name)
    return value == '1' or value == 'true'


def _parse_fake_now(value: str | None):
    """Parse MEMCORE_FAKE_NOW; None when unset/invalid (never raises)."""
    if value is None or value == '':
        return None
    try:
        if not value.endswith('Z') or 'T' not in value:
            return None
        dt = None
        for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
            try:
                dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if dt is None:
            return None
        # Round-trip check mirroring Date.toISOString validation:
        # only the exact millis form passes; junk, locale dates,
        # non-UTC offsets, and rolled-over days (e.g. 02-31) fail.
        millis = dt.microsecond // 1000
        roundtrip = dt.strftime('%Y-%m-%dT%H:%M:%S') + '.%03dZ' % millis
        if roundtrip != value:
            return None
        return dt
    except Exception:
        return None


def _read_flags() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {
        'alias': _is_on('MEMCORE_ABLATE_ALIAS_EXPANSION'),
        'bigram': _is_on('MEMCORE_ABLATE_THAI_BIGRAM'),
        'decay': _is_on('MEMCORE_ABLATE_DECAY'),
        'fake_now': _parse_fake_now(os.environ.get('MEMCORE_FAKE_NOW')),
    }
    return _CACHE


def is_alias_expansion_ablated() -> bool:
    """True when MEMCORE_ABLATE_ALIAS_EXPANSION=1/true."""
    return bool(_read_flags()['alias'])


def is_thai_bigram_ablated() -> bool:
    """True when MEMCORE_ABLATE_THAI_BIGRAM=1/true."""
    return bool(_read_flags()['bigram'])


def is_decay_ablated() -> bool:
    """Reserved stub: parsed, but no behaviour change yet."""
    return bool(_read_flags()['decay'])


def fake_now():
    """Parsed MEMCORE_FAKE_NOW datetime (UTC), or None when unset/invalid.

    Parsed but currently unused by ranking (stub for simulated-time
    protocols). Never raises.
    """
    return _read_flags()['fake_now']


def eval_now():
    """Default now for future lifecycle computations: FAKE_NOW else real clock."""
    fake = _read_flags()['fake_now']
    if fake is not None:
        return fake
    return datetime.now(timezone.utc)


def _reset_ablation_cache() -> None:
    """Test-only: clear the per-process env cache."""
    global _CACHE
    _CACHE = None
