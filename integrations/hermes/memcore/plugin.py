"""MemCore agent-half plugin: config binding, hooks, and tools.

Identity comes from config only (ADR-0003): agent_name defaults to the
profile name; the project resolves from path_bindings (longest-prefix
match on cwd) or default_project. Fail-closed: no resolvable project ->
hooks do nothing and tools return a clear config error. Tool schemas
carry no identity/scope parameters.
"""
import importlib.util
import json
import os
import pathlib
import sys
import threading
import uuid
from collections.abc import Mapping

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent

# NOTE: never add _PLUGIN_DIR itself to sys.path. Hermes plugin directories can
# contain names that shadow Hermes packages. Prefer an installed MemCore package,
# then an explicit MEMCORE_SRC checkout, then repository/legacy development roots.
def _ensure_memcore_importable():
    if importlib.util.find_spec('memcore') is not None:
        return
    candidates = []
    env_src = os.environ.get('MEMCORE_SRC')
    if env_src:
        candidates.append(pathlib.Path(env_src).expanduser())
    for parent in _PLUGIN_DIR.parents:
        if (parent / 'memcore' / 'core.py').is_file() and (parent / 'schema' / 'schema.sql').is_file():
            candidates.append(parent)
            break
    candidates.append(pathlib.Path.home() / 'Workspace' / 'memcore')
    for candidate in candidates:
        if (candidate / 'memcore' / 'core.py').is_file():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return


_ensure_memcore_importable()
from memcore import core, store  # noqa: E402


class ConfigError(Exception):
    """No resolvable agent/project binding in config (fail-closed)."""


def resolve_project(cwd, path_bindings, default_project=None):
    """Longest-prefix match of cwd against path_bindings.

    Case-insensitive, separator-normalized. Returns the bound project
    name, else default_project (which may be None).
    """
    if not cwd:
        return default_project

    def norm(s):
        return str(s).replace('\\', '/').rstrip('/').lower()

    cwd_n = norm(cwd)
    best = None
    best_len = -1
    for b in path_bindings or []:
        bpath = norm(b.get('path', ''))
        if not bpath:
            continue
        if cwd_n == bpath or cwd_n.startswith(bpath + '/'):
            if len(bpath) > best_len:
                best = b.get('project')
                best_len = len(bpath)
    return best or default_project


def _memcore_cfg(config):
    """Plugin settings. Official shape: plugins.entries.memcore.settings.*
    (what `hermes config set plugins.entries.memcore.settings.<k>` writes and
    what PluginContext.get_config / the loader validate). The flat
    plugins.memcore shape is accepted for backward compatibility with the
    engine test harness.
    """
    if not isinstance(config, dict):
        return {}
    entries = ((config.get('plugins') or {}).get('entries') or {})
    entry = entries.get('memcore')
    if isinstance(entry, Mapping):
        settings = entry.get('settings')
        if isinstance(settings, Mapping):
            merged = dict(settings)
            # Entry-level keys (e.g. enabled, allow_tool_override) stay out of
            # the settings namespace; nothing to merge beyond settings today.
            return merged
    plugins = config.get('plugins') or {}
    cfg = plugins.get('memcore')
    return cfg if isinstance(cfg, Mapping) else {}


