"""MemCore recall — decay, retention and freshness SQL fragments."""
from __future__ import annotations

import math

from . import store
from .write import REINFORCEMENT_WINDOW_DAYS
from .write import _eval_now_iso
from .write import _table_cols




# _eval_now_iso and _coerce_now imported from .write


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
# schema table cols cache imported from .write


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
    store._register_math_functions(conn)
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



#: Age thresholds mirroring apply_freshness_decay's defaults, applied when
#: projecting the freshness label at read time.
FRESHNESS_AGING_DAYS = 30

FRESHNESS_STALE_DAYS = 90
