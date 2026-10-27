"""MemCore writes — admission guards: tombstones, membership, claim identity, contradiction gates."""
import sqlite3

from . import store

from .write_support import (
    ContradictionHold,
    MemCoreError,
    NotFound,
    PermissionDenied,
    SCOPE_DETAIL_PREFIXES,
    TombstoneBlocked,
    _audit,
    _membership_role,
    _now,
    _private_tombstone_scope,
    _source_metadata,
    fingerprint,
    scrub_secrets,
)




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
