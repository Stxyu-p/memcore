"""MemCore CLI — shared plumbing: store handles, project/agent resolution, Hermes bindings."""
import json
import os
import pathlib
import sys

from . import mcp_server, store

from .mcp_install import ServerSpec




def _memcore_server_spec() -> ServerSpec:
    return ServerSpec(name="memcore", module="memcore.mcp_server")



def _sync_memcore_antigravity_schema() -> None:
    try:
        ag_mcp = pathlib.Path.home() / ".gemini" / "antigravity-cli" / "mcp"
        if not ag_mcp.parent.is_dir():
            return
        target = ag_mcp / "memcore"
        target.mkdir(parents=True, exist_ok=True)
        for tool in mcp_server.TOOLS:
            schema = {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            }
            (target / f"{tool['name']}.json").write_text(
                json.dumps(schema, ensure_ascii=False), encoding="utf-8"
            )
        if getattr(mcp_server, "SERVER_INSTRUCTIONS", None):
            (target / "instructions.md").write_text(
                mcp_server.SERVER_INSTRUCTIONS.strip() + "\n", encoding="utf-8"
            )
    except Exception:
        pass



def _sync_memcore_global_skill() -> None:
    skill_md = (
        "---\n"
        "name: memcore\n"
        "description: >-\n"
        "  Governed, bitemporal, local-first memory engine (MemCore) for storing, searching,\n"
        "  promoting, superseding, and auditing canonical project facts and architectural decisions across agent sessions.\n"
        "---\n\n"
        "# MemCore — Governed Bitemporal Memory\n\n"
        "MemCore provides governed project memory across agent sessions with 9 MCP tools:\n"
        "`memory_remember`, `memory_search`, `memory_promote`, `memory_supersede`, `memory_reject`,\n"
        "`memory_feedback`, `memory_review_queue`, `memory_review_decide`, and `memory_status`.\n"
        "\n"
        "**Surface note:** those nine are the **MCP server** tools (Codex CLI, Claude Code, Antigravity / Gemini CLI). "
        "In **Hermes**, memcore is the native memory provider (`memory.provider: memcore`), not an MCP server — eight "
        "tools are exposed natively under the same names (`memory_remember` … `memory_review_decide`), with no "
        "`memory_get` and no `memory_status`.\n"
    )
    for root in (
        pathlib.Path.home() / ".skill-proof" / "skills",
        pathlib.Path.home() / ".agents" / "skills",
        pathlib.Path.home() / ".gemini" / "config" / "skills",
    ):
        try:
            if root.is_dir():
                d = root / "memcore"
                d.mkdir(parents=True, exist_ok=True)
                target = d / "SKILL.md"
                if not target.exists():
                    target.write_text(skill_md, encoding="utf-8")
        except Exception:
            pass



DEFAULT_DB = str(pathlib.Path.home() / '.memcore' / 'memory.db')



def _configure_stdio_utf8():
    """Keep CLI output Unicode-safe on Windows legacy console encodings."""
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (OSError, ValueError):
                pass



def _open(args):
    return store.open_store(getattr(args, 'db', DEFAULT_DB))



def _open_readonly(args):
    """Open an existing current-schema store without creating or migrating it."""
    try:
        return store.open_store_readonly(getattr(args, 'db', DEFAULT_DB))
    except store.StoreError as e:
        sys.exit(f'error: {e}')



def _open_existing(args):
    """Open an existing writable store, allowing migrations but never creating it."""
    db_path = pathlib.Path(getattr(args, 'db', DEFAULT_DB)).expanduser()
    if not db_path.is_file():
        sys.exit(f'error: store does not exist: {db_path.resolve(strict=False)}')
    try:
        return store.open_store(str(db_path))
    except store.StoreError as e:
        sys.exit(f'error: {e}')



def _out(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))



def _is_network_path(path):
    raw = str(path)
    if raw.startswith('\\\\') or raw.startswith('//'):
        return True
    if os.name == 'nt':
        try:
            import ctypes
            anchor = pathlib.Path(path).anchor
            if anchor:
                return ctypes.windll.kernel32.GetDriveTypeW(anchor) == 4
        except Exception:
            pass
    return False



def _hermes_roots():
    """Candidate Hermes home roots, most-specific first (env > LOCALAPPDATA > ~)."""
    roots = []
    env_home = os.environ.get('HERMES_HOME')
    if env_home:
        roots.append(pathlib.Path(env_home).expanduser())
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(pathlib.Path(local) / 'hermes')
    roots.append(pathlib.Path.home() / '.hermes')
    return roots



