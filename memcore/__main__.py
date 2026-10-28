"""MemCore — CLI entrypoint: python -m memcore

Handlers live in the cli_* modules; they are re-exported here so that
`from memcore.__main__ import ...` keeps working for callers and tests.
"""
import argparse
import os  # harness patches memcore.__main__.os.replace
import sys  # tests swap cli.sys.stdout/stderr

from . import export as export_mod, mcp_server, store

from .mcp_install import add_install_arguments, run_clients, run_install

from .cli_support import (
    _memcore_server_spec,
    _sync_memcore_antigravity_schema,
    _sync_memcore_global_skill,
    DEFAULT_DB,
    _configure_stdio_utf8,
    _open,
    _open_readonly,
    _open_existing,
    _out,
    _is_network_path,
    _hermes_roots,
    _hermes_root_with_config,
    _hermes_profile_config_files,
    _enabled_memcore_settings,
    _binding_from_settings,
    _discover_hermes_memcore_bindings,
    _resolve_project_ref,
    _project_or_exit,
    _agent_identity_or_exit,
)

from .cli_memory import (
    cmd_remember,
    cmd_search,
    cmd_promote,
    cmd_supersede,
    cmd_deactivate,
    cmd_restore,
    cmd_reject,
    cmd_reject_value,
    cmd_tombstone_list,
    cmd_tombstone_unreject,
    cmd_tombstone_override,
    cmd_contradictions,
    cmd_mark_conflict,
    cmd_history,
    cmd_corroborate,
    cmd_golden_list,
    cmd_decay,
)

from .cli_journal import (
    _journal_scope,
    cmd_journal_stats,
    cmd_journal_review_list,
    cmd_journal_review_decide,
    cmd_journal_analysis_history,
    cmd_journal_dismiss,
    cmd_journal_sweep,
)

from .cli_ops import (
    cmd_init,
    cmd_project_add,
    cmd_project_list,
    cmd_agent_add,
    cmd_member_add,
    cmd_gc,
    _print_gc_scan,
    _apply_gc_and_report,
    _export_config_candidates,
    _default_export_binding,
    cmd_export,
    cmd_compact,
    cmd_export_project,
    cmd_import_project,
    _print_export_results,
    cmd_stats,
    cmd_import,
    _import_dry_run,
    _print_import_result,
    _plugin_deployment_report,
    _load_deploy_module,
    cmd_backup,
    cmd_backup_status,
    cmd_restore_backup,
    _assert_store_not_in_use,
    _remove_stale_sidecars,
    cmd_doctor,
    _check_profile_binding_drift,
    _binding_drift_reasons,
    _print_doctor_integrity,
    _print_doctor_bindings,
    _print_doctor_health,
    _print_doctor_report,
    _doctor_unhealthy,
    cmd_embed,
)






