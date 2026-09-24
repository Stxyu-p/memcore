"""Shorten 5 long pinned facts to <=280ch (2026-09-13). Abort-all on length violation."""
import sqlite3
import sys

sys.path.insert(0, 'C:/Users/BlankScreen/Workspace/memcore')
from memcore import store, core

DB = 'C:/Users/BlankScreen/.memcore/memory.db'
BAK = 'C:/Users/BlankScreen/.memcore/memory.db.pre-shorten-20260913.bak'
AGENT = 'agent-mika'

CANDIDATES = {
    'mem-9d04f45b254b':
        'Bot-to-bot dispatch (kanban retired 2026-08-21): write file with prefix '
        '"Message from [bot] <name>: "+body, run `hermes -p <p> chat --in ~ -c '
        '"Bot Chat" --create-if-missing -Q --query-file <file>`. Reply in session. '
        'English commands. No concurrency cap (override 2026-09-05).',
    'mem-ff22afd0e5b9':
        'Never `hermes config set plugins.enabled`: writes string, loader needs list, '
        'breaks all plugins. Use `hermes plugins enable/disable <name>`; repair via '
        'venv-Python yaml. eagle-eye needs its install.sh layout or "No __init__.py" error.',
    'mem-8d909e90be49':
        'HERMES_PROFILE env does not switch profiles (overwrites main config). Use '
        '`hermes profile use NAME` (sticky); back with `hermes profile use default`. '
        'Discord home_channel needs `platform: discord` or gateway KeyErrors.',
    'mem-6af3ac177392':
        'SOUL v2.1 fleet rules (ALTIMA-approved 2026-08-31): relay claims with verification '
        'status; verified claims attach evidence; replies inline/scannable; briefs carry '
        'absolute paths + constraints; no dispatch cap (override 2026-09-05); bad news early.',
    'mem-55bbd631306e':
        'lnwjud (224 tools): call ONLY via '
        'C:/Users/BlankScreen/Workspace/lnwjud-bridge/lnwjud_call.py '
        '(native MCP registration fails). Every request needs params._meta '
        '{protocolVersion, clientCapabilities} or -32022 rejects. '
        'computer_use needs workspaceId. Verified live 2026-09-02.',
}
# Restore prior trust for these (were accepted/source_backed). mem-8d909e90be49
# stays candidate/unverified as before.
REACCEPT = ['mem-9d04f45b254b', 'mem-ff22afd0e5b9', 'mem-6af3ac177392', 'mem-55bbd631306e']

conn = store.open_store(DB)

print('=== PRE-CHECK ===')
bad = False
for mid, new in CANDIDATES.items():
    row = conn.execute(
        "SELECT m.lifecycle, m.verification, m.pinned, length(v.content) "
        "FROM memory m JOIN memory_version v ON v.id=m.current_version_id "
        "WHERE m.id=?", (mid,)).fetchone()
    assert row, 'missing ' + mid
    flag = 'OK' if len(new) <= 280 else 'TOO LONG'
    if len(new) > 280:
        bad = True
    print('  [%s] %s %s/%s pin=%s old_len=%s new_len=%s'
          % (flag, mid, row[0], row[1], row[2], row[3], len(new)))
if bad:
    print('ABORT: length violation, no writes performed')
    sys.exit(1)

print('=== BACKUP ===')
src = store.open_store(DB)
dst = sqlite3.connect(BAK)
src._conn.backup(dst) if hasattr(src, '_conn') else None
dst.close()
# store.open_store returns a sqlite3 connection directly in this engine version
import sqlite3 as _s
s = _s.connect(DB)
d = _s.connect(BAK)
s.backup(d)
d.close()
s.close()
chk = _s.connect(BAK)
print('  backup memory count:', chk.execute('SELECT count(*) FROM memory').fetchone()[0])
chk.close()

print('=== SUPERSEDE ===')
for mid, new in CANDIDATES.items():
    try:
        ver = core.supersede(
            conn, mid, AGENT, new,
            reason='Recall budget: shorten pinned fact to <=280ch (2026-09-13).')
        print('  SUPERSEDED %s -> %s (len %s)' % (mid, ver, len(new)))
    except Exception as e:
        print('  SKIP %s: %s: %s' % (mid, type(e).__name__, e))

print('=== RE-ACCEPT (restore prior trust) ===')
now = core._now()
for mid in REACCEPT:
    conn.execute(
        "UPDATE memory SET lifecycle='accepted', verification='source_backed', "
        "updated_at=? WHERE id=?", (now, mid))
    print('  accepted+source_backed', mid)
conn.commit()

print('=== POST STATS ===')
print('  by lifecycle:', conn.execute('SELECT lifecycle, count(*) FROM memory GROUP BY lifecycle').fetchall())
print('  pinned:', conn.execute('SELECT count(*) FROM memory WHERE pinned=1').fetchone()[0])
print('  avg len (active):', conn.execute(
    "SELECT AVG(length(v.content)) FROM memory m JOIN memory_version v "
    "ON v.id=m.current_version_id WHERE m.lifecycle NOT IN ('rejected','disabled','superseded')"
).fetchone()[0])
print('  >280ch non-terminal:', conn.execute(
    "SELECT COUNT(*) FROM memory m JOIN memory_version v ON v.id=m.current_version_id "
    "WHERE length(v.content)>280 AND m.lifecycle NOT IN ('rejected','disabled','superseded')"
).fetchone()[0])
for r in conn.execute(
        "SELECT m.id, m.lifecycle, m.verification, length(v.content) FROM memory m "
        "JOIN memory_version v ON v.id=m.current_version_id WHERE m.pinned=1 "
        "ORDER BY length(v.content) DESC"):
    print('  pin %s %s/%s len=%s' % (r[0], r[1], r[2], r[3]))
conn.close()
print('DONE')
