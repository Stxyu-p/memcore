#!/usr/bin/env python3
"""Setup a scratch copy with test project and agent for import testing.

The source store is read, never written, but it is named by a required --src
with no default on purpose: a hardcoded store path is how a scratch setup
quietly turns into an operation on the live store.
"""
import argparse
import shutil
import os
import sqlite3
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--src', required=True, help='store file to copy (read-only)')
args = parser.parse_args()

src = os.path.abspath(os.path.expanduser(args.src))
if not os.path.isfile(src):
    sys.exit('no such store: %s' % src)

local_app = os.environ.get('LOCALAPPDATA', '/tmp')
dst = os.path.join(local_app, 'memcore_import_test.db')

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

# Add test project and agent
conn = sqlite3.connect(dst)
conn.execute("INSERT OR IGNORE INTO project (id, name) VALUES ('proj-import', 'import_test')")
conn.execute("INSERT OR IGNORE INTO agent (id, name, profile_key) VALUES ('agent-test', 'testbot', 'testbot')")
conn.execute(
    "INSERT OR IGNORE INTO project_membership (project_id, agent_id, role) VALUES ('proj-import', 'agent-test', 'owner')"
)
conn.commit()
conn.close()

print(f'Scratch import test copy ready at {dst}')