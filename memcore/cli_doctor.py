"""MemCore CLI — `doctor` health report: integrity, bindings, deployment."""
import os
import pathlib
import sys

from . import core
from . import ingest
from . import store
from .cli_support import DEFAULT_DB
from .cli_support import _discover_hermes_memcore_bindings
from .cli_support import _is_network_path
from .cli_support import _resolve_project_ref





# ── doctor ─────────────────────────────────────────────────────────────

def _plugin_deployment_report() -> dict:
    """Compare the deployed Hermes plugin runtime against the Git source.

    Reuses scripts/deploy_hermes_plugin.py so the runtime file allowlist has
    exactly one definition. doctor stays healthy when the script cannot be
    imported (doctor must not depend on repo layout).
    """
    try:
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        deploy = _load_deploy_module(repo_root / 'scripts' / 'deploy_hermes_plugin.py')
        target = deploy.default_target()
        plan = deploy.deployment_plan(target)
    except Exception as exc:  # noqa: BLE001 - doctor must not crash on this check
        return {'available': False, 'error': str(exc)}

    if not target.is_dir():
        return {
            'available': True,
            'target': str(target),
            'missing_plugin': True,
            'files': plan,
            'out_of_sync': None,
        }
    changed = [item for item in plan if item['state'] != 'same']
    return {
        'available': True,
        'target': str(target),
        'missing_plugin': False,
        'files': plan,
        'out_of_sync': changed or None,
    }




