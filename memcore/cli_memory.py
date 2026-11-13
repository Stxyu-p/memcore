"""MemCore CLI — governed memory commands: remember/search/supersede/tombstone/history."""
import sys

from . import core
from .progressive import markdown_outline

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
            topics=getattr(args, 'topics', None),
        )
        print(f'remembered: {mem_id} (version {ver_id})')
        if not getattr(args, 'topics', None):
            hints = core.suggest_topics(conn, project_id, agent_id, args.content)
            if hints:
                top = hints[0]
                print(f"topic suggestion: {top['topic_id']} "
                      f"({top['coverage']:.0%} token overlap) — pass --topic to attach")
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
                topics=getattr(args, 'topics', None),
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
        rows = core.list_memories(
            conn, project_id, agent_id,
            scope=args.scope, lifecycle=args.lifecycle, memory_type=args.type,
            verification=args.verification, freshness=args.freshness,
            limit=args.limit, offset=args.offset,
            topics=getattr(args, 'topics', None),
        )
    finally:
        conn.close()
    if args.json:
        _out(rows)
        return
    if not rows:
        print('(no memories)')
        return
    for r in rows:
        flags = ' TOMBSTONED' if r['tombstoned'] else ''
        print(f"[{r['id']}] {r['scope']}/{r['lifecycle']}/{r['verification']}/"
              f"{r['freshness']} {r['type']} rc={r['recall_count']}{flags}")
        preview = ' '.join(str(r['content'] or '').split())
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
        row = conn.execute(
            'SELECT project_id FROM memory WHERE id = ?', (args.memory_id,)
        ).fetchone()
        if row is None:
            sys.exit(f'error: memory {args.memory_id} not found')
        detail = core.get_memory_detail(conn, row[0], agent_id, args.memory_id)
    finally:
        conn.close()
    if detail is None:
        sys.exit(f'error: memory {args.memory_id} not found or not accessible')
    if args.json:
        _out(detail)
        return
    m = detail['memory']
    v = detail['version']
    versions = detail['versions']
    tombstones = detail['tombstones']
    triples = detail['triples']
    links = detail['decisions']
    print(f"memory: {m['id']}")
    print(f"project: {m['project_id']}")
    print(f"labels: {m['scope']} | {m['lifecycle']} | "
          f"{m['verification']} | {m['freshness']}")
    print(f"type: {m['type']}  owner: {m['owner_agent_id']}  "
          f"pinned={m['pinned']} critical={m['critical']}")
    print(f"created: {m['created_at']}  updated: {m['updated_at']}")
    print(f"recall_count: {m['recall_count']}  "
          f"last_recalled: {m['last_recalled'] or 'never'}")
    print(f"claim_fingerprint: {m['claim_fingerprint']}")
    print()
    print('content:')
    print(v['content'] if v is not None else '(missing current version)')
    if v is not None:
        print()
        print('provenance:')
        print(f"  version: {v['id']}  by: {v['created_by_agent_id']}  "
              f"at: {v['created_at']}")
        print(f"  valid: {v['valid_from']} .. {v['valid_until'] or 'now'}")
        print(f"  reason: {v['reason'] or '-'}")
        print(f"  source_file: {v['source_file'] or '-'}")
        print(f"  source_commit: {v['source_commit'] or '-'}")
        print(f"  source_hash: {v['source_hash'] or '-'}")
        print(f"  when_to_use: {v['when_to_use'] or '-'}")
    print()
    print(f'versions ({len(versions)}):')
    for ver in versions:
        marker = ' <- current' if ver['id'] == m['current_version_id'] else ''
        print(f"  {ver['id']}  {ver['valid_from']} .. {ver['valid_until'] or 'now'}  "
              f"by {ver['created_by_agent_id']}  {ver['content_chars']} chars{marker}")
    print()
    if tombstones:
        print('tombstones (active):')
        for t in tombstones:
            print(f"  {t['id']}  scope={t['scope']}  at={t['created_at']}  "
                  f"reason={t['reason']}")
    else:
        print('tombstones: none active')
    print()
    if triples:
        print('triples:')
        for t in triples:
            print(f"  {t['subject']} --{t['predicate']}--> {t['object']}")
    else:
        print('triples: none')
    print()
    if links:
        print('linked decisions:')
        for l in links:
            print(f"  {l['id']}  [{l['status']}/{l['lifecycle']}] {l['title']}  "
                  f"({l['relation']})")
    else:
        print('linked decisions: none')


def cmd_read(args):
    """Read a memory as a compact outline or delegate to the full inspector."""
    if getattr(args, 'level', 'full') == 'full':
        return cmd_show(args)

    conn = _open_readonly(args)
    try:
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        row = conn.execute(
            'SELECT project_id FROM memory WHERE id = ?', (args.memory_id,)
        ).fetchone()
        if row is None:
            sys.exit(f'error: memory {args.memory_id} not found')
        detail = core.get_memory_detail(conn, row[0], agent_id, args.memory_id)
    finally:
        conn.close()
    if detail is None:
        sys.exit(f'error: memory {args.memory_id} not found or not accessible')

    memory = detail['memory']
    version = detail['version']
    payload = {
        'level': 'outline',
        'memory_id': memory['id'],
        'project_id': memory['project_id'],
        'labels': {
            'scope': memory['scope'],
            'lifecycle': memory['lifecycle'],
            'verification': memory['verification'],
            'freshness': memory['freshness'],
        },
        'type': memory['type'],
        'when_to_use': version['when_to_use'] if version else None,
        'source_file': version['source_file'] if version else None,
        'outline': markdown_outline(version['content'] if version else ''),
    }
    if args.json:
        _out(payload)
        return
    print(f"memory: {payload['memory_id']} ({payload['project_id']})")
    labels = payload['labels']
    print(f"labels: {labels['scope']} | {labels['lifecycle']} | {labels['verification']} | {labels['freshness']}")
    print(f"type: {payload['type']}")
    if payload['when_to_use']:
        print(f"when_to_use: {payload['when_to_use']}")
    print('outline:')
    for line in payload['outline']:
        print(f'  {line}')


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
        recent = conn.execute(
            'SELECT a.action, a.memory_id, a.created_at, m.lifecycle, m.type, v.content '
            'FROM audit_event a '
            'JOIN memory m ON m.id = a.memory_id '
            'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
            'WHERE a.project_id = ? AND a.memory_id IS NOT NULL '
            "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
            'ORDER BY a.id DESC LIMIT 5', params
        ).fetchall()
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
        'recent_writes': [
            {'action': r[0], 'memory_id': r[1], 'at': r[2],
             'lifecycle': r[3], 'type': r[4],
             'preview': ' '.join(str(r[5] or '').split())[:80]}
            for r in recent
        ],
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
    if recent:
        print('recent writes:')
        for r in recent:
            preview = ' '.join(str(r[5] or '').split())[:70]
            print(f'  {r[0]:<12} {r[1]}  {r[2]}  {r[3]}/{r[4]}  {preview}')


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
