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



def cmd_list(args):
    """Browse memories this agent may read (read-only, filterable, paginated)."""
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        where = ["m.project_id = ?", "(m.scope = 'project' OR m.owner_agent_id = ?)"]
        params = [project_id, agent_id]
        if args.scope:
            where.append('m.scope = ?')
            params.append(args.scope)
        if args.lifecycle:
            where.append('m.lifecycle = ?')
            params.append(args.lifecycle)
        else:
            where.append("m.lifecycle NOT IN ('superseded','rejected','disabled')")
        if args.type:
            where.append('m.type = ?')
            params.append(args.type)
        if args.verification:
            where.append('m.verification = ?')
            params.append(args.verification)
        if args.freshness:
            where.append('m.freshness = ?')
            params.append(args.freshness)
        sql = (
            'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
            '       m.type, m.pinned, m.critical, COALESCE(m.recall_count,0), '
            '       m.updated_at, '
            '       EXISTS(SELECT 1 FROM tombstone t WHERE '
            '              t.claim_fingerprint=m.claim_fingerprint '
            '              AND t.overridden_by IS NULL) AS tomb, '
            '       v.content '
            'FROM memory m '
            'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
            'WHERE ' + ' AND '.join(where) + ' '
            'ORDER BY m.updated_at DESC, m.id ASC LIMIT ? OFFSET ?'
        )
        rows = conn.execute(sql, params + [args.limit, args.offset]).fetchall()
    finally:
        conn.close()
    if args.json:
        _out([
            {'id': r[0], 'scope': r[1], 'lifecycle': r[2],
             'verification': r[3], 'freshness': r[4], 'type': r[5],
             'pinned': bool(r[6]), 'critical': bool(r[7]),
             'recall_count': r[8], 'updated_at': r[9],
             'tombstoned': bool(r[10]), 'content': r[11]}
            for r in rows
        ])
        return
    if not rows:
        print('(no memories)')
        return
    for r in rows:
        flags = ' TOMBSTONED' if r[10] else ''
        print(f'[{r[0]}] {r[1]}/{r[2]}/{r[3]}/{r[4]} {r[5]} rc={r[8]}{flags}')
        preview = ' '.join(str(r[11] or '').split())
        if len(preview) > 160:
            preview = preview[:160] + '...'
        print(f'    {preview}')


