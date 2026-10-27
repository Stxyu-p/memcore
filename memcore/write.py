"""MemCore Ã¢â‚¬â€ core memory operations.

All writes: tombstone admission guard -> short transaction -> audit event.
All reads: scope enforced in SQL WHERE (never post-filtering).
"""
import json
import pathlib
import unicodedata

from . import store

from .write_governance import (
    _active_tombstone_id,
    _active_tombstone_row,
    _current_claim_identity,
    _prepare_memory_write,
    _replay_idempotent_create,
    _require_membership,
    _require_memory_write_access,
    _require_nonempty_reason,
    _sweep_duplicate_claims,
    _tombstone_active,
    _tombstone_scope,
    _validate_when_to_use,
    pre_accept_conflict_check,
)

from .write_support import (
    AmbiguousTombstonePrefix,
    ContradictionHold,
    MemCoreError,
    NotFound,
    PermissionDenied,
    RejectResult,
    TombstoneBlocked,
    _audit,
    _coerce_now,
    _fetch_embedding_safe,
    _new_id,
    _now,
    _private_tombstone_scope,
    _sha256_file,
    _source_metadata,
    _store_embedding_safe,
    _table_cols,
    fingerprint,
    scrub_secrets,
)

from .write_corroboration import (
    CORROBORATE_ACCEPT_N,
    GOLDEN_N,
    HIGH_CONFIDENCE_ACCEPT,
    corroboration_members,
    accept_memory,
    auto_accept,
    set_golden,
    _corrob_conflict_hits,
    _corrob_veto,
    _promote_canonical,
    maybe_auto_corrob,
)

from .write_governance import (
    _tombstone_scope,
    admission_allowed,
    _active_tombstone_row,
    _tombstone_active,
    _active_tombstone_id,
    _require_nonempty_reason,
    _sweep_duplicate_claims,
    _require_membership,
    _current_claim_identity,
    _require_memory_write_access,
    _validate_scope_detail,
    _validate_when_to_use,
    _prepare_memory_write,
    _replay_idempotent_create,
    scan_contradictions,
    mark_contradiction,
    pre_accept_conflict_check,
)

from .write_support import (
    __version__,
    MemCoreError,
    TombstoneBlocked,
    ContradictionHold,
    PermissionDenied,
    NotFound,
    AmbiguousTombstonePrefix,
    _SECRET_PATTERNS,
    _SECRET_REDACTED,
    _PRIVATE_TAG_RE,
    _PRIVATE_REDACTED,
    scrub_private_tags,
    scrub_secrets,
    RejectResult,
    fingerprint,
    _new_id,
    _AUTO_SOURCE_EXTS,
    _AUTO_SOURCE_TOKEN_RE,
    _AUTO_SOURCE_LINE_SUFFIX_RE,
    _MAX_AUTO_SOURCE_BYTES,
    _auto_detect_source_file,
    _source_metadata,
    _sha256_file,
    compute_file_hash,
    _now,
    _audit,
    _private_tombstone_scope,
    _membership_role,
    SCOPE_DETAIL_PREFIXES,
    _fetch_embedding_safe,
    _store_embedding_safe,
    _TABLE_COLS_CACHE_MAX,
    _CONN_TABLE_COLS,
    _clear_table_cols_cache,
    _conn_schema_marker,
    _table_cols,
    _eval_now_iso,
    _coerce_now,
)




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