def _binding_cwd():
    """Resolve the logical Hermes session cwd, not the process-global cwd."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:
        return os.getcwd()


def binding_from_config(config, profile_name=''):
    cfg = _memcore_cfg(config)
    agent_name = cfg.get('agent_name') or profile_name or ''
    project = resolve_project(
        _binding_cwd(),
        cfg.get('path_bindings'),
        cfg.get('default_project'),
    )
    return agent_name, project


def require_binding(config, profile_name=''):
    agent_name, project = binding_from_config(config, profile_name)
    if not agent_name or not project:
        raise ConfigError(
            'memcore: no identity/project binding. Set '
            'plugins.entries.memcore.settings (agent_name, default_project '
            'or path_bindings) in config.yaml.'
        )
    return agent_name, project


def default_store_path(config):
    cfg = _memcore_cfg(config)
    raw = cfg.get('store_path') or str(pathlib.Path.home() / '.memcore' / 'memory.db')
    return str(pathlib.Path(raw).expanduser())


# -- Recall block builder (pure, unit-tested) -------------------------------

def build_recall_block(pinned_rows, search_rows, budget_chars=1200, max_items=8):
    """Deterministic, budget-capped recall block. Empty rows -> ''.

    Pinned/critical rows first, then search hits (deduped by memory id).
    One line per item: "- [scope] content". Whole block capped at
    budget_chars (header included). Oversized items are skipped, never clipped:
    a missing condition or negation can reverse a memory's meaning.
    """
    seen = set()
    rows = []
    for r in list(pinned_rows or []) + list(search_rows or []):
        if r[0] not in seen:
            seen.add(r[0])
            rows.append(r)
    lines = []
    header = 'Shared project memory (memcore):'
    budget_chars = max(0, int(budget_chars))
    prefix_len = len(header) + 1  # header + first newline
    if budget_chars <= prefix_len:
        return ''
    used = prefix_len
    for r in rows:
        if len(lines) >= max_items:
            break
        content = ' '.join(str(r[5] if len(r) > 5 else r).split())
        scope = r[1] if len(r) > 1 else '?'
        lifecycle = r[2] if len(r) > 2 else '?'
        verification = r[3] if len(r) > 3 else '?'
        freshness = r[4] if len(r) > 4 else '?'
        line = '- [%s | %s | %s | %s] %s' % (
            scope, lifecycle, verification, freshness, content
        )
        separator = 1 if lines else 0
        remaining = budget_chars - used - separator
        if remaining <= 0:
            break
        if len(line) > remaining:
            continue
        lines.append(line)
        used += separator + len(line)
    if not lines:
        return ''
    return header + '\n' + '\n'.join(lines)


def _is_trivial_query(query):
    """True when a query carries no durable intent (greeting/ack).

    Reuses the ingest classifier's trivial definition so recall and the
    journal agree on what has no signal. Fail-open: any classifier
    problem means inject as before, never skip.
    """
    try:
        from memcore import ingest as _ingest
    except Exception:
        return False
    try:
        _status, decision, _candidate = _ingest.classify_user_text(query)
    except Exception:
        return False
    return decision == 'trivial'


# -- Store access ------------------------------------------------------------

_connections = {}
_connections_lock = threading.RLock()


def _get_conn(store_path):
    """Return one cached SQLite connection per live worker thread/store path."""
    path = str(pathlib.Path(store_path).expanduser())
    thread = threading.current_thread()
    tid = threading.get_ident()
    with _connections_lock:
        # Evict dead worker handles on every access. Keeping even one dead
        # connection can retain an unfinished transaction and lock the store.
        # Owner-object identity also protects against OS/Python thread-id reuse.
        for key, (owner, _p, stale_conn) in list(_connections.items()):
            if owner is not thread and not owner.is_alive():
                _connections.pop(key, None)
                try:
                    stale_conn.close()
                except Exception:
                    pass
        entry = _connections.get(tid)
        if entry is not None:
            owner, current_path, conn = entry
            if owner is thread and current_path == path:
                return conn
            # A recycled thread id or path change must never inherit another
            # worker's SQLite handle/transaction state.
            _connections.pop(tid, None)
            try:
                conn.close()
            except Exception:
                pass
        # Per-thread connections avoid transaction interleaving. Disable the
        # sqlite thread-affinity guard only so reset_conn can close all cached
        # handles during plugin reload/tests, including handles from workers.
        conn = store.open_store(path, check_same_thread=False)
        _connections[tid] = (thread, path, conn)
        return conn


def reset_conn():
    """Close every cached plugin connection (tests/reload hook)."""
    with _connections_lock:
        entries = list(_connections.values())
        _connections.clear()
    for _owner, _path, conn in entries:
        try:
            conn.close()
        except Exception:
            pass


# -- Tools (schemas carry NO identity/scope params; config is the source) ----

def _tool_error(msg):
    return json.dumps({'success': False, 'error': msg})


def _tool_ok(**kw):
    return json.dumps(dict(success=True, **kw))


def _open_tool_store(config):
    """Open the configured store; None means only that the file is absent.

    Existing-but-unopenable stores must raise so tools can distinguish schema,
    permission, and corruption failures from a genuinely missing database.
    """
    path = default_store_path(config)
    db = pathlib.Path(path).expanduser()
    if not db.exists():
        return None
    return _get_conn(str(db))


def _tool_store_or_error(config):
    try:
        conn = _open_tool_store(config)
    except Exception as e:
        return None, _tool_error(
            'store open failed: %s: %s' % (type(e).__name__, e)
        )
    if conn is None:
        return None, _tool_error(
            'store not found; run: python -m memcore --db <store> init'
        )
    return conn, None


def _resolve_project_id(conn, project_ref):
    """Resolve an exact project id/UUID or a unique project name/slug."""
    direct = conn.execute(
        'SELECT id FROM project WHERE id = ?', (project_ref,)
    ).fetchone()
    if direct:
        return direct[0]

    by_name = conn.execute(
        'SELECT id FROM project WHERE name = ? ORDER BY id', (project_ref,)
    ).fetchall()
    if len(by_name) > 1:
        raise ConfigError('project name %s is ambiguous in store' % project_ref)
    if by_name:
        return by_name[0][0]

    # Backward compatibility with the Phase-1 CLI's proj-<slug> ids, even if
    # a hand-built store omitted the matching project.name value.
    legacy = 'proj-' + project_ref
    row = conn.execute('SELECT id FROM project WHERE id = ?', (legacy,)).fetchone()
    return row[0] if row else None


def _require_bound_membership(conn, project_name, agent_name):
    pid = _resolve_project_id(conn, project_name)
    if pid is None:
        raise ConfigError('project %s does not exist in store' % project_name)
    aid = 'agent-' + agent_name
    row = conn.execute(
        'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
        (pid, aid)).fetchone()
    if not row:
        raise ConfigError('agent %s is not a member of project %s' % (agent_name, project_name))
    return pid, aid


def _require_bound_memory(conn, project_name, agent_name, memory_id):
    pid, aid = _require_bound_membership(conn, project_name, agent_name)
    row = conn.execute(
        'SELECT 1 FROM memory WHERE id=? AND project_id=?', (memory_id, pid)
    ).fetchone()
    if not row:
        raise core.PermissionDenied(
            'memory is not in the project bound to this profile'
        )
    return pid, aid


def tool_memory_remember(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    content = (args.get('content') or '').strip()
    if not content:
        return _tool_error('content is required')
    # ADR-0003: explicit tool writes are always shared project memory.
    # The model never chooses scope; private observations are created only by
    # post_llm_call() and can later be promoted explicitly.
    scope = 'project'
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        # Idempotent per (project, agent, content) â€” repeated identical tool
        # calls don't duplicate rows (ALTIMA gate #2). Use supersede to update.
        fp = core.fingerprint(content)
        mem_id, ver_id = core.create_memory(
            conn, pid, 'agent-' + agent_name, content,
            scope=scope, memory_type=args.get('type') or 'note',
            reason='memory_remember tool',
            idempotency_key=f'remember:{pid}:agent-{agent_name}:{fp}')
        conn.commit()
        return _tool_ok(memory_id=mem_id, version_id=ver_id, scope=scope)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_search(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    query = (args.get('query') or '').strip()
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        rows = core.search(conn, pid, aid, query, limit=10)
        return _tool_ok(results=[
            {'id': r[0], 'scope': r[1], 'lifecycle': r[2],
             'verification': r[3], 'freshness': r[4], 'content': r[5]}
            for r in rows
        ])
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_promote(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    memory_id = (args.get('memory_id') or '').strip()
    if not memory_id:
        return _tool_error('memory_id is required')
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        _pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
        core.promote(conn, memory_id, aid)
        conn.commit()
        return _tool_ok(memory_id=memory_id, promoted=True)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_supersede(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    memory_id = (args.get('memory_id') or '').strip()
    new_content = (args.get('new_content') or '').strip()
    if not memory_id or not new_content:
        return _tool_error('memory_id and new_content are required')
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        _pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
        core.supersede(conn, memory_id, aid, new_content,
                       reason=args.get('reason') or 'memory_supersede tool')
        conn.commit()
        return _tool_ok(memory_id=memory_id, superseded=True)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_reject(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    memory_id = (args.get('memory_id') or '').strip()
    if not memory_id:
        return _tool_error('memory_id is required')
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        _pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
        core.reject(conn, memory_id, aid,
                    reason=args.get('reason') or 'memory_reject tool')
        conn.commit()
        return _tool_ok(memory_id=memory_id, rejected=True, tombstoned=True)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_feedback(args, ctx=None):
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    memory_id = (args.get('memory_id') or '').strip()
    outcome = (args.get('outcome') or '').strip().lower()
    if not memory_id:
        return _tool_error('memory_id is required')
    if outcome not in ('accepted', 'rejected', 'stale'):
        return _tool_error('outcome must be accepted|rejected|stale')
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        if outcome == 'rejected':
            pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
            # core.reject owns one atomic transaction including audit+tombstone.
            core.reject(conn, memory_id, aid,
                        reason='rejected via memory_feedback: ' + memory_id)
            return _tool_ok(memory_id=memory_id, outcome=outcome)

        conn.execute('BEGIN IMMEDIATE')
        try:
            pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
            # Enforce private ownership and re-check lifecycle inside the write tx.
            _project_id, _scope, _owner, lifecycle, _role = (
                core._require_memory_write_access(conn, memory_id, aid)
            )
            if lifecycle in ('rejected', 'disabled', 'superseded'):
                raise core.MemCoreError(
                    f'cannot apply {outcome} feedback to terminal memory '
                    f'(lifecycle={lifecycle})'
                )
            if outcome == 'accepted':
                content = conn.execute(
                    'SELECT v.content FROM memory m JOIN memory_version v '
                    'ON v.id=m.current_version_id WHERE m.id=?',
                    (memory_id,)
                ).fetchone()[0]
                blocked = core._tombstone_active(
                    conn, core.fingerprint(content), pid,
                    scope=_scope, agent_id=_owner
                )
                if blocked:
                    raise core.TombstoneBlocked(
                        core.fingerprint(content), blocked[0]
                    )
                conn.execute(
                    "UPDATE memory SET lifecycle='accepted', updated_at=? WHERE id=?",
                    (core._now(), memory_id))
            else:
                conn.execute(
                    "UPDATE memory SET freshness='stale', updated_at=? WHERE id=?",
                    (core._now(), memory_id))
            store_audit(conn, 'feedback', aid, memory_id, pid,
                        {'outcome': outcome, 'source': 'memory_feedback tool'})
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            raise
        return _tool_ok(memory_id=memory_id, outcome=outcome)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def store_audit(conn, action, actor, memory_id=None, project_id=None, detail=None):
    """AuditEvent for tool-side writes outside core's transaction helpers."""
    conn.execute(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (action, actor, memory_id, project_id, json.dumps(detail or {}), core._now()))


