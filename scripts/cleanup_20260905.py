"""MemStore cleanup: junk reject, merges supersede, long shorten, promote stable facts."""
import sys, os
sys.path.insert(0, 'C:/Users/BlankScreen/Workspace/memcore')
from memcore import store, core

db = os.path.expanduser('~/.memcore/memory.db')
conn = store.open_store(db)
AGENT = 'agent-mika'

def reject(mid, reason):
    try:
        core.reject(conn, mid, AGENT, reason)
        print(f'  REJECTED {mid}: {reason[:60]}')
    except Exception as e:
        print(f'  ERR reject {mid}: {e}')

def supersede(mid, new_content, reason):
    try:
        core.supersede(conn, mid, AGENT, new_content, reason)
        print(f'  SUPERSEDED {mid} -> len({len(new_content)})')
    except Exception as e:
        print(f'  ERR supersede {mid}: {e}')

def promote(mid):
    try:
        core.promote(conn, mid, AGENT)
        print(f'  PROMOTED {mid} private->project')
    except Exception as e:
        print(f'  ERR promote {mid}: {e}')

# ── 1. JUNK (7) ─────────────────────────────────────────────
print('=== 1. REJECT JUNK ===')
reject('mem-0bb03e508767', 'Cleanup: junk blob from godmode @file prompt dump (4000ch). Not an operational fact.')
reject('mem-ff8bbf7a7bc6', 'Cleanup: raw SORA audit output (4000ch). Summary preserved in superseded entries.')
reject('mem-9a3ec2659816', 'Cleanup: smoke test artifact (26ch). No operational value.')
reject('mem-5932f89c5fc6', 'Cleanup: exact duplicate of mem-00cd4a9d1dff (141ch, same content word-for-word).')
reject('mem-58b18044c80b', 'Cleanup: test isolation scratch (60ch). No operational value.')
reject('mem-e89eb4262a9b', 'Cleanup: burn-in staging scratch (57ch). No operational value.')
reject('mem-48fa21b6877f', 'Cleanup: Holographic provider description (239ch). Deprecated since MemCore cutover 2026-09-02; info now redundant with standing MEMORY entry.')

# ── 2. MERGE lnwjud (3 → 1) ─────────────────────────────────
print('\n=== 2. SUPERSEDE lnwjud ===')
lnwjud_canonical = (
    'lnwjud v4.44.0 (Electron app at %LOCALAPPDATA%\\Programs\\lnwjud) = Windows automation suite '
    'with built-in MCP stdio bridge (lnwjud-mcp-stdio.cmd) exposing 224 tools: workspace/file/git/process/shell '
    '+ computer_use/vision/accessibility/input_event/window/clipboard/web_fetch/dom_cdp/screen_record/office/wsl_exec/run_goal. '
    'Custom wire protocol: EVERY request needs params._meta = {io.modelcontextprotocol/protocolVersion: "2026-07-28", '
    'io.modelcontextprotocol/clientCapabilities: {}} or it rejects with -32022; Hermes native mcp_servers registration will NOT connect; '
    'use wrapper C:/Users/BlankScreen/Workspace/lnwjud-bridge/lnwjud_call.py (python lnwjud_call.py <tool> \'<json>\', stateless per-call). '
    'Bridge runs standalone without the desktop app. Registered workspaces: ~/Workspace, memcore, hermes-memcore-plugin, hermes-agent. '
    'computer_use requires workspaceId; window/input_event use field operation. Verified live 2026-09-02.'
)
supersede('mem-55bbd631306e', lnwjud_canonical, 'Consolidate: merge 3 lnwjud entries (55bbd+54724+de78f) into one canonical fact.')
reject('mem-547240b8611d', 'Cleanup: merged into mem-55bbd631306e (lnwjud canonical). Thai duplicate.')
reject('mem-de78f0881404', 'Cleanup: merged into mem-55bbd631306e (lnwjud canonical). Short duplicate.')