def _hermes_root_with_config():
    """First candidate root that actually holds a config.yaml, else None."""
    for root in _hermes_roots():
        if (root / 'config.yaml').is_file():
            return root
    return None



def _hermes_profile_config_files(root):
    """(profile, config path) pairs: default plus every profile with a config."""
    files = [('default', root / 'config.yaml')]
    profiles = root / 'profiles'
    if profiles.is_dir():
        files.extend(
            (p.name, p / 'config.yaml') for p in profiles.iterdir()
            if p.is_dir() and (p / 'config.yaml').is_file()
        )
    return files



def _enabled_memcore_settings(cfg):
    """The memcore settings dict when the plugin is enabled, else None."""
    plugins = cfg.get('plugins') or {}
    if 'memcore' not in (plugins.get('enabled') or []):
        return None
    entry = (plugins.get('entries') or {}).get('memcore') or {}
    return entry.get('settings') or {}



def _binding_from_settings(profile, cfg_path, settings):
    """One normalized Hermes -> MemCore binding record."""
    agent = settings.get('agent_name') or (profile if profile != 'default' else None)
    projects = []
    default_project = settings.get('default_project')
    if isinstance(default_project, str) and default_project.strip():
        projects.append(default_project.strip())
    for binding in settings.get('path_bindings') or []:
        if isinstance(binding, dict):
            project = binding.get('project')
            if isinstance(project, str) and project.strip():
                projects.append(project.strip())
    projects = list(dict.fromkeys(projects))
    return {
        'profile': profile,
        'config': str(cfg_path),
        'agent': agent,
        'projects': projects,
        'store_path': str(settings.get('store_path') or DEFAULT_DB),
    }



def _discover_hermes_memcore_bindings():
    """Best-effort read of enabled MemCore bindings from Hermes YAML."""
    try:
        import yaml
    except Exception as exc:
        return {'available': False, 'error': f'PyYAML unavailable: {exc}', 'bindings': []}

    root = _hermes_root_with_config()
    if root is None:
        return {'available': False, 'error': 'Hermes config.yaml not found', 'bindings': []}

    bindings = []
    errors = []
    for profile, cfg_path in _hermes_profile_config_files(root):
        try:
            data = yaml.safe_load(cfg_path.read_text(encoding='utf-8')) or {}
            settings = _enabled_memcore_settings(data)
            if settings is None:
                continue
            bindings.append(_binding_from_settings(profile, cfg_path, settings))
        except Exception as exc:
            errors.append(f'{cfg_path}: {type(exc).__name__}: {exc}')
    return {'available': True, 'root': str(root), 'bindings': bindings, 'errors': errors}



def _resolve_project_ref(conn, project_ref):
    """Resolve exact project id/UUID or a unique project name/slug."""
    if not project_ref or not isinstance(project_ref, str):
        return None, f'missing_project:{project_ref}'
    direct = conn.execute(
        'SELECT id FROM project WHERE id=?', (project_ref,)
    ).fetchone()
    if direct:
        return direct[0], None
    by_name = conn.execute(
        'SELECT id FROM project WHERE name=? ORDER BY id', (project_ref,)
    ).fetchall()
    if len(by_name) > 1:
        return None, f'ambiguous_project:{project_ref}'
    if by_name:
        return by_name[0][0], None
    legacy = f'proj-{project_ref}'
    row = conn.execute('SELECT id FROM project WHERE id=?', (legacy,)).fetchone()
    return (row[0], None) if row else (None, f'missing_project:{project_ref}')



def _project_or_exit(conn, project_ref):
    pid, error = _resolve_project_ref(conn, project_ref)
    if error:
        sys.exit(f'error: {error}')
    return pid



def _agent_identity_or_exit(conn, agent_name):
    """Return (agent_id, exists) only if deterministic identity is unambiguous."""
    if not agent_name or not isinstance(agent_name, str):
        sys.exit('error: agent name is required and must be a non-empty string')
    aid = f'agent-{agent_name}'
    by_id = conn.execute(
        'SELECT name, profile_key FROM agent WHERE id=?', (aid,)
    ).fetchone()
    if by_id is not None:
        if by_id != (agent_name, agent_name):
            sys.exit(
                f'error: agent id {aid} has different identity '
                f'(name={by_id[0]}, profile_key={by_id[1]})'
            )
        return aid, True
    by_profile = conn.execute(
        'SELECT id, name FROM agent WHERE profile_key=?', (agent_name,)
    ).fetchone()
    if by_profile is not None:
        sys.exit(
            f'error: profile_key {agent_name} already belongs to agent '
            f'{by_profile[0]} ({by_profile[1]})'
        )
    return aid, False
