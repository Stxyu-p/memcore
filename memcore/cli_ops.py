"""MemCore CLI — project setup, backup/restore, doctor, import/export, gc, embed."""
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import time

from . import core
from . import embedding
from . import export as export_mod
from . import ingest
from . import store
from .cli_support import DEFAULT_DB
from .cli_support import _agent_identity_or_exit
from .cli_support import _open
from .cli_support import _open_readonly
from .cli_support import _out
from .cli_support import _project_or_exit

from .cli_doctor import (
    _plugin_deployment_report,
    _load_deploy_module,
    cmd_doctor,
    _check_profile_binding_drift,
    _binding_drift_reasons,
    _print_doctor_integrity,
    _print_doctor_bindings,
    _print_doctor_health,
    _print_doctor_report,
    _doctor_unhealthy,
)






# ── setup subcommands ──────────────────────────────────────────────────

def cmd_init(args):
    conn = _open(args)
    conn.close()
    print(f'initialized store at {args.db}')




def cmd_project_add(args):
    conn = _open(args)
    pid = f'proj-{args.name}'
    try:
        by_id = conn.execute(
            'SELECT name FROM project WHERE id=?', (pid,)
        ).fetchone()
        if by_id is not None:
            if by_id[0] != args.name:
                sys.exit(
                    f'error: project id {pid} already exists with name {by_id[0]}'
                )
            print(f'project: {pid}')
            return
        by_name = conn.execute(
            'SELECT id FROM project WHERE name=? ORDER BY id', (args.name,)
        ).fetchall()
        if by_name:
            sys.exit(
                f'error: project name {args.name} already belongs to '
                f'{", ".join(row[0] for row in by_name)}'
            )
        conn.execute(
            'INSERT INTO project (id, name, description) VALUES (?, ?, ?)',
            (pid, args.name, args.description)
        )
        print(f'project: {pid}')
    finally:
        conn.close()




def cmd_project_list(args):
    conn = _open_readonly(args)
    try:
        rows = conn.execute('SELECT id, name, description FROM project ORDER BY name').fetchall()
    finally:
        conn.close()
    _out([{'id': r[0], 'name': r[1], 'description': r[2]} for r in rows])




def cmd_agent_add(args):
    conn = _open(args)
    try:
        aid, exists = _agent_identity_or_exit(conn, args.name)
        if not exists:
            conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, args.name, args.name)
            )
        print(f'agent: {aid}')
    finally:
        conn.close()




def cmd_member_add(args):
    conn = _open(args)
    try:
        pid = _project_or_exit(conn, args.project)
        aid, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {aid} does not exist; create it first')
        with store.write_tx(conn):
            row = conn.execute(
                'SELECT role FROM project_membership WHERE project_id=? AND agent_id=?',
                (pid, aid)
            ).fetchone()
            if row is not None:
                current_role = row[0]
                if current_role != args.role:
                    raise SystemExit(
                        f'error: membership already exists with role {current_role}; '
                        f'requested role {args.role} was not applied'
                    )
                print(f'member: {aid} -> {pid} ({current_role})')
                return
            conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)',
                (pid, aid, args.role)
            )
            core._audit(
                conn, 'agent_joined', None, None, pid,
                {'source': 'memcore member CLI', 'agent_id': aid, 'role': args.role}
            )
            print(f'member: {aid} -> {pid} ({args.role})')
    finally:
        conn.close()




# ── operational tooling ─────────────────────────────────────────────