def cmd_show(args):
    """Full inspector for one memory: labels, provenance, versions, tombstone, links."""
    conn = _open_readonly(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        m = conn.execute(
            'SELECT id, project_id, scope, owner_agent_id, type, lifecycle, '
            '       verification, freshness, current_version_id, pinned, critical, '
            '       created_at, updated_at, claim_fingerprint, '
            '       COALESCE(recall_count,0), last_recalled '
            'FROM memory WHERE id = ?', (args.memory_id,)
        ).fetchone()
        if m is None:
            sys.exit(f'error: memory {args.memory_id} not found')
        project_id = m[1]
        if core._membership_role(conn, project_id, agent_id) is None:
            sys.exit("error: agent is not a member of this memory's project")
        if m[2] == 'private' and m[3] != agent_id:
            sys.exit('error: private memory is owned by another agent')
        v = conn.execute(
            'SELECT id, content, reason, created_by_agent_id, created_at, '
            '       validated_at, valid_from, valid_until, supersedes_version_id, '
            '       source_hash, source_commit, source_file, when_to_use '
            'FROM memory_version WHERE id = ?', (m[8],)
        ).fetchone()
        versions = conn.execute(
            'SELECT id, valid_from, valid_until, supersedes_version_id, '
            '       created_by_agent_id, LENGTH(content) '
            'FROM memory_version WHERE memory_id = ? '
            'ORDER BY datetime(valid_from) ASC, id ASC', (args.memory_id,)
        ).fetchall()
        tombstones = conn.execute(
            'SELECT id, scope, reason, created_at FROM tombstone '
            'WHERE claim_fingerprint = ? AND overridden_by IS NULL '
            'ORDER BY created_at DESC', (m[13],)
        ).fetchall()
        triples = conn.execute(
            'SELECT subject, predicate, object FROM memory_triple '
            'WHERE memory_id = ? AND version_id = ? ORDER BY id',
            (args.memory_id, m[8])
        ).fetchall()
        links = conn.execute(
            'SELECT dl.decision_id, d.title, d.status, d.lifecycle, dl.relation '
            'FROM decision_link dl JOIN decision d ON d.id = dl.decision_id '
            "WHERE dl.target_type = 'memory' AND dl.target_id = ? "
            'ORDER BY dl.decision_id', (args.memory_id,)
        ).fetchall()
    finally:
        conn.close()
    if args.json:
        _out({
            'memory': {
                'id': m[0], 'project_id': m[1], 'scope': m[2],
                'owner_agent_id': m[3], 'type': m[4], 'lifecycle': m[5],
                'verification': m[6], 'freshness': m[7],
                'current_version_id': m[8], 'pinned': bool(m[9]),
                'critical': bool(m[10]), 'created_at': m[11],
                'updated_at': m[12], 'claim_fingerprint': m[13],
                'recall_count': m[14], 'last_recalled': m[15],
            },
            'version': (None if v is None else {
                'id': v[0], 'content': v[1], 'reason': v[2],
                'created_by_agent_id': v[3], 'created_at': v[4],
                'validated_at': v[5], 'valid_from': v[6],
                'valid_until': v[7], 'supersedes_version_id': v[8],
                'source_hash': v[9], 'source_commit': v[10],
                'source_file': v[11], 'when_to_use': v[12],
            }),
            'versions': [
                {'id': r[0], 'valid_from': r[1], 'valid_until': r[2],
                 'supersedes_version_id': r[3], 'created_by_agent_id': r[4],
                 'content_chars': r[5]}
                for r in versions
            ],
            'tombstones': [
                {'id': r[0], 'scope': r[1], 'reason': r[2], 'created_at': r[3]}
                for r in tombstones
            ],
            'triples': [
                {'subject': r[0], 'predicate': r[1], 'object': r[2]}
                for r in triples
            ],
            'decisions': [
                {'id': r[0], 'title': r[1], 'status': r[2],
                 'lifecycle': r[3], 'relation': r[4]}
                for r in links
            ],
        })
        return
    print(f'memory: {m[0]}')
    print(f'project: {m[1]}')
    print(f'labels: {m[2]} | {m[5]} | {m[6]} | {m[7]}')
    print(f'type: {m[4]}  owner: {m[3]}  '
          f'pinned={bool(m[9])} critical={bool(m[10])}')
    print(f'created: {m[11]}  updated: {m[12]}')
    print(f'recall_count: {m[14]}  last_recalled: {m[15] or "never"}')
    print(f'claim_fingerprint: {m[13]}')
    print()
    print('content:')
    print(v[1] if v is not None else '(missing current version)')
    if v is not None:
        print()
        print('provenance:')
        print(f'  version: {v[0]}  by: {v[3]}  at: {v[4]}')
        print(f'  valid: {v[6]} .. {v[7] or "now"}')
        print(f'  reason: {v[2] or "-"}')
        print(f'  source_file: {v[11] or "-"}')
        print(f'  source_commit: {v[10] or "-"}')
        print(f'  source_hash: {v[9] or "-"}')
        print(f'  when_to_use: {v[12] or "-"}')
    print()
    print(f'versions ({len(versions)}):')
    for ver in versions:
        marker = ' <- current' if ver[0] == m[8] else ''
        print(f'  {ver[0]}  {ver[1]} .. {ver[2] or "now"}  '
              f'by {ver[4]}  {ver[5]} chars{marker}')
    print()
    if tombstones:
        print('tombstones (active):')
        for t in tombstones:
            print(f'  {t[0]}  scope={t[1]}  at={t[3]}  reason={t[2]}')
    else:
        print('tombstones: none active')
    print()
    if triples:
        print('triples:')
        for t in triples:
            print(f'  {t[0]} --{t[1]}--> {t[2]}')
    else:
        print('triples: none')
    print()
    if links:
        print('linked decisions:')
        for l in links:
            print(f'  {l[0]}  [{l[2]}/{l[3]}] {l[1]}  ({l[4]})')
    else:
        print('linked decisions: none')


def cmd_pulse(args):
    """One-screen operational snapshot: counts, recent writes/recalls, pending, freshness."""
    from . import ingest
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        vis = "(m.scope = 'project' OR m.owner_agent_id = ?)"
        base = f'FROM memory m WHERE m.project_id = ? AND {vis}'
        params = [project_id, agent_id]
        by_lifecycle = dict(conn.execute(
            f'SELECT m.lifecycle, COUNT(*) {base} GROUP BY m.lifecycle', params
        ).fetchall())
        by_freshness = dict(conn.execute(
            f'SELECT m.freshness, COUNT(*) {base} GROUP BY m.freshness', params
        ).fetchall())
        writes_24h = conn.execute(
            f"SELECT COUNT(*) {base} "
            "AND datetime(m.created_at) >= datetime('now','-1 day')", params
        ).fetchone()[0]
        recalled_24h = conn.execute(
            f'SELECT COUNT(*) {base} AND m.last_recalled IS NOT NULL '
            "AND datetime(m.last_recalled) >= datetime('now','-1 day')", params
        ).fetchone()[0]
        last = conn.execute(
            'SELECT m.id, m.updated_at, v.content FROM memory m '
            'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
            f'WHERE m.project_id = ? AND {vis} '
            'ORDER BY datetime(m.updated_at) DESC, m.id ASC LIMIT 1', params
        ).fetchone()
        decisions = conn.execute(
            "SELECT COUNT(*) FROM decision WHERE project_id = ? "
            "AND (scope = 'project' OR owner_agent_id = ?)", params
        ).fetchone()[0]
        journal = ingest.journal_stats(conn, project_id, agent_id)
    finally:
        conn.close()
    total = sum(by_lifecycle.values())
    preview = ''
    if last is not None:
        preview = ' '.join(str(last[2] or '').split())[:80]
    data = {
        'memories': {'total': total, 'by_lifecycle': by_lifecycle,
                     'by_freshness': by_freshness},
        'decisions': decisions,
        'writes_24h': writes_24h,
        'recalled_24h_distinct': recalled_24h,
        'pending_reviews': journal.get('semantic_review_pending', 0),
        'last_write': (None if last is None else
                       {'id': last[0], 'at': last[1], 'preview': preview}),
    }
    if args.json:
        _out(data)
        return
    print(f'memories: {total}  ' +
          ' '.join(f'{k}={v}' for k, v in sorted(by_lifecycle.items())))
    print(f'decisions: {decisions}')
    print('freshness: ' +
          ' '.join(f'{k}={v}' for k, v in sorted(by_freshness.items())))
    print(f'writes (24h): {writes_24h}   recalled (24h, distinct): {recalled_24h}')
    print(f"pending reviews: {data['pending_reviews']}")
    if last is not None:
        print(f'last write: {last[0]}  {last[1]}  {preview}')
    else:
        print('last write: none')


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