def _load_deploy_module(path: pathlib.Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('memcore_deploy_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module




def cmd_doctor(args):
    """Integrity + drift checks. Thin adapter over the two deep health seams."""
    conn = None
    try:
        conn = store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
    except store.StoreError as e:
        sys.exit(f'error: {e}')
    db_path = pathlib.Path(getattr(args, 'db', DEFAULT_DB)).expanduser()
    skip_binding = bool(getattr(args, 'skip_binding_check', False))
    report = {}
    report.update(store.inspect_storage_health(
        conn, getattr(args, 'db', DEFAULT_DB)))
    report.update(core.inspect_governance_health(conn))
    # Journal health is content-free. Review backlog is informational;
    # failed processing is an actual health failure.
    report['journal'] = ingest.journal_stats(conn)
    # Journal age: whole days the oldest pending row has waited. Informational
    # only - a cold queue reports None, never a health failure.
    report['journal_age'] = ingest.journal_age(conn)
    report['network_path'] = _is_network_path(db_path)
    report['store_parent_writable'] = os.access(db_path.parent, os.W_OK)
    config_check, binding_drift = _check_profile_binding_drift(
        conn, db_path, skip_binding=skip_binding)
    report['config_check'] = config_check
    report['binding_drift'] = binding_drift
    conn.close()

    # Deployed Hermes plugin runtime must match the Git source of truth.
    report['plugin_deployment'] = _plugin_deployment_report()
    _print_doctor_report(report, skip_binding=skip_binding)
    if _doctor_unhealthy(report, skip_binding=skip_binding):
        sys.exit(1)




def _check_profile_binding_drift(conn, db_path, skip_binding=False):
    """(config_check, binding_drift): enabled bindings must resolve here."""
    config_check = _discover_hermes_memcore_bindings()
    drift = []
    if config_check.get('available') and not skip_binding:
        current_db = db_path.resolve(strict=False)
        for binding in config_check.get('bindings', []):
            reasons = _binding_drift_reasons(conn, binding, current_db)
            if reasons:
                drift.append({
                    'profile': binding['profile'],
                    'agent': binding.get('agent'),
                    'projects': binding.get('projects') or [],
                    'reasons': reasons,
                })
    return config_check, drift




def _binding_drift_reasons(conn, binding, current_db):
    """Content-free reasons one enabled binding does not resolve into this store."""
    reasons = []
    configured_db = pathlib.Path(binding['store_path']).expanduser().resolve(strict=False)
    if configured_db != current_db:
        reasons.append(f'store_mismatch:{configured_db}')
    agent = binding.get('agent')
    projects = binding.get('projects') or []
    if not agent:
        reasons.append('missing_agent_identity')
    if not projects:
        reasons.append('missing_project_binding')
    for project in projects:
        pid, project_error = _resolve_project_ref(conn, project)
        if project_error:
            reasons.append(project_error)
            continue
        if agent:
            aid = f'agent-{agent}'
            if not conn.execute(
                'SELECT 1 FROM project_membership '
                'WHERE project_id=? AND agent_id=?', (pid, aid)
            ).fetchone():
                reasons.append(f'missing_membership:{agent}->{project}')
    return reasons




def _print_doctor_integrity(report):
    """Integrity + identity sections of the doctor report."""
    print(f"integrity: {report['integrity_check']}")
    print(f"foreign-key violations: {report['foreign_key_violations']}")
    print(f"journal mode: {report['journal_mode']}")
    print(f"wal file: present={report['wal_file']['exists']}")
    print(f"orphaned versions: {report['orphaned_memory_versions']}")
    print(f"orphaned audit: {report['orphaned_audit_events']}")
    print(f"idempotency violations: {report['idempotency_violations']}")
    print(
        f"current-version ownership violations: "
        f"{report['current_version_ownership_violations'] or 'none'}"
    )
    print(
        f"memory fingerprint violations: "
        f"{report['memory_fingerprint_violations'] or 'none'}"
    )
    print(f"tombstones: {report['tombstones']['active']} active, "
          f"{report['tombstones']['overridden']} overridden")
    print(
        f"unguarded rejected memories: "
        f"{report['unguarded_rejected_memories'] or 'none'}"
    )
    print(f"tombstone violations: {report['tombstone_violations'] or 'none'}")
    print('memberships:')
    for name, agent, role in report['memberships']:
        print(f'  {name}: {agent} ({role})')
    print(f"agent name collisions: {report['agent_name_collisions'] or 'none'}")
    print(f"project name collisions: {report['project_name_collisions'] or 'none'}")




def _print_doctor_bindings(report, skip_binding):
    """Config-binding drift + path sections of the doctor report."""
    if skip_binding:
        print('config bindings: skipped (--skip-binding-check)')
    elif report['config_check'].get('available'):
        print('config bindings:')
        drift_by_profile = {
            item['profile']: item for item in report['binding_drift']
        }
        for binding in report['config_check'].get('bindings', []):
            drift = drift_by_profile.get(binding['profile'])
            if drift:
                print(f"  {binding['profile']}: DRIFT - {', '.join(drift['reasons'])}")
            else:
                print(f"  {binding['profile']}: OK")
        for err in report['config_check'].get('errors', []):
            print(f'  config error: {err}')
    else:
        print(f"config bindings: unavailable ({report['config_check'].get('error')})")
    print(f"network path: {report['network_path']}")
    print(f"store parent writable: {report['store_parent_writable']}")




def _print_doctor_health(report):
    """FTS / corroboration / provenance / backup / journal / deploy sections."""
    print(f"fts index: in_sync={report['fts_index']['in_sync']}")
    corrob = report['corroboration']
    print(
        f"corroboration: {corrob['fingerprints']} fingerprint(s) "
        f"(at_1={corrob['at_1']} at_2={corrob['at_2']} "
        f"at_accept>={corrob['accept_n']}={corrob['at_accept']} "
        f"at_golden>={corrob['golden_n']}={corrob['at_golden']})"
    )
    prov = report['provenance']
    print(
        f"provenance: {prov['checked']} checked "
        f"(sealed={prov['sealed']} valid={prov['valid']} "
        f"invalid={prov['invalid']} unsealed={prov['unsealed']})"
    )
    if prov['invalid']:
        print(f"  INVALID seals: {', '.join(prov['invalid_ids'])}")
        print("  hint: attribution fields were altered after write. "
              "Investigate before trusting these events.")
    if not corrob['reachable']:
        print('  hint: no claim has enough independent sources to promote. '
              "use 'memcore corroborate --project <p> --agent <a>' to inspect.")
    backup_report = report['backups']
    count = backup_report['snapshot_count']
    age = backup_report['newest_age_days']
    print(
        f"backups: {count} snapshot(s), "
        f"newest {age if age is not None else 'n/a'}d old, "
        f"recovery_ready={backup_report['recovery_ready']}"
    )
    if backup_report['problems']:
        print(f"  backup problems: {', '.join(backup_report['problems'])}")
        print(f"  hint: run 'memcore backup' to create a recovery point.")
    print(f"migration locks: {report['migration_locks']}")
    print(
        "journal: "
        f"health={report['journal']['health']}, "
        f"events={report['journal']['total_events']}, "
        f"semantic_pending={report['journal']['semantic_review_pending']}, "
        f"unresolved_builtin={report['journal']['unresolved_builtin_mutations']}, "
        f"failed={report['journal']['by_status'].get('failed', 0)}"
    )
    oldest_days = report['journal_age']['oldest_pending_days']
    print(f"journal age: oldest_pending={'n/a' if oldest_days is None else f'{oldest_days}d'}")
    if report['journal']['unresolved_builtin_mutations'] > 0:
        print("  hint: pending unresolved built-in mutations detected. "
              "Run 'memcore journal-stats' to inspect and "
              "'memcore journal-dismiss <id> --agent <agent>' to resolve.")
    deploy = report['plugin_deployment']
    if not deploy.get('available'):
        print(f"plugin deploy check: unavailable ({deploy.get('error')})")
    elif deploy.get('missing_plugin'):
        print(f"plugin deploy check: NOT INSTALLED ({deploy['target']})")
    else:
        drift = deploy.get('out_of_sync')
        if drift:
            names = ', '.join(item['relative'] for item in drift)
            print(f"plugin deploy check: OUT OF SYNC - {names}")
        else:
            print(f"plugin deploy check: OK ({deploy['target']})")




def _print_doctor_report(report, skip_binding=False):
    """Render the doctor report (adapter only: no checks live here)."""
    _print_doctor_integrity(report)
    _print_doctor_bindings(report, skip_binding)
    _print_doctor_health(report)




def _doctor_unhealthy(report, skip_binding=False) -> bool:
    """Gate for the doctor exit status; content-free boolean roll-up."""
    if not skip_binding:
        if report['binding_drift']:
            return True
        if report['config_check'].get('errors'):
            return True
    deploy = report['plugin_deployment']
    red_flags = (
        report['integrity_check'] != 'ok',
        report['foreign_key_violations'] > 0,
        report['journal_mode'] != 'wal',
        report['orphaned_memory_versions'] > 0,
        report['orphaned_audit_events'] > 0,
        report['idempotency_violations'] > 0,
        bool(report['current_version_ownership_violations']),
        bool(report['memory_fingerprint_violations']),
        bool(report['unguarded_rejected_memories']),
        bool(report['tombstone_violations']),
        bool(report['agent_name_collisions']),
        bool(report['project_name_collisions']),
        not report['store_parent_writable'],
        not report['fts_index']['in_sync'],
        not report['backups']['recovery_ready'],
        report['provenance']['invalid'] > 0,
        report['migration_locks'] != 'none',
        report['journal']['by_status'].get('failed', 0) > 0,
        bool(deploy.get('missing_plugin')),
        bool(deploy.get('out_of_sync')),
    )
    return any(red_flags)
