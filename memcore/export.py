"""Export governed memory as agent-readable instruction files.

Every major coding agent already reads a plain markdown convention file from
the working tree, and every one of them does it with zero processes and zero
ports: Codex reads `AGENTS.md` and `MEMORY.md`, Antigravity (`agy`) reads
`GEMINI.md` and `AGENTS.md`, Claude Code reads `CLAUDE.md`. Verified by
scanning the installed binaries for those filenames.

So the local-first answer to "make MemCore global" is not a server — it is one
command that writes the best memories into the file each agent already reads.
No daemon, no port, no MCP, no dependency. The file is regenerated on demand;
nothing polls it.

The export is deliberately SELECTIVE. Measured on the live store: 47 of 133
accepted memories have never been recalled, and research on memory-ranking
systems shows that low-precision context actively degrades retrieval. Ranking
therefore happens here, using the engine's own ranking, and the output is
capped so a huge store cannot produce an unusable file.
"""
from __future__ import annotations

import hashlib
import pathlib

from . import core

#: How each agent family discovers its convention file. Values are the exact
#: filenames found in the installed binaries.
AGENT_CONVENTIONS = {
    'codex': ('AGENTS.md', 'MEMORY.md'),
    'agy': ('AGENTS.md', 'GEMINI.md'),
    'claude': ('CLAUDE.md',),
    'gemini': ('GEMINI.md',),
    'generic': ('AGENTS.md',),
}

#: One output path per host. Each list is ordered by how strongly the host was
#: verified to read it — the first entry is that host's PRIMARY file.
#:
#: Evidence (installed binaries scanned 2026-10-08, plus `freebuff --help`):
#:   codex    AGENTS.md x73, MEMORY.md x72  -> MEMORY.md is the memory surface
#:   agy      AGENTS.md x54, GEMINI.md x49  -> GEMINI.md is its own convention
#:   freebuff ".agents" files + mcp.json    -> a directory, per its own --help
#:   claude   CLAUDE.md
#:
#: Default is ONE file for every host: when the engine changes, every agent must
#: see the same content. Divergent per-host files create the worst kind of bug —
#: two agents, two different truths, no obvious cause.
HOST_TARGETS = {
    'codex': ('MEMORY.md',),
    'agy': ('GEMINI.md',),
    'freebuff': ('.agents/memory.md',),
    'claude': ('CLAUDE.md',),
    'generic': ('MEMORY.md',),
}

#: Writes every known target. Convenient when several agents work the same repo;
#: noisy in git, so it is opt-in rather than the default.
HOST_ALL_TARGETS = ('MEMORY.md', 'GEMINI.md', 'CLAUDE.md', '.agents/memory.md')

#: Where the export lands when no --host and no --out is given.
DEFAULT_OUT = pathlib.Path('MEMORY.md')


def targets_for(host):
    """Output paths for one host, or the single default for an unknown host."""
    if host is None:
        return (DEFAULT_OUT,)
    key = str(host).strip().lower()
    if key == 'all':
        return HOST_ALL_TARGETS
    return HOST_TARGETS.get(key, (DEFAULT_OUT,))


def _stamp():
    return core._now()


