"""Phase 2: accept canonical, pin, shorten remaining long rows."""
import sys, os
sys.path.insert(0, 'C:/Users/BlankScreen/Workspace/memcore')
from memcore import store, core
from datetime import datetime

db = os.path.expanduser('~/.memcore/memory.db')
conn = store.open_store(db)
now = datetime.utcnow().isoformat() + 'Z'

# Fix 1: newly superseded items -> accepted + source_backed
fix_accept = ['mem-55bbd631306e', 'mem-9d04f45b254b', 'mem-12ece0327b39', 'mem-1cb3d6aaa6fa',
              'mem-2b503b02f898', 'mem-74b08adece91', 'mem-d6196a8ab6f3', 'mem-09b2c98f2a24',
              'mem-99e1895d8308', 'mem-7a13a0dfa455', 'mem-1e111b3f85b7', 'mem-a1176c6ea1f7',
              'mem-a4c0b2bc16d5', 'mem-2fcfcad97e5b', 'mem-6af3ac177392', 'mem-ff22afd0e5b9']
for mid in fix_accept:
    conn.execute("UPDATE memory SET lifecycle='accepted', verification='source_backed', updated_at=? WHERE id=?", (now, mid))
print('Accepted+verified', len(fix_accept), 'items')

# Fix 2: stable rows -> accepted
for mid in ['mem-0e5284903e23', 'mem-0fa8f83ae01c', 'mem-00cd4a9d1dff']:
    conn.execute("UPDATE memory SET lifecycle='accepted', verification='source_backed', updated_at=? WHERE id=?", (now, mid))
print('Accepted 3 stable rows')

# Fix 3: pin canonical facts
pins = ['mem-55bbd631306e', 'mem-9d04f45b254b', 'mem-1df34d262019', 'mem-68be1f685ef6',
        'mem-8d909e90be49', 'mem-bfe7663281e4', 'mem-6af3ac177392', 'mem-ff22afd0e5b9',
        'mem-c9e857056622', 'mem-6f95d1a4a2e5']
for mid in pins:
    conn.execute('UPDATE memory SET pinned=1, updated_at=? WHERE id=?', (now, mid))
print('Pinned', len(pins), 'canonical facts')

# Fix 4: shorten remaining long ones via supersede
shorts2 = [
    ('mem-1df34d262019',
     '9router is P Choke AI gateway at localhost:20128 (npm global 9router/app/custom-server.js) since 2026-08-16. '
     'Hermes uses provider custom Local with key_env HERMES_CUSTOM_LOCALHOST_20128_API_KEY. '
     'Embeddings verified 2026-08-26: gemini/gemini-embedding-001 = 3072 dims.'),
    ('mem-35615dda14b5',
     'NovelClaw semantics: MergeNovelMemory(existing,candidate,fresh) - fresh overwrites Role/Notes per episode; '
     'identity (name/Gender/Pronouns) stays curated. Glossary bootstraps when translation ends with empty glossary '
     '(ep 0160: 8 chars / 25 facts / 7 terms). Manual discover does not autosave.'),
    ('mem-68be1f685ef6',
     'Direct Discord REST: token = DISCORD_BOT_TOKEN from the Hermes .env file. Must set User-Agent header '
     'DiscordBot (see discord-api-docs) or Cloudflare returns 403 code 1010. '
     'discord_admin has no create_channel - use POST guilds channels + channels messages endpoints.'),
    ('mem-8d909e90be49',
     'Setting env var HERMES_PROFILE does not select a profile in this Hermes version - it overwrites the main config. '
     'Use hermes profile use NAME (sticky), then hermes profile use default to switch back. '
     'platforms.discord.home_channel must contain field platform discord or gateway crashes KeyError.'),
    ('mem-bfe7663281e4',
     'Hermes fleet = 5 agents (2026-08-31): MIKA = orchestrator (default), NUA = evidence, SORA = software, '
     'ALTIMA = review (cx gpt-5.6-terra-review, verdicts APPROVE REQUEST CHANGES REJECT + INCOMPLETE), '
     'MILIM = experience (joined late Aug).'),
    ('mem-fad4ba153e31',
     'ALTIMA created jointly 2026-08-21: NUA wrote SOUL.md + rubric, SORA wrote skill altima-review-methodology, '
     'MIKA created profile + verified e2e. Model via 9router. E2E passed: all 3 planted findings caught.'),
    ('mem-0ba895b3d8d9',
     'hermes-skill-factory (Romanescu11, 522 stars) installed 2026-08-18 as meta-skill only. '
     'plugin.py skipped (API outdated, hardcodes home dir wrong on Windows). Use via natural language.'),
    ('mem-263d6d9d419e',
     'browser-use CLI broken (BrowserStateRequestEvent ValueError even with CDP connected). '
     'Working path: headless chrome with remote-debugging-port, then raw WebSocket CDP from Node script.'),
]
for mid, short in shorts2:
    try:
        core.supersede(conn, mid, 'agent-mika', short, 'Shorten to fit recall budget.')
        print('SHORTENED', mid, '->', len(short))
    except Exception as e:
        print('ERR shorten', mid, e)

conn.commit()

print()
print('=== FINAL ===')
for r in conn.execute('SELECT lifecycle,COUNT(*) FROM memory GROUP BY lifecycle ORDER BY COUNT(*) DESC'):
    print('  %s: %s' % (r[0], r[1]))
for r in conn.execute('SELECT verification,COUNT(*) FROM memory GROUP BY verification ORDER BY COUNT(*) DESC'):
    print('  %s: %s' % (r[0], r[1]))
print('avg length (active):', conn.execute(
    'SELECT AVG(length(v.content)) FROM memory m JOIN memory_version v ON v.id=m.current_version_id '
    'WHERE m.lifecycle NOT IN ("rejected","disabled","superseded")').fetchone()[0])
print('pinned:', conn.execute('SELECT COUNT(*) FROM memory WHERE pinned=1').fetchone()[0])
print('>280ch non-terminal:', conn.execute(
    'SELECT COUNT(*) FROM memory m JOIN memory_version v ON v.id=m.current_version_id '
    'WHERE length(v.content)>280 AND m.lifecycle NOT IN ("rejected","disabled","superseded")').fetchone()[0])
print('tombstones:', conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()[0])
top = conn.execute(
    'SELECT m.id,length(v.content),substr(v.content,1,70),m.lifecycle,m.pinned FROM memory m '
    'JOIN memory_version v ON v.id=m.current_version_id '
    'WHERE m.lifecycle NOT IN ("rejected","disabled","superseded") '
    'ORDER BY m.pinned DESC,length(v.content) DESC LIMIT 12').fetchall()
print('Top rows:')
for r in top:
    print('  [%s] %s %sch [%s] %s' % ('PIN' if r[4] else '   ', r[0], r[1], r[3], r[2]))
conn.close()