# ── 3. MERGE bot-dispatch (3 → 1) ───────────────────────────
print('\n=== 3. SUPERSEDE bot-dispatch ===')
dispatch_canonical = (
    'Bot-to-bot dispatch (kanban deprecated 2026-08-21): write message to file with mandatory prefix '
    '"Message from [bot] <sender_name>: " + newline + message body. Run background: hermes -p <profile> chat '
    '--in ~ -c "Bot Chat" --create-if-missing -Q --query-file <file>. Reply arrives as new message in session. '
    'Commands to agents always in English. Op constraint 2026-09-05: no concurrent dispatch cap needed — '
    'call freely without rate limit concern.'
)
supersede('mem-9d04f45b254b', dispatch_canonical, 'Consolidate: merge 3 bot-dispatch entries (9d04+e344+MEMORY) into one canonical fact.')
reject('mem-e344017489ce', 'Cleanup: merged into mem-9d04f45b254b (bot-dispatch canonical). Thai duplicate from earlier session.')

# ── 4. SHORTEN long entries ──────────────────────────────────
print('\n=== 4. SHORTEN >300ch entries ===')
shorts = [
    ('mem-12ece0327b39',
     'Skills hub (2026-09-03): godmode installed via hermes skills install -y; ui-ux-pro-max updated via hermes skills update. '
     'Pitfall: hermes skills check update_available lies for url-sourced skills on Windows (CRLF false positive) — '
     'verify via diff against raw.githubusercontent.com with tr -d "\\r" both sides.'),
    ('mem-1cb3d6aaa6fa',
     'MemCore cutover 2026-09-02 (P Choke order: "ถึงเวลาถอด"): burn-in gate passed (suite 208 green, doctor clean, '
     'round-trip memory_remember→memory_search). Deleted hermes-memory-ui (CLI WinError 5 file lock → rm direct). '
     'Pushed c67a261 per Stage 5. Hermes app restart pending to load new plugin runtime.'),
    ('mem-2b503b02f898',
     'Telefilter (MV3 Brave, v0.9.0) 2026-09-02: unlocked protected content (noforwards) by patching '
     'forwardCheck→copy flow in MAIN-world adapter service-worker.js. 52 tests green (+4 copy paths). '
     'Uncommitted: 12 modified files in extension tree. Not yet live-tested in Brave (needs extension reload).'),
    ('mem-74b08adece91',
     'MemCore at C:/Users/BlankScreen/Workspace/memcore/ — stdlib-only Python package: memcore/{store,core,ingest,engine,native_provider}. '
     'Deployed plugin at LOCALAPPDATA/hermes/plugins/memcore. Run: python -m memcore {init,project,agent,member,remember,search,doctor}.'),
    ('mem-d6196a8ab6f3',
     'MemCore CLI: python -m memcore {init,project,agent,member,remember,search,promote,supersede,deactivate,doctor}. '
     'Fingerprint-based dup detection + bulk-cleanup tool for merges.'),
    ('mem-09b2c98f2a24',
     'NovelClaw QA fixes (commit 5bc36b1): (1) number-comparison false-positived on commas/order/Chinese numerals '
     '— fixed via findMissingNumbers normalization; (2) LLM dropped paragraphs per chunk — fixed with chunk-level retry x3. '
     'Recomputed 151 reports, no chapter below 90.'),
    ('mem-99e1895d8308',
     'MemCore status 2026-09-02: doctor clean (integrity/FK/FTS/memberships OK, 6 agents bound). '
     'Fix: deployed plugin at LOCALAPPDATA was stale — resynced via deploy_hermes_plugin.py --check. App restart still pending.'),
    ('mem-7a13a0dfa455',
     'simplify-code is a bundled skill (curator patches refused). Each run needs manual gap-filling: '
     '(1) no git repo → git init; (2) parallel subagents via delegate_task each inherit Hermes config. '
     'Use for parallel 4-agent code cleanup of recent changes.'),
    ('mem-1e111b3f85b7',
     'MemCore desktop dashboard fix (2026-08-31): empty /memcore page came from missing dashboard/manifest.json. '
     'Hermes desktop scans plugin dirs for manifest.json + plugin.js; create both to register tab.'),
    ('mem-a1176c6ea1f7',
     'NovelClaw audit hardening (commit 717ab3f, suite green): SSRF protection via dial-time check '
     '(Dialer.Control + TestValidator); staticcheck 0 findings; qa scripts moved to scripts/qa-archived + gitignore covers data dumps.'),
    ('mem-a4c0b2bc16d5',
     'Profile configs carry their own custom base_url; it must end in /v1 (http://localhost:20128/v1) '
     'or 9router returns 404 HTML (Next.js). Symptoms: default profile works, others 404. Fix with regex re.M + restart app (2026-08-16).'),
    ('mem-2fcfcad97e5b',
     'Dispatch ops gotchas: always pass -m B.AI (old Bot Chats restore dead models = 401); '
     'model must also be listed in 9router config. Pin stable model; never override silently.'),
    ('mem-6af3ac177392',
     'SOUL v2.1 fleet rules (2026-08-31, ALTIMA-approved): relayed claims keep verification status; '
     'verified claim must attach evidence; replies inline and scannable; briefs need absolute paths + settled constraints; '
     'max 2 parallel dispatches per wave; MIKA uses HYPOTHESIS label; bad news early.'),
    ('mem-ff22afd0e5b9',
     '"hermes config set plugins.enabled ..." writes a string instead of a list and breaks config '
     '(loader requires list). Use hermes plugins enable/disable <name> or fix via Python yaml in venv. '
     'eagle-eye plugin needs install.sh layout (src/plugin.py→__init__.py, plugin.yaml→root, etc.) else "No __init__.py" error.'),
]
for mid, short in shorts:
    supersede(mid, short, 'Shorten to ≤280ch for recall budget. Content preserved; full history in version chain.')