# -- Hooks -------------------------------------------------------------------

def _load_live_config():
    """Authoritative config + profile when the hook payload doesn't carry them.

    pre_llm_call/post_llm_call payloads contain session metadata only â€” no
    `config` and no `profile_name` â€” so hooks must read them from the live
    Hermes config (profile-aware via HERMES_HOME).
    """
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _ctx_cfg(ctx):
    cfg = None
    if isinstance(ctx, dict):
        cfg = ctx.get('config')
    else:
        cfg = getattr(ctx, 'config', None)
    if isinstance(cfg, dict) and cfg:
        return cfg
    return _load_live_config()


def _ctx_profile(ctx):
    if isinstance(ctx, dict):
        name = ctx.get('profile_name') or ''
    else:
        name = getattr(ctx, 'profile_name', '') or ''
    if name:
        return name
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or ''
    except Exception:
        return ''


def pre_llm_call(ctx=None, user_message='', **_):
    """Bounded recall block injected into the user message. Fail-closed.

    Two calling shapes: Hermes lifecycle payload (user_message kwarg â€” the
    hook registry filters payload kwargs to the declared signature) and the
    test harness shape (a single dict ctx).
    """
    if isinstance(ctx, dict):
        query = (ctx.get('user_message') or user_message or '')
    else:
        query = user_message or ''
    config = _ctx_cfg(ctx)
    try:
        agent_name, project = require_binding(config, _ctx_profile(ctx))
    except ConfigError:
        return None
    if not pathlib.Path(default_store_path(config)).expanduser().exists():
        return None
    cfg = _memcore_cfg(config)
    inject_cfg = cfg.get('inject') or {}
    try:
        budget = max(0, int(inject_cfg.get('budget_chars', 1200)))
        max_items = max(0, int(inject_cfg.get('max_items', 8)))
    except (TypeError, ValueError):
        budget, max_items = 1200, 8
    if budget == 0 or max_items == 0:
        return None
    if _is_trivial_query(query):
        # Greeting/ack turns carry no durable intent (same definition the
        # ingest journal uses to ignore them). Skip pinned injection too:
        # ~500 chars saved per trivial turn, zero recall value lost.
        return None
    try:
        conn = _open_tool_store(config)
    except Exception:
        return None
    if conn is None:
        return None
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        pinned = conn.execute(
            'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, v.content '
            'FROM memory m JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
            'WHERE m.project_id = ? AND m.pinned = 1 AND m.critical = 1 '
            "  AND (m.scope = 'project' OR m.owner_agent_id = ?) "
            "  AND m.lifecycle IN ('candidate','accepted','conflict') "
            '  AND ' + core._recall_tombstone_guard('m') + ' '
            'ORDER BY m.critical DESC, datetime(m.updated_at) DESC, m.id ASC '
            'LIMIT ?',
            (pid, aid, max_items)).fetchall()
        pinned_ids = {r[0] for r in pinned}
        hits = []
        if query.strip():
            hits = [h for h in core.search(conn, pid, aid, query,
                                         limit=min(500, max_items + len(pinned)))
                    if h[0] not in pinned_ids]
        block = build_recall_block(pinned, hits, budget, max_items)
        if not block:
            return None
        # Lifecycle contract: dict with 'context' is injected into the turn's
        # user message (see agent/turn_context.py pre_llm_call handling).
        return {'context': block}
    except Exception:
        return None