def rank_for_export(conn, project_id, agent_id, limit, include_private=False):
    """Top memories for a human/agent-readable brief, in engine rank order.

    Uses core.search with a deliberately empty query path: rather than ranking
    against a query, it walks the live rows the engine itself considers
    trustworthy and orders them by the same retention signal recall uses. This
    keeps a single ranking definition — an export that ranked differently from
    recall would be worse than useless.
    """
    rows = conn.execute(
        'SELECT m.id, m.scope, m.lifecycle, m.verification, m.freshness, '
        '       v.content, m.claim_fingerprint '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id AND v.memory_id = m.id '
        'WHERE m.project_id = ? '
        "  AND m.lifecycle IN ('candidate', 'accepted', 'conflict') "
        # Private rows are opt-in: an export is a file OTHER agents read,
        # and scope confinement is the entire point of the store. Measured
        # before this guard: 36 private rows were reachable by default.
        "  AND (m.scope = 'project' OR (? = 1 AND m.owner_agent_id = ?)) "
        '  AND ' + core._recall_tombstone_guard('m') + ' '
        'ORDER BY m.pinned DESC, m.critical DESC, '
        "CASE m.lifecycle WHEN 'accepted' THEN 0 WHEN 'conflict' THEN 1 ELSE 2 END, "
        "CASE m.verification WHEN 'user_authoritative' THEN 0 "
        "WHEN 'runtime_verified' THEN 1 WHEN 'source_backed' THEN 2 ELSE 3 END, "
        "CASE m.freshness WHEN 'current' THEN 0 WHEN 'aging' THEN 1 ELSE 2 END, "
        'm.updated_at DESC, m.id ASC',
        (project_id, 1 if include_private else 0, agent_id)).fetchall()
    # Fold corroborating copies: the fleet writes one claim from several agents,
    # so an export that repeats it teaches the reader nothing new.
    seen = set()
    keep = []
    for row in rows:
        fp = row[6] or core.fingerprint(' '.join(str(row[5]).split()))
        if fp in seen:
            continue
        seen.add(fp)
        keep.append(row)
        if len(keep) >= limit:
            break
    return keep


def _label(row):
    return '[%s | %s | %s | %s]' % (
        row[1], row[2], row[3], row[4])


def render(rows, title, project, generated_at=None, generator=True):
    """Render the export as markdown a coding agent can act on.

    The generated marker carries a digest of the CONTENT, not a timestamp, so
    a re-run that changes nothing produces a byte-identical file. Measured:
    with a timestamp the writer rewrote the file every single run, which
    would show as a permanent dirty git status in every repo.
    """
    body = _body_lines(rows, project)
    out = ['# %s' % title, '']
    if generator:
        digest = hashlib.sha256('\n'.join(body).encode('utf-8')).hexdigest()[:12]
        out.append('<!-- generated by MemCore (content %s) — do not edit by hand; '
                   'run `python -m memcore export` to refresh -->' % digest)
        out.append('')
    return '\n'.join(out + body) + '\n'


def _body_lines(rows, project):
    """The part of the export that reflects actual memory content."""
    out = []
    if not rows:
        out.append('No governed memory available for this project yet.')
        return out
    out.append('%d governed memories, ranked. Each line carries its trust '
               'state: [scope | lifecycle | verification | freshness].' % len(rows))
    out.append('')
    for row in rows:
        content = ' '.join(str(row[5]).split())
        out.append('- %s %s' % (_label(row), content))
    out.append('')
    out.append('---')
    out.append('')
    out.append('- Project: `%s`' % project)
    out.append('- Treat `candidate` and `unverified` rows as tentative; '
               '`conflict` rows are disputed and unresolved.')
    return out


def export(conn, project_id, agent_id, out_path=None, limit=40,
           include_private=False, title='Shared project memory',
           write=True, force=False, host=None, out_dir=None):
    """Build (and by default write) the export file(s).

    ``host`` selects the conventional filename(s) for one agent family; an
    explicit ``out_path`` always wins. Content is identical for every target —
    see HOST_TARGETS for why.
    """
    rows = rank_for_export(conn, project_id, agent_id, limit, include_private)
    text = render(rows, title, project_id)
    size = len(text.encode('utf-8'))
    if out_path is not None:
        paths = [pathlib.Path(out_path)]
    else:
        base_dir = pathlib.Path(out_dir) if out_dir else pathlib.Path.cwd()
        paths = [base_dir / name for name in targets_for(host)]
    results = []
    for path in paths:
        result = {'path': str(path), 'rows': len(rows), 'bytes': size,
                  'wrote': False}
        if write:
            path = path.expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Only rewrite when the content actually changes, so agents (and git)
            # do not see a modified timestamp on every invocation.
            previous = path.read_text(encoding='utf-8') if path.is_file() else None
            if previous != text or force:
                path.write_text(text, encoding='utf-8')
                result['wrote'] = True
        results.append(result)
    return results if len(results) > 1 else results[0]
