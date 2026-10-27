"""MemCore CLI — governed memory commands: remember/search/supersede/tombstone/history."""
import sys

from . import core

from .cli_support import (
    _agent_identity_or_exit,
    _open,
    _open_existing,
    _open_readonly,
    _out,
    _project_or_exit,
)




# ── memory subcommands ─────────────────────────────────────────────────

def cmd_remember(args):
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        mem_id, ver_id = core.create_memory(
            conn,
            project_id=project_id,
            agent_id=agent_id,
            content=args.content,
            scope=args.scope,
            memory_type=args.type,
            idempotency_key=args.idempotency_key,
            reason=args.reason,
            source_file=args.source_file,
            source_commit=args.source_commit,
            source_hash=args.source_hash,
            when_to_use=getattr(args, 'when_to_use', None),
        )
        print(f'remembered: {mem_id} (version {ver_id})')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_search(args):
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        try:
            rows = core.search(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                query=args.query,
                limit=args.limit,
                mode=getattr(args, 'mode', 'standard'),
                as_of=getattr(args, 'as_of', None),
                changed_since=getattr(args, 'changed_since', None),
            )
        except core.MemCoreError as e:
            sys.exit(f'error: {e}')
    finally:
        conn.close()
    if not rows:
        print('(no results)')
        return
    for r in rows:
        scope_tag = 'SHARED' if r[1] == 'project' else 'PRIVATE'
        print(f'[{r[0]}] ({scope_tag}, {r[2]}) {r[5]}')
        print(f'    rank={r[7]:.3f}')