def post_llm_call(ctx=None, assistant_message='', **_):
    """Conservative observation recording. Candidate-only, never auto-accept."""
    if isinstance(ctx, dict):
        observation = (ctx.get('assistant_message') or assistant_message or '')
    else:
        observation = assistant_message or ''
    observation = observation.strip()
    if len(observation) < 80:
        return None
    config = _ctx_cfg(ctx)
    try:
        agent_name, project = require_binding(config, _ctx_profile(ctx))
    except ConfigError:
        return None
    if not pathlib.Path(default_store_path(config)).expanduser().exists():
        return None
    try:
        conn = _open_tool_store(config)
    except Exception:
        return None
    if conn is None:
        return None
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        content = observation[:2000]
        core.create_memory(
            conn, pid, aid, content,
            scope='private', memory_type='observation',
            reason='post_llm_call observation',
            idempotency_key=f'observe:{pid}:{aid}:{core.fingerprint(content)}'
        )
        conn.commit()
    except Exception:
        return None
    return None


def auto_join(ctx):
    """Insert agent row + membership on first boot when enabled. Best-effort."""
    config = _ctx_cfg(ctx)
    cfg = _memcore_cfg(config)
    if not cfg.get('auto_join'):
        return
    try:
        agent_name, project = require_binding(config, _ctx_profile(ctx))
    except ConfigError:
        return
    try:
        conn = _get_conn(default_store_path(config))  # opens + creates on first boot
    except Exception:
        return
    try:
        conn.execute('BEGIN IMMEDIATE')
        aid = 'agent-' + agent_name
        existing_agent = conn.execute(
            'SELECT name, profile_key FROM agent WHERE id=?', (aid,)
        ).fetchone()
        if existing_agent is None:
            by_profile = conn.execute(
                'SELECT id, name FROM agent WHERE profile_key=?', (agent_name,)
            ).fetchone()
            if by_profile is not None:
                raise ConfigError(
                    'profile_key %s already belongs to %s' %
                    (agent_name, by_profile[0])
                )
            conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, agent_name, agent_name)
            )
        elif existing_agent != (agent_name, agent_name):
            raise ConfigError(
                'agent id %s has different identity (name=%s, profile_key=%s)' %
                (aid, existing_agent[0], existing_agent[1])
            )
        pid = _resolve_project_id(conn, project)
        if pid is None:
            # auto_join may create a slug-based project for convenience, but a
            # UUID is an exact durable reference and must never be rewritten as
            # proj-<uuid> when the referenced project is missing.
            try:
                uuid.UUID(project)
            except (ValueError, AttributeError, TypeError):
                pid = project if project.startswith('proj-') else 'proj-' + project
                conn.execute('INSERT INTO project (id, name) VALUES (?, ?)',
                             (pid, project.removeprefix('proj-')))
            else:
                raise ConfigError('configured project UUID does not exist: %s' % project)
        cur = conn.execute('INSERT OR IGNORE INTO project_membership '
                           '(project_id, agent_id, role) VALUES (?, ?, ?)',
                           (pid, aid, 'member'))
        if cur.rowcount:
            store_audit(conn, 'agent_joined', aid, None, pid,
                        {'source': 'memcore auto_join'})
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except Exception:
            pass


