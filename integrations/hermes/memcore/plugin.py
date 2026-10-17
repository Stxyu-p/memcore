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
import re
import sys
import threading
import uuid
from collections.abc import Mapping

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent

# NOTE: never add _PLUGIN_DIR itself to sys.path. Hermes plugin directories can
# contain names that shadow Hermes packages. Prefer an installed MemCore package,
# then an explicit MEMCORE_SRC checkout, then repository/legacy development roots.
def _memcore_engine_importable():
    """True iff the ENGINE resolves, not merely a directory named ``memcore``.

    ``memcore/memcore/`` has no ``__init__.py``, so the genuine engine is itself a
    PEP 420 namespace package and ``find_spec('memcore')`` returns
    ``origin=None`` for it. A plain folder named ``memcore/`` — which is what the
    process cwd contributes when Hermes starts above the checkout, and what any
    sibling directory looks like — is indistinguishable from it. Probing the
    ``memcore.core`` SUBMODULE is the only sound test: the shadow cannot satisfy
    it, the real engine can. Checking ``origin`` cannot separate the two, and
    ``find_spec('memcore') is not None`` is what silently disabled the fleet's
    memory provider from 2026-09-26 until 2026-10-03.
    """
    try:
        return importlib.util.find_spec('memcore.core') is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _ensure_memcore_importable():
    if _memcore_engine_importable():
        return
    candidates = []
    env_src = os.environ.get('MEMCORE_SRC')
    if env_src:
        candidates.append(pathlib.Path(env_src).expanduser())
    for parent in _PLUGIN_DIR.parents:
        if (parent / 'memcore' / 'core.py').is_file() and ((parent / 'memcore' / 'schema' / 'schema.sql').is_file() or (parent / 'schema' / 'schema.sql').is_file()):
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

#: Per-row content cap (F5 fix). Rows are truncated at a word boundary with
#: an ellipsis instead of skipped entirely — a missing condition or negation
#: can reverse a memory's meaning, so truncation only ever drops the TAIL.
MAX_ROW_CHARS = 220

#: Share of the budget pinned rows may consume when the turn's query also
#: produced search hits. Without a reserve, a handful of long Golden pins eats
#: the whole block and query-relevant hits never render — the ranking engine
#: then has no effect on what the model actually reads.
PINNED_MAX_SHARE = 0.5


_NEG_PAT = re.compile(
    r"\b(not|never|no|none|forbidden|prohibited|must\s+not|cannot|do\s+not)\b"
    r"|(ห้าม|ไม่|ไม่ใช่|ไม่ได้|ยกเว้น|ปราศจาก)",
    re.IGNORECASE,
)
_NUM_PATH_PAT = re.compile(r"(\d+(?:\.\d+)?|[a-zA-Z0-9_\-]+/[a-zA-Z0-9_.\-/]+|`[^`]+`)")


def _truncate_row_content(content, max_chars):
    """Collapse whitespace, cap at max_chars on a word boundary + ellipsis.

    Space-separated text cuts at the last space (never mid-word). Thai and
    other spaceless scripts fall back to a hard cut — there is no boundary
    to respect. Short content passes through untouched.
    Prioritizes salience (negations, numbers, paths) across clauses when possible.
    """
    text = ' '.join(str(content).split())
    if len(text) <= max_chars:
        return text

    clauses = [c.strip() for c in re.split(r'(?<=[.!?])\s+|\n+', text) if c.strip()]
    if len(clauses) > 1:
        def score_clause(idx, cl):
            score = 10.0 if idx == 0 else (3.0 / (idx + 1))
            if _NEG_PAT.search(cl):
                score += 15.0
            score += min(len(_NUM_PATH_PAT.findall(cl)) * 3.0, 12.0)
            return score

        ranked = sorted(enumerate(clauses), key=lambda x: score_clause(x[0], x[1]), reverse=True)
        selected = []
        used_len = 0
        for idx, cl in ranked:
            add_len = len(cl) + (3 if selected else 0)
            if used_len + add_len <= max_chars - 1:
                selected.append((idx, cl))
                used_len += add_len
        if selected:
            selected.sort(key=lambda x: x[0])
            res = " … ".join(c for _, c in selected)
            if len(res) < len(text) and not res.endswith("…"):
                res += "…"
            if len(res) <= max_chars + 1:
                return res

    cut = text[:max_chars]
    space = cut.rfind(' ')
    if space > max_chars // 2:
        cut = cut[:space]
    return cut.rstrip() + '…'