def main(argv=None):
    _configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        prog='memcore',
        description='MemCore — shared project memory core + CLI'
    )
    parser.add_argument('--db', default=DEFAULT_DB, help='store path (default ~/.memcore/memory.db)')
    # --db must be accepted in either position (`memcore --db X doctor` and
    # `memcore doctor --db X`). argparse handles the former natively; the
    # latter needs the flag re-declared on each subparser.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS (not None) so an absent --db leaves no attribute for the
    # subparser to overwrite — that is what made `--db X doctor` silently
    # fall back to DEFAULT_DB and touch the real user store.
    common.add_argument('--db', default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('init', help='create store', parents=[common]).set_defaults(func=cmd_init)

    p = sub.add_parser('project', help='project management', parents=[common])
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add', parents=[common])
    pa.add_argument('name')
    pa.add_argument('--description', default='')
    pa.set_defaults(func=cmd_project_add)
    psub.add_parser('list', parents=[common]).set_defaults(func=cmd_project_list)

    p = sub.add_parser('agent', help='agent management', parents=[common])
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add', parents=[common])
    pa.add_argument('name')
    pa.set_defaults(func=cmd_agent_add)

    p = sub.add_parser('member', help='membership management', parents=[common])
    psub = p.add_subparsers(dest='subcommand', required=True)
    pa = psub.add_parser('add', parents=[common])
    pa.add_argument('project', help='project id/UUID or unique name/slug')
    pa.add_argument('agent')
    pa.add_argument('--role', default='member', choices=['member', 'owner'])
    pa.set_defaults(func=cmd_member_add)

    p = sub.add_parser('remember', help='store a memory', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True)
    p.add_argument('content')
    p.add_argument('--scope', default='private', choices=['project', 'private'])
    p.add_argument('--type', default='fact')
    p.add_argument('--idempotency-key', default=None)
    p.add_argument('--reason', default=None)
    p.add_argument('--when-to-use', default=None, dest='when_to_use',
                   help='optional situational trigger describing when this memory applies')
    p.add_argument('--source-file', default=None,
                   help='file whose SHA-256 will be stored as source provenance')
    p.add_argument('--source-commit', default=None,
                   help='optional repository commit for provenance context')
    p.add_argument('--source-hash', default=None,
                   help='optional SHA-256; must match --source-file')
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser('search', help='FTS5 search over memories', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True)
    p.add_argument('query', nargs='?', default='')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--mode', default='standard', choices=['standard', 'strict', 'historical'],
                   help='recall mode: standard, strict (verified & non-stale only), or historical')
    p.add_argument('--as-of', default=None, dest='as_of',
                   help='optional ISO-8601 timestamp for point-in-time bitemporal query')
    p.add_argument('--changed-since', default=None, dest='changed_since',
                   help='optional ISO-8601 timestamp to return only memories updated since then')
    p.set_defaults(func=cmd_search)

    p = sub.add_parser('promote', help='private -> project scope', parents=[common])
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser('supersede', help='correct a memory (new version)', parents=[common])
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.add_argument('content')
    p.add_argument('--reason', default=None)
    p.add_argument('--when-to-use', default=None, dest='when_to_use',
                   help='optional updated situational trigger')
    p.add_argument('--source-file', default=None,
                   help='file whose SHA-256 will be stored as source provenance')
    p.add_argument('--source-commit', default=None,
                   help='optional repository commit for provenance context')
    p.add_argument('--source-hash', default=None,
                   help='optional SHA-256; must match --source-file')
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser('deactivate', help='soft delete a memory', parents=[common])
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.set_defaults(func=cmd_deactivate)

    p = sub.add_parser('restore', help='restore a disabled memory', parents=[common])
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser('reject', help='reject a memory and create a tombstone', parents=[common])
    p.add_argument('memory_id')
    p.add_argument('--agent', required=True)
    p.add_argument('reason')
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser('reject-value', help='file a project refusal guard for a value (pre-emptive)', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('content')
    p.set_defaults(func=cmd_reject_value)

    p = sub.add_parser('tombstone', help='tombstone management', parents=[common])
    tsub = p.add_subparsers(dest='subcommand', required=True)
    to = tsub.add_parser('override', help='explicitly override an active refusal guard', parents=[common])
    to.add_argument('tombstone_id')
    to.add_argument('--agent', required=True)
    to.set_defaults(func=cmd_tombstone_override)
    tl = tsub.add_parser('list', help='list active refusal guards (content-free)', parents=[common])
    tl.add_argument('--project', required=True,
                    help='project id/UUID or unique name/slug')
    tl.add_argument('--agent', required=True)
    tl.set_defaults(func=cmd_tombstone_list)
    tu = tsub.add_parser('unreject', help='lift a refusal guard by id or fingerprint prefix', parents=[common])
    tu.add_argument('tombstone_ref')
    tu.add_argument('--agent', required=True)
    tu.set_defaults(func=cmd_tombstone_unreject)

    p = sub.add_parser('gc', help='retention sweep (reversible for memories)', parents=[common])
    p.add_argument('--candidate-days', type=int, default=30,
                   help='inactive unevidenced candidates older than N days are disabled (default 30)')
    p.add_argument('--tombstone-days', type=int, default=90,
                   help='overridden tombstones older than N days are purged (default 90)')
    p.add_argument('--journal-days', type=int, default=None,
                   help='processed/ignored journal events older than N days are purged (optional)')
    p.add_argument('--apply', action='store_true',
                   help='perform the sweep (dry-run otherwise)')
    p.set_defaults(func=cmd_gc)

    sub.add_parser('stats', help='operational statistics', parents=[common]).set_defaults(func=cmd_stats)

    p = sub.add_parser('export', help='write governed memory to an agent-readable file',
                       parents=[common])
    p.add_argument('--project', default=None,
                   help='project id/UUID or unique name (default: configured project)')
    p.add_argument('--agent', default=None,
                   help='agent name for private-scope visibility (default: configured agent)')
    p.add_argument('--out', default=None,
                   help='explicit output file (default: the --host convention)')
    p.add_argument('--out-dir', default='.',
                   help="directory to write the --host target(s) into "
                        "(default: current directory)")
    p.add_argument('--host', default=None,
                   choices=sorted(export_mod.HOST_TARGETS) + ['all'],
                   help="write the instruction file this agent reads; "
                        "use 'all' for every distinct supported target")
    p.add_argument('--limit', type=int, default=40,
                   help='max memories to export (default: 40)')
    p.add_argument('--title', default='Shared project memory',
                   help='heading for the exported file')
    p.add_argument('--include-private', action='store_true',
                   help="include this agent's private memories")
    p.add_argument('--grouped', action='store_true',
                   help='group exported memories by category (Golden Rules, Decisions, Facts, Preferences, Notes)')
    p.add_argument('--include-tombstones', action='store_true', dest='include_tombstones',
                   help='include active refusal guards (tombstones) in a Do Not Repeat section')
    p.add_argument('--stdout', action='store_true',
                   help='print instead of writing a file')
    p.add_argument('--force', action='store_true',
                   help='rewrite even when the content is unchanged')
    p.set_defaults(func=cmd_export)

    p = sub.add_parser('compact', help='consolidate accepted historical memories into snapshots',
                       parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name')
    p.add_argument('--agent', default='curator',
                   help='agent identity performing compaction (default: curator)')
    p.add_argument('--days', type=float, default=14.0,
                   help='cutoff age in days for compaction eligibility (default: 14)')
    p.add_argument('--dry-run', action='store_true',
                   help='preview compaction candidates without applying')
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser('export-project', help='export canonical project memories and tombstones to .memcore.jsonl',
                       parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name')
    p.add_argument('--out', default='.memcore.jsonl',
                   help='output JSONL file path (default: .memcore.jsonl)')
    p.set_defaults(func=cmd_export_project)

    p = sub.add_parser('import-project', help='import project memories and tombstones from .memcore.jsonl snapshot',
                       parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name')
    p.add_argument('--agent', default='curator',
                   help='agent identity performing import (default: curator)')
    p.add_argument('--in', dest='in_file', default='.memcore.jsonl',
                   help='input JSONL file path (default: .memcore.jsonl)')
    p.set_defaults(func=cmd_import_project)

    p = sub.add_parser('journal-stats', help='content-free ingest journal health', parents=[common])
    p.add_argument('--project', default=None,
                   help='optional project id/UUID or unique name/slug')
    p.add_argument('--agent', default=None, help='optional agent name')
    p.set_defaults(func=cmd_journal_stats)

    p = sub.add_parser('journal-review-list', help='list pending semantic review events', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True, help='agent name')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--show-content', action='store_true',
                   help='explicitly reveal raw untrusted journal content')
    p.set_defaults(func=cmd_journal_review_list)

    p = sub.add_parser('journal-review-decide', help='remember/ignore/defer one review event', parents=[common])
    p.add_argument('event_id')
    p.add_argument('--agent', required=True, help='event owner agent name')
    p.add_argument('--verdict', required=True, choices=['remember', 'ignore', 'defer'])
    p.add_argument('--content', default='',
                   help='candidate memory content; required for remember')
    p.add_argument('--confidence', type=float, default=None)
    p.add_argument('--rationale', default='')
    p.add_argument('--analyzer', default='memcore-cli')
    p.set_defaults(func=cmd_journal_review_decide)

    p = sub.add_parser('journal-analysis-history', help='show semantic analysis audit history', parents=[common])
    p.add_argument('event_id')
    p.add_argument('--agent', required=True, help='event owner agent name')
    p.set_defaults(func=cmd_journal_analysis_history)

    p = sub.add_parser('journal-dismiss', help='dismiss pending unresolved built-in mutation or event', parents=[common])
    p.add_argument('event_id')
    p.add_argument('--agent', required=True, help='operator agent name')
    p.add_argument('--rationale', default='operator_dismissed', help='reason for dismissal')
    p.set_defaults(func=cmd_journal_dismiss)

    p = sub.add_parser('corroborate', help='scan/apply corroboration promotion (dry-run default)', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True, help='operator agent name')
    p.add_argument('--apply', action='store_true',
                   help='promote eligible claims (dry-run otherwise)')
    p.set_defaults(func=cmd_corroborate)

    p = sub.add_parser('golden-list', help='list the Golden Rule set', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.set_defaults(func=cmd_golden_list)

    p = sub.add_parser('journal-sweep', help='auto-dismiss stale builtin + defer-cap (dry-run default)', parents=[common])
    p.add_argument('--builtin-days', type=int, default=7,
                   help='builtin unresolved older than N days is dismissed')
    p.add_argument('--max-defers', type=int, default=3,
                   help='semantic events deferred N+ times are ignored')
    p.add_argument('--apply', action='store_true',
                   help='perform the sweep (dry-run otherwise)')
    p.set_defaults(func=cmd_journal_sweep)

    p = sub.add_parser('decay', help='freshness decay sweep (dry-run default)', parents=[common])
    p.add_argument('--aging-days', type=int, default=30)
    p.add_argument('--stale-days', type=int, default=90)
    p.add_argument('--apply', action='store_true',
                   help='perform the sweep (dry-run otherwise)')
    p.set_defaults(func=cmd_decay)

    p = sub.add_parser('import', help='import memories from JSON', parents=[common])
    p.add_argument('--file', required=True, help='JSON file path')
    p.add_argument('--agent', required=True, help='agent name')
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--scope', default='project', choices=['project', 'private'],
                   help='scope for imported memories (default project)')
    p.add_argument('--dry-run', action='store_true',
                   help='preview validation/dedup results without writing anything')
    p.set_defaults(func=cmd_import)

    p = sub.add_parser('backup', help='create a verified recovery snapshot', parents=[common])
    p.add_argument('--keep', type=int, default=14,
                   help='snapshots to retain (default 14)')
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser('backup-status', help='report recovery readiness (no writes)', parents=[common])
    p.add_argument('--max-age-days', type=int, default=store.BACKUP_MAX_AGE_DAYS,
                   help=f'newest snapshot older than this is stale '
                        f'(default {store.BACKUP_MAX_AGE_DAYS})')
    p.add_argument('--min-count', type=int, default=store.BACKUP_MIN_COUNT,
                   help=f'snapshots required for recovery_ready '
                        f'(default {store.BACKUP_MIN_COUNT})')
    p.set_defaults(func=cmd_backup_status)

    p = sub.add_parser('restore-from-snapshot', help='restore the store from a snapshot', parents=[common])
    p.add_argument('--snapshot', required=True, help='snapshot file to restore from')
    p.add_argument('--confirm', action='store_true',
                   help='actually perform the restore (preview otherwise)')
    p.set_defaults(func=cmd_restore_backup)

    p = sub.add_parser('contradictions', help='scan for disagreeing claim pairs (read-only)', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--limit', type=int, default=200)
    p.set_defaults(func=cmd_contradictions)

    p = sub.add_parser('mark-conflict', help='mark two memories as conflict (governed)', parents=[common])
    p.add_argument('--project', required=True,
                   help='project id/UUID or unique name/slug')
    p.add_argument('--agent', required=True, help='acting agent name')
    p.add_argument('--memory-a', required=True)
    p.add_argument('--memory-b', required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('--confirm', action='store_true',
                   help='actually perform the marking (preview otherwise)')
    p.set_defaults(func=cmd_mark_conflict)

    p = sub.add_parser('history', help='show the version valid at a timestamp', parents=[common])
    p.add_argument('--agent', required=True, help='reading agent name')
    p.add_argument('memory_id')
    p.add_argument('--as-of', required=True, dest='as_of',
                   help="ISO timestamp, e.g. '2026-09-01T00:00:00Z'")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser('embed', help='backfill or refresh vector embeddings', parents=[common])
    p.add_argument('--limit', type=int, default=100, help='max memories to embed (default 100)')
    p.add_argument('--all', dest='all_memories', action='store_true', help='embed all memories without limit')
    p.add_argument('--provider', choices=['local', 'openai', 'openrouter', 'ollama', 'fastembed', 'none'], default=None, help='embedding provider preset')
    p.add_argument('--model', default=None, help='embedding model name')
    p.add_argument('--endpoint', default=None, help='embedding endpoint URL')
    p.add_argument('--api-key', default=None, help='API key for embedding provider')
    p.add_argument('--dry-run', action='store_true', help='preview without storing vectors')
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser('doctor', help='integrity + drift checks', parents=[common])
    p.add_argument('--skip-binding-check', action='store_true',
                   help='skip Hermes config binding drift checks (useful for test/staging stores)')
    p.set_defaults(func=cmd_doctor)

    def _cmd_install(a):
        if not getattr(a, 'dry_run', False) and not getattr(a, 'print_only', False):
            try:
                if not getattr(a, 'db', None):
                    a.db = DEFAULT_DB
                conn = _open(a)
                try:
                    with store.write_tx(conn):
                        conn.execute(
                            "INSERT OR IGNORE INTO project (id, name, description) VALUES ('proj-default', 'default', 'Default shared project')"
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES ('agent-hermes', 'hermes', 'hermes')"
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) VALUES ('proj-default', 'agent-hermes', 'owner')"
                        )
                finally:
                    conn.close()
                print(f"[MemCore Setup Wizard] Store initialized at {a.db} (WAL + FTS5 + default bindings ready)")
            except Exception:
                pass
        rc = run_install(_memcore_server_spec(), a, remove=False)
        if not getattr(a, 'dry_run', False) and not getattr(a, 'print_only', False):
            _sync_memcore_antigravity_schema()
            _sync_memcore_global_skill()
        sys.exit(rc)

    def _cmd_uninstall(a):
        sys.exit(run_install(_memcore_server_spec(), a, remove=True))

    def _cmd_clients(a):
        sys.exit(run_clients(_memcore_server_spec()))

    def _cmd_mcp(a):
        sys.exit(mcp_server.main())

    p_inst = sub.add_parser('install', help='Run Setup Wizard & auto-register MemCore MCP server across agent CLIs/IDEs', parents=[common])
    add_install_arguments(p_inst)
    p_inst.set_defaults(func=_cmd_install)

    p_setup = sub.add_parser('setup', help='Run full MemCore Setup Wizard (init store, bindings, skills & MCP clients)', parents=[common])
    add_install_arguments(p_setup)
    p_setup.set_defaults(func=_cmd_install)

    p_uninst = sub.add_parser('uninstall', help='Remove MemCore MCP server registration from agent CLIs/IDEs', parents=[common])
    add_install_arguments(p_uninst)
    p_uninst.set_defaults(func=_cmd_uninstall)

    sub.add_parser('clients', help='List supported agent clients and MemCore registration status', parents=[common]).set_defaults(func=_cmd_clients)
    sub.add_parser('mcp', help='Run the stdio MCP server (JSON-RPC 2.0)', parents=[common]).set_defaults(func=_cmd_mcp)

    args = parser.parse_args(argv)
    # --db is accepted before or after the subcommand; the fallback is resolved
    # here, once, because the subparser cannot distinguish "not given" from
    # "given at the top level" without suppressing its own default.
    if not getattr(args, 'db', None):
        args.db = DEFAULT_DB
    args.func(args)


if __name__ == '__main__':
    main()