def cmd_gc(args):
    """GC retention sweep: dry-run by default, --apply performs actual sweep."""
    conn = None
    try:
        if args.candidate_days < 0 or args.tombstone_days < 0:
            sys.exit('error: GC retention days must be >= 0')
        conn = (_open(args) if args.apply else
                store.open_store_readonly(getattr(args, 'db', DEFAULT_DB)))
        journal_days = getattr(args, 'journal_days', None)
        if journal_days is not None and journal_days < 0:
            sys.exit('error: journal retention days must be >= 0')
        _print_gc_scan(conn, args, journal_days)
        if args.apply:
            _apply_gc_and_report(conn, args, journal_days)
    except (core.MemCoreError, store.StoreError) as e:
        sys.exit(f'error: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass




def _print_gc_scan(conn, args, journal_days):
    """Dry-run view: scan candidates, tombstones, prunable journal events."""
    candidates, tombstones = core.gc_scan(
        conn,
        candidate_days=args.candidate_days,
        tombstone_days=args.tombstone_days
    )
    print('GC scan:')
    print(
        f'  candidates (candidate, no evidence, unpinned/non-critical, '
        f'inactive >{args.candidate_days}d): {len(candidates)}'
    )
    for c in candidates:
        print(f'    {c[0]} (project={c[1]}, last_updated={c[4]})')
    print(f'  overridden tombstones (age >{args.tombstone_days}d): {len(tombstones)}')
    for t in tombstones:
        print(f'    {t[0]} (fingerprint={t[1][:8]}..., reason={t[3]})')
    if journal_days is not None:
        cutoff_j = core._cutoff(conn, journal_days)
        prunable = conn.execute(
            "SELECT COUNT(*) FROM ingest_event WHERE status IN ('ignored','processed') "
            "AND created_at <= ?", (cutoff_j,)
        ).fetchone()[0]
        print(f'  prunable journal events (status in ignored/processed, '
              f'age >{journal_days}d): {prunable}')




def _apply_gc_and_report(conn, args, journal_days):
    """--apply leg: run gc_apply (+ journal prune) and report the results."""
    disabled, purged = core.gc_apply(
        conn,
        candidate_days=args.candidate_days,
        tombstone_days=args.tombstone_days
    )
    print(f'  applied: {len(disabled)} disabled, {len(purged)} purged')
    for memory_id in disabled:
        print(f'    disabled: {memory_id}')
    for tid in purged:
        print(f'    purged: {tid}')
    if journal_days is not None:
        pruned_journal = ingest.prune_journal(conn, days=journal_days)
        print(f'  journal: {pruned_journal} historical events pruned')




def _export_config_candidates():
    """Config files consulted by the export binding fallback (env-aware)."""
    hermes_home = pathlib.Path(
        os.environ.get('HERMES_HOME') or (pathlib.Path.home() / '.hermes'))
    profile = os.environ.get('HERMES_PROFILE_NAME') or ''
    candidates = [hermes_home / "config.yaml"]
    if profile:
        candidates.append(
            pathlib.Path.home() / ".hermes" / "profiles" / profile / "config.yaml")
    candidates.append(pathlib.Path.home() / ".hermes" / "config.yaml")
    return candidates




def _default_export_binding():
    """Best-effort (project, agent) for an export with no flags.

    Reads the memcore plugin settings straight out of config.yaml with the stdlib
    yaml-free reader the rest of the CLI already uses, and never imports
    hermes_cli. Measured: `from hermes_cli.config import load_config` costs
    3.7 seconds on this machine because it drags in httpx, rich and asyncio —
    which would make a 17ms command take four seconds and would put an HTTP
    client on the import path of a daemonless, offline engine.
    """
    project = agent = None
    # The active profile is a HERMES_HOME concern, not a hermes_cli import:
    # pulling hermes_cli.profiles in cost another 82ms and its own http.client.
    for path in _export_config_candidates():
        if not path.is_file():
            continue
        try:
            import yaml  # optional; falls back to the explicit flags
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        settings = (((cfg.get("plugins") or {}).get("entries") or {})
                    .get("memcore", {}).get("settings") or {})
        project = settings.get("default_project") or project
        agent = settings.get("agent_name") or agent
        if project and agent:
            break
    return project, agent




def cmd_compact(args):
    """Run governed project memory compaction."""
    from .compaction import compact_project_memories
    conn = _open(args)
    project_id = args.project
    agent_id = getattr(args, 'agent', None) or 'curator'
    days = getattr(args, 'days', 14)
    dry_run = getattr(args, 'dry_run', False)
    res = compact_project_memories(conn, project_id=project_id, agent_id=agent_id,
                                  older_than_days=days, dry_run=dry_run)
    if dry_run:
        print(f"Dry-run: found {res['compacted_count']} candidate memories eligible for compaction in project '{project_id}'")
    else:
        print(f"Compacted {res['compacted_count']} memories in project '{project_id}' -> new decision memory {res['new_memory_id']}")


def cmd_export(args):
    """Write governed memory to a file every coding agent already reads."""
    conn = None
    try:
        project = args.project
        agent = args.agent
        if project is None or agent is None:
            bound_project, bound_agent = _default_export_binding()
            project = project or bound_project
            agent = agent or bound_agent
        conn = _open_readonly(args)
        project_id = _project_or_exit(conn, project)
        grouped = bool(getattr(args, 'grouped', False))
        include_tombstones = bool(getattr(args, 'include_tombstones', False))
        if args.stdout:
            rows = export_mod.rank_for_export(
                conn, project_id, 'agent-' + agent, args.limit, args.include_private)
            tombstones = None
            if include_tombstones and core._membership_role(conn, project_id, 'agent-' + agent) is not None:
                all_tombs = core.list_tombstones(conn, project_id, 'agent-' + agent)
                if not args.include_private:
                    all_tombs = [t for t in all_tombs if not str(t[2]).startswith('private:')]
                tombstones = all_tombs[:20]
            sys.stdout.write(export_mod.render(
                rows, args.title, project, grouped=grouped, tombstones=tombstones))
            return
        # An explicit --out wins; --host picks the conventional filename(s),
        # written under --out-dir so a user can target a repo from anywhere.
        out_path = args.out
        results = export_mod.export(
            conn, project_id, 'agent-' + agent, out_path,
            limit=args.limit, include_private=args.include_private,
            title=args.title, force=args.force, host=args.host,
            out_dir=args.out_dir, grouped=grouped,
            include_tombstones=include_tombstones)
        _print_export_results(results)
    except (core.MemCoreError, store.StoreError, ValueError) as e:
        sys.exit(f'error: {e}')
    except OSError as e:
        # Unwritable target, missing parent, permissions: report, never traceback.
        sys.exit(f'error: {e}')
    finally:
        if conn is not None:
            conn.close()




def _print_export_results(results):
    """Report one export run; exits 1 when any target refused a write."""
    if not isinstance(results, list):
        results = [results]
    refused = 0
    for result in results:
        if result.get('refused'):
            refused += 1
            print(f"refused {result['path']}: {result['refused']}")
        elif result['wrote']:
            print(f"wrote {result['path']} "
                  f"({result['rows']} rows, {result['bytes']} chars)")
        else:
            print(f"{result['path']} already up to date "
                  f"({result['rows']} rows); use --force to rewrite")
    if refused:
        sys.exit(1)




def cmd_stats(args):
    """Operational stats including a content-free ingest health snapshot."""
    conn = _open_readonly(args)
    try:
        stats = core.stats(conn)
        stats['schema_version'] = store._current_version(conn)
        stats['journal'] = ingest.journal_stats(conn)
        stats['autonomy_per_day'] = ingest.autonomy_per_day(conn)
    finally:
        conn.close()
    _out(stats)




def cmd_import(args):
    """Import memories from JSON file with --agent and --project options."""
    conn = None
    try:
        dry_run = getattr(args, 'dry_run', False)
        conn = (store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
                if dry_run else _open(args))
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                items = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f'error: failed to read import file: {e}')
        
        if not isinstance(items, list):
            sys.exit('error: import file must contain a JSON array of items')
        
        # Operator-run CLI: project identity is never invented during import.
        # Accept exact project IDs/UUIDs or a unique name/slug, matching plugin
        # binding semantics.
        project_id = _project_or_exit(conn, args.project)

        if dry_run:
            _import_dry_run(conn, args, items, project_id)
            conn.close()
            return

        with store.write_tx(conn):
            agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
            if not agent_exists:
                conn.execute(
                    'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                    (agent_id, args.agent, args.agent)
                )
            joined = conn.execute(
                'INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) '
                "VALUES (?, ?, 'member')",
                (project_id, agent_id)
            )
            if joined.rowcount:
                core._audit(conn, 'agent_joined', agent_id, None, project_id,
                            {'source': 'memcore import CLI'})

        result = core.import_memories(conn, items, project_id, agent_id, scope=args.scope)
        conn.close()
        _print_import_result(result)
    except (core.MemCoreError, store.StoreError) as e:
        sys.exit(f'error: {e}')
    except FileNotFoundError:
        sys.exit(f'error: file not found: {args.file}')
    except json.JSONDecodeError as e:
        sys.exit(f'error: invalid JSON: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass




def _import_dry_run(conn, args, items, project_id):
    """Preview one import: classification + identity state, zero writes."""
    agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
    plan = core.plan_import(
        conn, items, project_id, scope=args.scope, agent_id=agent_id
    )
    member_exists = bool(conn.execute(
        'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
        (project_id, agent_id)
    ).fetchone())
    print('import dry-run:')
    print(f'  total: {plan["total"]}')
    print(f'  would add: {plan["would_add"]}')
    print(f'  skipped: {plan["skipped"]}')
    for reason, count in sorted(plan['reasons'].items()):
        print(f'    {reason}: {count}')
    print(f'  agent: {"existing" if agent_exists else "would create"}')
    print(f'  membership: {"existing" if member_exists else "would join"}')
    print('  writes performed: 0')




def _print_import_result(result):
    """Render one completed import's result counters."""
    print('import complete:')
    print(f'  added: {result["added"]}')
    print(f'  skipped: {result["skipped"]}')
    if result['created']:
        print('  created memories:')
        for mem_id, ver_id in result['created']:
            print(f'    {mem_id} (version {ver_id})')




def cmd_backup(args):
    """Create a verified recovery snapshot of the store."""
    db_path = getattr(args, 'db', DEFAULT_DB)
    try:
        dest = store.backup_store(db_path, keep=args.keep)
    except store.StoreError as e:
        sys.exit(f'error: {e}')
    size_kb = dest.stat().st_size // 1024
    print(f'backup created: {dest} ({size_kb} KB)')
    print(f'integrity_check: ok')
    report = store.verify_backups(db_path)
    print(f'snapshots retained: {report["snapshot_count"]}')
    print(f'recovery_ready: {report["recovery_ready"]}')
    if report['problems']:
        print(f'  remaining problems: {", ".join(report["problems"])}')
        print(f'  hint: run \'memcore backup\' again after '
              f'{store.BACKUP_MAX_AGE_DAYS} more days to clear staleness, '
              f'or repeat until {store.BACKUP_MIN_COUNT} snapshots exist.')




def cmd_backup_status(args):
    """Report recovery readiness without creating a snapshot."""
    db_path = getattr(args, 'db', DEFAULT_DB)
    _out(store.verify_backups(
        db_path,
        max_age_days=args.max_age_days,
        min_count=args.min_count,
    ))




def cmd_restore_backup(args):
    """Restore the store from a snapshot file. Requires --confirm.

    The current store is preserved next to itself before being replaced, so an
    accidental restore is itself recoverable.
    """
    db_path = pathlib.Path(getattr(args, 'db', DEFAULT_DB)).expanduser()
    snapshot = pathlib.Path(args.snapshot).expanduser()
    if not snapshot.is_file():
        sys.exit(f'error: snapshot does not exist: {snapshot}')

    check = sqlite3.connect(str(snapshot), timeout=10)
    try:
        row = check.execute('PRAGMA integrity_check').fetchone()
        if not row or row[0] != 'ok':
            sys.exit(f'error: snapshot failed integrity check: '
                     f'{row[0] if row else "no result"}')
        counts = {
            'memories': check.execute('SELECT COUNT(*) FROM memory').fetchone()[0],
            'journal_events': check.execute(
                'SELECT COUNT(*) FROM ingest_event').fetchone()[0],
        }
    except sqlite3.DatabaseError as e:
        sys.exit(f'error: snapshot is unreadable: {e}')
    finally:
        check.close()

    if not args.confirm:
        print('restore is a destructive operation. Re-run with --confirm.')
        print(f'  target   : {db_path}')
        print(f'  snapshot : {snapshot}')
        print(f'  contents : {counts["memories"]} memories, '
              f'{counts["journal_events"]} journal events')
        print('  the existing store is preserved as <name>.pre-restore-<ts>.bak')
        return

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if db_path.is_file():
        _assert_store_not_in_use(db_path)
        preserved = db_path.with_name(f'{db_path.name}.pre-restore-{stamp}.bak')
        shutil.copy2(db_path, preserved)
        print(f'preserved current store: {preserved}')

    _remove_stale_sidecars(db_path)

    # H8: atomic replace via a same-directory temp file — a crash or full
    # disk mid-copy must leave the existing store untouched, never truncated.
    tmp_path = db_path.with_name(db_path.name + f'.tmp.{os.getpid()}')
    try:
        shutil.copy2(snapshot, tmp_path)
        os.replace(tmp_path, db_path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    print(f'restored: {snapshot} -> {db_path}')
    print(f'  memories={counts["memories"]} '
          f'journal_events={counts["journal_events"]}')
    print("run 'memcore doctor' to verify the restored store.")




def _assert_store_not_in_use(db_path):
    """Fail loudly when a live connection still holds the target store.

    Probes for an exclusive/shared lock or a WAL index on the database in a
    way that works identically on Windows and POSIX.
    """
    try:
        target_check = sqlite3.connect(str(db_path), timeout=0.1)
        try:
            target_check.execute('PRAGMA busy_timeout = 100')
            target_check.execute('PRAGMA locking_mode = EXCLUSIVE')
            target_check.execute('BEGIN EXCLUSIVE')
            target_check.execute('COMMIT')
        finally:
            target_check.close()
    except sqlite3.OperationalError:
        sys.exit(
            f'error: cannot restore — store at {db_path} is in use by a live connection. '
            f'Close every process using this store, then retry.'
        )




def _remove_stale_sidecars(db_path):
    """Delete -wal/-shm sidecars of the file being replaced.

    Sidecars belong to the file being replaced; keeping them would graft
    another database's WAL frames onto the restored image. A sidecar held by
    another live connection cannot be deleted, so fail loudly instead of
    silently producing a store whose WAL belongs to a different database.
    """
    for ext in ('-wal', '-shm'):
        stale = db_path.with_name(db_path.name + ext)
        if not stale.exists():
            continue
        try:
            stale.unlink()
        except PermissionError:
            sys.exit(
                f'error: cannot remove {stale.name} — it is in use. '
                f'Close every process using this store, then retry.'
            )




def cmd_embed(args):
    conn = _open(args)
    provider = getattr(args, 'provider', None)
    model = getattr(args, 'model', None)
    endpoint = getattr(args, 'endpoint', None)
    api_key = getattr(args, 'api_key', None)
    limit = None if getattr(args, 'all_memories', False) else getattr(args, 'limit', 100)
    dry_run = getattr(args, 'dry_run', False)

    target_endpoint, target_model, _ = embedding.resolve_config(
        provider=provider, endpoint=endpoint, model=model, api_key=api_key
    )
    if not target_endpoint:
        print('Embedding is disabled or no valid endpoint configured.')
        conn.close()
        return

    query = (
        'SELECT m.id, v.id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'LEFT JOIN memory_embedding e ON e.version_id = v.id AND e.memory_id = m.id '
        "WHERE e.vector IS NULL AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        'ORDER BY m.created_at DESC'
    )
    params = ()
    if limit is not None:
        query += ' LIMIT ?'
        params += (limit,)

    rows = conn.execute(query, params).fetchall()
    if dry_run:
        p_label = provider or 'default'
        print(f'Dry run: {len(rows)} memories need embedding (provider: {p_label}, model: {target_model})')
        conn.close()
        return

    if not rows:
        print('All memories are already embedded.')
        conn.close()
        return

    chunk_size = 20
    total_embedded = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i + chunk_size]
        texts = [r[2] for r in chunk]
        vectors = embedding.get_embeddings_batch(
            texts,
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            provider=provider,
        )
        if not vectors or len(vectors) != len(chunk):
            print(f'Failed to fetch embeddings from {target_endpoint}')
            break
        for (mid, vid, _), vec in zip(chunk, vectors):
            store.store_embedding(conn, mid, vid, target_model, vec)
            total_embedded += 1
        conn.commit()

    print(f'Embedded {total_embedded} memories (model: {target_model})')
    conn.close()
