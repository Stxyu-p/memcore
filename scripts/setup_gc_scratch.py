#!/usr/bin/env python3
"""Create a scratch copy with some old candidate memories for gc --apply testing.

The source store is read, never written, but it is named by a required --src
with no default on purpose: a hardcoded store path is how a scratch setup
quietly turns into an operation on the live store.
"""
import argparse
import shutil
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memcore import store, core

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--src', required=True, help='store file to copy (read-only)')
args = parser.parse_args()

src = os.path.abspath(os.path.expanduser(args.src))
if not os.path.isfile(src):
    sys.exit('no such store: %s' % src)

local_app = os.environ.get('LOCALAPPDATA', '/tmp')
dst = os.path.join(local_app, 'memcore_gc_test.db')

# Remove existing test db
if os.path.exists(dst):
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(dst + suffix)
        except OSError:
            pass

# Copy the live store
shutil.copy2(src, dst)
for suffix in ('-wal', '-shm'):
    try:
        shutil.copy2(src + suffix, dst + suffix)
    except OSError:
        pass

# Add a test project and agent, plus old candidate memories
conn = store.open_store(dst)

# Ensure the project exists
conn.execute("INSERT OR IGNORE INTO project (id, name) VALUES ('proj-ghost', 'ghost')")
conn.execute("INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES ('agent-ghost', 'ghost', 'ghost')")
conn.execute(
    "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) VALUES ('proj-ghost', 'agent-ghost', 'owner')"
)

# Add an old candidate memory (backdated 40 days ago, no evidence links)
mem_id, ver_id = core.create_memory(
    conn, 'proj-ghost', 'agent-ghost', 'old stale candidate memory with no evidence'
)
# Backdate it
conn.execute(
    "UPDATE memory SET created_at=datetime('now', '-40 days') WHERE id=?",
    (mem_id,)
)

# Add a rejected memory and backdate it (to test tombstone purge)
mem_id2, _ = core.create_memory(
    conn, 'proj-ghost', 'agent-ghost', 'to be rejected then tombstoned'
)
core.reject(conn, mem_id2, 'agent-ghost', 'obsolete claim')
tomb_id = conn.execute('SELECT id FROM tombstone ORDER BY created_at DESC LIMIT 1').fetchone()[0]
# Backdate the tombstone
conn.execute(
    "UPDATE tombstone SET created_at=datetime('now', '-100 days') WHERE id=?",
    (tomb_id,)
)

conn.commit()
conn.close()

print(f'GC test copy ready at {dst}')
print(f'  - candidate memory backdated 40 days: {mem_id}')
print(f'  - tombstone backdated 100 days: {tomb_id}')