def cmd_promote(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        core.promote(conn, args.memory_id, agent_id)
        print(f'promoted: {args.memory_id} -> project scope')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_supersede(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        new_ver = core.supersede(
            conn, args.memory_id, agent_id,
            args.content, reason=args.reason,
            source_file=args.source_file,
            source_commit=args.source_commit,
            source_hash=args.source_hash,
            when_to_use=getattr(args, 'when_to_use', None),
        )
        print(f'superseded: {args.memory_id} -> new version {new_ver}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_deactivate(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        core.deactivate(conn, args.memory_id, agent_id)
        print(f'deactivated: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_restore(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        core.restore(conn, args.memory_id, agent_id)
        print(f'restored: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_reject(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        result = core.reject(conn, args.memory_id, agent_id, args.reason)
        if result['swept']:
            print(f"rejected + tombstoned: {args.memory_id} "
                  f"(swept {result['swept']} duplicate(s): "
                  f"{', '.join(result['swept_ids'])})")
        else:
            print(f'rejected + tombstoned: {args.memory_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_reject_value(args):
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        result = core.reject_value(
            conn, project_id, agent_id, args.content, args.reason)
        print(f"rejected value + tombstoned: {result['fingerprint'][:8]}... "
              f"(tombstone {result['tombstone_id']})")
        if result['swept']:
            print(f"swept {result['swept']} duplicate(s): "
                  f"{', '.join(result['swept_ids'])}")
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_tombstone_list(args):
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        rows = core.list_tombstones(conn, project_id, agent_id)
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()
    if not rows:
        print('(no active refusal guards)')
        return
    for tomb_id, claim_fp, scope, reason, created_at in rows:
        print(f'{tomb_id} fp:{claim_fp[:8]} scope:{scope} '
              f'created:{created_at} reason:{reason}')



def cmd_tombstone_unreject(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        outcome = core.unreject_tombstone(conn, args.tombstone_ref, agent_id)
        print(('unrejected' if outcome['overridden'] else 'already overridden')
              + f": {outcome['tombstone_id']}")
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_tombstone_override(args):
    conn = _open(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        changed = core.override_tombstone(conn, args.tombstone_id, agent_id)
        print(('overridden' if changed else 'already overridden') + f': {args.tombstone_id}')
    except core.MemCoreError as e:
        sys.exit(f'error: {e}')
    finally:
        conn.close()



def cmd_contradictions(args):
    """Scan one project for disagreeing claim pairs (read-only)."""
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        pairs = core.scan_contradictions(conn, project_id, limit_pairs=args.limit)
    finally:
        conn.close()
    print(f'contradiction scan: {len(pairs)} candidate pair(s)')
    for a_id, b_id, reason in pairs:
        print(f'  {a_id} <-> {b_id} ({reason})')
    if not pairs:
        print('  no disagreeing pairs found')



def cmd_mark_conflict(args):
    """Mark two memories as conflict (governed, audited, reversible)."""
    conn = _open_existing(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        # H2: the named project is an active guard — both rows must live in
        # the project this command was told to operate on.
        for mem_id in (args.memory_a, args.memory_b):
            row = conn.execute(
                'SELECT project_id FROM memory WHERE id = ?', (mem_id,)
            ).fetchone()
            if row is None:
                sys.exit(f'error: memory not found: {mem_id}')
            if row[0] != project_id:
                sys.exit(
                    f'error: memory {mem_id} is not in project {args.project}')
        if not args.confirm:
            print('marking conflict is a governed mutation. Re-run with --confirm.')
            print(f'  pair: {args.memory_a} <-> {args.memory_b}')
            print(f'  reason: {args.reason}')
            return
        try:
            core.mark_contradiction(
                conn, args.memory_a, args.memory_b, agent_id, args.reason)
        except core.MemCoreError as e:
            sys.exit(f'error: {e}')
        conn.commit()
        print(f'marked conflict: {args.memory_a} <-> {args.memory_b}')
    finally:
        conn.close()



def cmd_history(args):
    """Show the version valid at a timestamp (point-in-time read)."""
    conn = _open_readonly(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        row = core.version_at(conn, args.memory_id, agent_id, args.as_of)
    finally:
        conn.close()
    if row is None:
        print('no version valid at that time (or not accessible)')
        return
    ver_id, content, valid_from, valid_until, by = row
    print(f'version: {ver_id}')
    print(f'valid: {valid_from} .. {valid_until or "now"}')
    print(f'by: {by}')
    print(f'content: {content}')



def cmd_corroborate(args):
    """Scan one project's fingerprints for corroboration sets (dry-run default)."""
    conn = _open(args) if args.apply else _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        rows = conn.execute(
            'SELECT claim_fingerprint, COUNT(DISTINCT owner_agent_id) AS sources, '
            '  COUNT(*) AS copies '
            'FROM memory '
            'WHERE project_id=? AND claim_fingerprint IS NOT NULL '
            "  AND lifecycle IN ('candidate','accepted','conflict') "
            'GROUP BY claim_fingerprint HAVING sources >= 2 '
            'ORDER BY sources DESC, copies DESC',
            (project_id,),
        ).fetchall()
        results = []
        for claim_fp, sources, copies in rows:
            if args.apply:
                outcome = core.maybe_auto_corrob(conn, project_id, claim_fp, agent_id)
            else:
                outcome = {'fingerprint': claim_fp, 'sources': sources,
                           'action': ('would_accept' if sources >= core.CORROBORATE_ACCEPT_N
                                      else 'below_threshold')}
            outcome['copies'] = copies
            results.append(outcome)
        acted = [r for r in results if r.get('action') in ('accepted', 'golden')]
    finally:
        conn.close()
    print(f'corroboration scan: {len(results)} multi-source claim(s)')
    for r in results:
        extra = f" canonical={r['canonical']}" if 'canonical' in r else ''
        print(f"  {r['fingerprint'][:8]}… sources={r['sources']} "
              f"copies={r['copies']} action={r['action']}{extra}")
    if args.apply:
        print(f'  promoted: {len(acted)}')
    else:
        print('  dry-run: no writes (use --apply)')



def cmd_golden_list(args):
    """List the Golden Rule set: pinned+critical rows (always injected)."""
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        rows = conn.execute(
            'SELECT m.id, m.owner_agent_id, m.lifecycle, m.verification, '
            '  v.content, m.updated_at '
            'FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id AND v.memory_id=m.id '
            'WHERE m.project_id=? AND m.pinned=1 AND m.critical=1 '
            "  AND m.lifecycle IN ('candidate','accepted','conflict') "
            'ORDER BY datetime(m.updated_at) DESC, m.id ASC',
            (project_id,),
        ).fetchall()
    finally:
        conn.close()
    _out({'project_id': project_id, 'golden_count': len(rows), 'golden': [
        {'id': r[0], 'owner': r[1], 'lifecycle': r[2], 'verification': r[3],
         'content': r[4], 'updated_at': r[5]}
        for r in rows
    ]})



def cmd_decay(args):
    """Freshness decay sweep: current → aging → stale (dry-run default)."""
    conn = _open(args) if args.apply else _open_readonly(args)
    try:
        # C1: both modes run core.apply_freshness_decay's single scan so the
        # dry-run lists always match what --apply will do (previously the
        # dry-run SQL missed the last_recalled reinforcement window).
        try:
            aged, staled = core.apply_freshness_decay(
                conn, aging_days=args.aging_days, stale_days=args.stale_days,
                dry_run=not args.apply)
        except core.MemCoreError as e:
            sys.exit(f'error: {e}')
    finally:
        conn.close()
    print(f'decay ({"applied" if args.apply else "dry-run"}):')
    print(f'  current → aging (>{args.aging_days}d): {len(aged)}')
    for mid in aged:
        print(f'    {mid}')
    print(f'  aging → stale (>{args.stale_days}d): {len(staled)}')
    for mid in staled:
        print(f'    {mid}')
