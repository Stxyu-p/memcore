"""MemCore writes — acceptance, corroboration and golden promotion policy."""
from . import store

from .write_governance import (
    _current_claim_identity,
    _require_memory_write_access,
    _tombstone_active,
    pre_accept_conflict_check,
)

from .write_support import (
    MemCoreError,
    NotFound,
    TombstoneBlocked,
    _audit,
    _now,
    fingerprint,
)




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