# ── 5. PROMOTE stable private→project ────────────────────────
print('\n=== 5. PROMOTE stable private→project ===')
promote('mem-0e5284903e23')   # MemCore behavioral proof
promote('mem-0fa8f83ae01c')   # MemCore = canonical team memory
promote('mem-00cd4a9d1dff')   # SORA gc/stats/import verdict

# ── 6. ACCEPT stable facts ───────────────────────────────────
print('\n=== 6. ACCEPT stable facts ===')
for mid in ['mem-c9e857056622', 'mem-6f95d1a4a2e5', 'mem-950875d4652f', 'mem-6c599ac46575', 'mem-263d6d9d419e']:
    try:
        conn.execute("UPDATE memory SET lifecycle='accepted', verification='source_backed' WHERE id=? AND lifecycle='candidate'", (mid,))
        print(f'  ACCEPTED {mid}')
    except Exception as e:
        print(f'  ERR accept {mid}: {e}')
conn.commit()

# ── FINAL STATS ──────────────────────────────────────────────
print('\n=== FINAL STATS ===')
stats = conn.execute('''
    SELECT
        COUNT(*) as total,
        SUM(CASE WHEN lifecycle='accepted' THEN 1 ELSE 0 END) as accepted,
        SUM(CASE WHEN lifecycle='candidate' THEN 1 ELSE 0 END) as candidate,
        SUM(CASE WHEN lifecycle='rejected' THEN 1 ELSE 0 END) as rejected,
        SUM(CASE WHEN lifecycle='disabled' THEN 1 ELSE 0 END) as disabled,
        SUM(CASE WHEN lifecycle='superseded' THEN 1 ELSE 0 END) as superseded
    FROM memory
''').fetchone()
print(f'  total={stats[0]} accepted={stats[1]} candidate={stats[2]} rejected={stats[3]} disabled={stats[4]} superseded={stats[5]}')

ver = conn.execute('SELECT verification, COUNT(*) FROM memory GROUP BY verification').fetchall()
print(f'  verification: {dict(ver)}')

long = conn.execute('''
    SELECT m.id, length(v.content), substr(v.content,1,60), m.lifecycle
    FROM memory m JOIN memory_version v ON v.id=m.current_version_id
    WHERE length(v.content) > 280 AND m.lifecycle NOT IN ('rejected','disabled','superseded')
    ORDER BY length(v.content) DESC LIMIT 10
''').fetchall()
print(f'  >280ch non-terminal: {len(long)} rows')
for r in long:
    print(f'    {r[0]} {r[1]}ch [{r[3]}] {r[2]}')

avg = conn.execute('''
    SELECT AVG(length(v.content))
    FROM memory m JOIN memory_version v ON v.id=m.current_version_id
    WHERE m.lifecycle NOT IN ('rejected','disabled','superseded')
''').fetchone()
print(f'  avg length (active): {avg[0]:.0f}ch')

tombstones = conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()
print(f'  tombstones: {tombstones[0]}')

conn.close()
print('\nDONE')