#: Default preview cap for tool_memory_search (L0 progressive recall).
#: Full bodies cost ~4.8x the chars of a preview row (BASELINE_token.md);
#: the tool lane returns previews unless include_content=True is explicit.
SEARCH_PREVIEW_CHARS = 180
SEARCH_PREVIEW_MIN = 40
SEARCH_PREVIEW_MAX = 500


def _clamp_preview_chars(raw):
    """Clamp hostile preview_chars into [MIN, MAX]; non-numeric -> default."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return SEARCH_PREVIEW_CHARS
    return min(max(n, SEARCH_PREVIEW_MIN), SEARCH_PREVIEW_MAX)


def _preview_search_content(content, preview_chars):
    """Collapse whitespace, cap at preview_chars via the shared row cap.

    Returns (text, truncated). Short content passes through untouched.
    """
    text = ' '.join(str(content).split())
    if len(text) <= preview_chars:
        return text, False
    return _truncate_row_content(text, preview_chars), True


def build_recall_block(pinned_rows, search_rows, budget_chars=1200, max_items=8,
                       pinned_max_share=PINNED_MAX_SHARE):
    """Deterministic, budget-capped recall block. Empty rows -> ''.

    Pinned/critical rows first, then search hits — deduped by memory id AND
    by claim fingerprint, so N corroborating copies collapse to one line
    (canonical first). One line per item: "- [scope] content". Whole block
    capped at budget_chars (header included). Each row's content is capped
    at MAX_ROW_CHARS on a word boundary (tail only). A row that still does
    not fit the remaining budget is skipped so a shorter later row can use
    the space — but when nothing fits, the first row is rendered truncated
    instead of returning an empty block for a non-empty match set.

    pinned_max_share caps how much of the budget the pinned tier may use once
    search hits exist, so query-relevant hits always get rendered space. With
    no hits there is nothing to reserve for and the cap does not apply.
    """
    seen_ids = set()
    seen_fps = set()
    pinned_set = []
    hit_set = []
    for tier_rows, tier in ((pinned_rows, pinned_set), (search_rows, hit_set)):
        if not isinstance(tier_rows, (list, tuple)):
            continue
        for r in tier_rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            if r[0] in seen_ids:
                continue
            seen_ids.add(r[0])
            # Prefer the row's stored claim fingerprint (index 8); compute the
            # SHA-256 fallback only for short rows or legacy NULL fingerprints.
            fp = r[8] if len(r) > 8 and r[8] else None
            if fp is None:
                try:
                    from memcore import core as _core
                    fp = _core.fingerprint(
                        ' '.join(str(r[5] if len(r) > 5 else r).split()))
                except Exception:
                    fp = None
            if fp is not None:
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
            tier.append(r)
    lines = []
    header = 'Shared project memory (memcore):'
    try:
        budget_chars = int(budget_chars)
        if budget_chars < 0:
            budget_chars = 1200
    except (TypeError, ValueError, OverflowError):
        budget_chars = 1200
    try:
        max_items = int(max_items)
        if max_items < 1:
            max_items = 8
    except (TypeError, ValueError, OverflowError):
        max_items = 8
    prefix_len = len(header) + 1  # header + first newline
    if budget_chars <= prefix_len:
        return ''
    # Reserve budget for hits whenever the query produced any, so a handful of
    # long Golden pins cannot starve the rows that actually match the turn.
    pinned_cap = budget_chars
    if hit_set and pinned_max_share is not None:
        try:
            share = float(pinned_max_share)
        except (TypeError, ValueError):
            share = PINNED_MAX_SHARE
        share = min(max(share, 0.0), 1.0)
        if share > 0:
            pinned_cap = min(budget_chars,
                             prefix_len + int((budget_chars - prefix_len) * share))
    used = prefix_len

    def _fit(r, cap):
        content = ' '.join(str(r[5] if len(r) > 5 else r).split())
        label = '- [%s | %s | %s | %s] ' % (
            r[1] if len(r) > 1 else '?', r[2] if len(r) > 2 else '?',
            r[3] if len(r) > 3 else '?', r[4] if len(r) > 4 else '?')
        separator = 1 if lines else 0
        remaining = min(cap, budget_chars) - used - separator
        if remaining <= len(label) + 40:
            return None
        line = label + _truncate_row_content(content, MAX_ROW_CHARS)
        if len(line) > remaining:
            # Too long even capped — let a shorter later row use the space.
            return None
        return line, separator

    for r in pinned_set:
        if len(lines) >= max_items:
            break
        fit = _fit(r, pinned_cap)
        if fit is not None:
            line, separator = fit
            lines.append(line)
            used += separator + len(line)
    for r in hit_set:
        if len(lines) >= max_items:
            break
        fit = _fit(r, budget_chars)
        if fit is not None:
            line, separator = fit
            lines.append(line)
            used += separator + len(line)
    if not lines and (pinned_set or hit_set):
        # Fallback: a non-empty match set never renders empty. Truncate the
        # first (highest-priority) row to whatever budget remains.
        r = (pinned_set or hit_set)[0]
        content = ' '.join(str(r[5] if len(r) > 5 else r).split())
        label = '- [%s | %s | %s | %s] ' % (
            r[1] if len(r) > 1 else '?', r[2] if len(r) > 2 else '?',
            r[3] if len(r) > 3 else '?', r[4] if len(r) > 4 else '?')
        available = budget_chars - prefix_len - len(label)
        if available >= 40:
            lines.append(label + _truncate_row_content(content, available))
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
                # The handle is cached for the life of the thread, so a write
                # interrupted by something `except Exception` cannot catch
                # (SIGINT, worker kill, plugin reload) would leave an open
                # transaction behind — and the next turn would fail with
                # "cannot start a transaction within a transaction". The old
                # per-turn close() discarded it for free. Roll back here
                # instead: one guard at the choke point every caller routes
                # through, cheaper and safer than a try/finally per hook.
                if conn.in_transaction:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
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


def _tool_str_arg(args, key, required=False, default=''):
    if not isinstance(args, dict):
        return None, 'arguments must be a JSON object'
    val = args.get(key)
    if val is None:
        if required:
            return None, f'{key} is required'
        return default, None
    if not isinstance(val, str):
        return None, f'{key} must be a string'
    val = val.strip()
    if required and not val:
        return None, f'{key} is required'
    return val, None


def tool_memory_remember(args, ctx=None):
    content, err = _tool_str_arg(args, 'content', required=True)
    if err:
        return _tool_error(err)
    raw_type = args.get('type')
    if raw_type is not None and not isinstance(raw_type, str):
        return _tool_error('type must be a string')
    mem_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else 'note'
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    # ADR-0003: explicit tool writes are always shared project memory.
    # The model never chooses scope; private observations are created only by
    # the semantic review lane and can later be promoted explicitly.
    scope = 'project'
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        # Idempotent per (project, agent, content) — repeated identical tool
        # calls don't duplicate rows (ALTIMA gate #2). Use supersede to update.
        # ADR-0019 explicit lane: memory_remember is a deliberate durability
        # request, so the row is accepted directly (not parked in candidate).
        # Single source ≠ corroborated: verification stays unverified until
        # independent agents repeat the claim (ADR-0018 sweep below).
        fp = core.fingerprint(content)
        mem_id, ver_id = core.create_memory(
            conn, pid, 'agent-' + agent_name, content,
            scope=scope, memory_type=mem_type,
            lifecycle='accepted',
            reason='memory_remember tool (explicit auto-accept)',
            source_file=args.get('source_file'),
            source_commit=args.get('source_commit'),
            idempotency_key=f'remember:{pid}:agent-{agent_name}:{fp}')
        core.maybe_auto_corrob(conn, pid, fp, 'agent-' + agent_name)
        conn.commit()
        return _tool_ok(memory_id=mem_id, version_id=ver_id, scope=scope)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_search(args, ctx=None):
    query, err = _tool_str_arg(args, 'query', required=False, default='')
    if err:
        return _tool_error(err)
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        pid, aid = _require_bound_membership(conn, project, agent_name)
        rows = core.recall(conn, pid, aid, query, limit=10)
        if args.get('include_content'):
            return _tool_ok(results=[
                {'id': r[0], 'scope': r[1], 'lifecycle': r[2],
                 'verification': r[3], 'freshness': r[4], 'content': r[5],
                 'content_truncated': False, 'content_chars': len(r[5] or '')}
                for r in rows
            ])
        preview_chars = _clamp_preview_chars(args.get('preview_chars'))
        results = []
        for r in rows:
            text, truncated = _preview_search_content(r[5], preview_chars)
            results.append(
                {'id': r[0], 'scope': r[1], 'lifecycle': r[2],
                 'verification': r[3], 'freshness': r[4], 'content': text,
                 'content_truncated': truncated,
                 'content_chars': len(r[5] or '')})
        return _tool_ok(results=results)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_promote(args, ctx=None):
    memory_id, err = _tool_str_arg(args, 'memory_id', required=True)
    if err:
        return _tool_error(err)
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
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
    memory_id, err = _tool_str_arg(args, 'memory_id', required=True)
    if err:
        return _tool_error(err)
    new_content, err = _tool_str_arg(args, 'new_content', required=True)
    if err:
        return _tool_error(err)
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
    conn, store_error = _tool_store_or_error(_ctx_cfg(ctx))
    if store_error:
        return store_error
    try:
        _pid, aid = _require_bound_memory(conn, project, agent_name, memory_id)
        core.supersede(conn, memory_id, aid, new_content,
                       reason=args.get('reason') or 'memory_supersede tool',
                       source_file=args.get('source_file'),
                       source_commit=args.get('source_commit'))
        conn.commit()
        return _tool_ok(memory_id=memory_id, superseded=True)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def tool_memory_reject(args, ctx=None):
    memory_id, err = _tool_str_arg(args, 'memory_id', required=True)
    if err:
        return _tool_error(err)
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
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
    memory_id, err = _tool_str_arg(args, 'memory_id', required=True)
    if err:
        return _tool_error(err)
    outcome, err = _tool_str_arg(args, 'outcome', required=False, default='')
    if err:
        return _tool_error(err)
    outcome = outcome.lower()
    if outcome not in ('accepted', 'rejected', 'stale'):
        return _tool_error('outcome must be accepted|rejected|stale')
    try:
        agent_name, project = require_binding(_ctx_cfg(ctx), _ctx_profile(ctx))
    except ConfigError as e:
        return _tool_error(str(e))
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

        with store.write_tx(conn):
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
                # Route through core.auto_accept so this path gains the same
                # conflict gate + corroboration sweep the ingest lanes have.
                # Hold is audit-only: on contradiction leave the row as-is,
                # write no accepted feedback, and report the hold clearly.
                result = core.auto_accept(
                    conn, memory_id, aid,
                    reason='accepted via memory_feedback tool',
                )
                if result['held']:
                    return _tool_ok(memory_id=memory_id, outcome='held',
                                    held=True,
                                    reason='contradiction hold: claim contradicts live memory',
                                    hits=[list(h) for h in result['hits']])
                store_audit(conn, 'feedback', aid, memory_id, pid,
                            {'outcome': outcome, 'source': 'memory_feedback tool',
                             'transition_applied': result['transitioned']})
            else:
                conn.execute(
                    "UPDATE memory SET freshness='stale', updated_at=? WHERE id=?",
                    (core._now(), memory_id))
                store_audit(conn, 'feedback', aid, memory_id, pid,
                            {'outcome': outcome, 'source': 'memory_feedback tool'})
        return _tool_ok(memory_id=memory_id, outcome=outcome)
    except Exception as e:
        return _tool_error('%s: %s' % (type(e).__name__, e))


def store_audit(conn, action, actor, memory_id=None, project_id=None, detail=None):
    """AuditEvent for tool-side writes outside core's transaction helpers."""
    conn.execute(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (action, actor, memory_id, project_id, json.dumps(detail or {}), core._now()))


# -- Config resolution -----------------------------------------------------

def _load_live_config():
    """Authoritative config + profile when a tool payload omits them.

    Some hosts call the tools without a config/profile payload; those must
    read the live Hermes config (profile-aware via HERMES_HOME).
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
        with store.write_tx(conn):
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
                'source_file': {
                    'type': 'string',
                    'description': 'Optional path to a source file; MemCore stores its absolute path and SHA-256.',
                },
                'source_commit': {
                    'type': 'string',
                    'description': 'Optional repository commit for provenance context.',
                },
            },
            'required': ['content'],
        },
        'handler': tool_memory_remember,
    },
    {
        'name': 'memory_search',
        'description': 'Search shared project memory (FTS5). Returns previews by default; full bodies need include_content.',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string'},
                'include_content': {'type': 'boolean'},
                'preview_chars': {'type': 'integer'},
            },
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
                'source_file': {
                    'type': 'string',
                    'description': 'Optional path to the replacement claim source file; MemCore stores its absolute path and SHA-256.',
                },
                'source_commit': {
                    'type': 'string',
                    'description': 'Optional repository commit for provenance context.',
                },
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