# -- Tool schemas (NO identity/scope params anywhere) -------------------------

TOOL_SCHEMAS = [
    {
        'name': 'memory_remember',
        'description': 'Store a fact, decision, or note in shared project memory.',
        'parameters': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string'},
                'type': {'type': 'string', 'enum': ['fact', 'decision', 'preference', 'note']},
            },
            'required': ['content'],
        },
        'handler': tool_memory_remember,
    },
    {
        'name': 'memory_search',
        'description': 'Search shared project memory (FTS5).',
        'parameters': {
            'type': 'object',
            'properties': {'query': {'type': 'string'}},
            'required': ['query'],
        },
        'handler': tool_memory_search,
    },
    {
        'name': 'memory_promote',
        'description': 'Promote a private memory to project scope.',
        'parameters': {
            'type': 'object',
            'properties': {'memory_id': {'type': 'string'}},
            'required': ['memory_id'],
        },
        'handler': tool_memory_promote,
    },
    {
        'name': 'memory_supersede',
        'description': 'Correct a memory: new version, history kept.',
        'parameters': {
            'type': 'object',
            'properties': {
                'memory_id': {'type': 'string'},
                'new_content': {'type': 'string'},
                'reason': {'type': 'string'},
            },
            'required': ['memory_id', 'new_content'],
        },
        'handler': tool_memory_supersede,
    },
    {
        'name': 'memory_reject',
        'description': 'Reject a memory and tombstone its claim.',
        'parameters': {
            'type': 'object',
            'properties': {
                'memory_id': {'type': 'string'},
                'reason': {'type': 'string'},
            },
            'required': ['memory_id'],
        },
        'handler': tool_memory_reject,
    },
    {
        'name': 'memory_feedback',
        'description': 'Mark a memory accepted/rejected/stale after use.',
        'parameters': {
            'type': 'object',
            'properties': {
                'memory_id': {'type': 'string'},
                'outcome': {'type': 'string', 'enum': ['accepted', 'rejected', 'stale']},
            },
            'required': ['memory_id', 'outcome'],
        },
        'handler': tool_memory_feedback,
    },
]
