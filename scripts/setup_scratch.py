#!/usr/bin/env python3
"""Setup a scratch copy of a MemCore store for testing.

The source store is read, never written, but it is named by a required
--src with no default on purpose: a hardcoded store path is how a scratch
setup quietly turns into an operation on the live store.
"""
import argparse
import shutil
import os
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--src', required=True, help='store file to copy (read-only)')
args = parser.parse_args()

src = os.path.abspath(os.path.expanduser(args.src))
if not os.path.isfile(src):
    sys.exit('no such store: %s' % src)

local_app = os.environ.get('LOCALAPPDATA', '/tmp')
dst = os.path.join(local_app, 'memcore_test.db')

if os.path.exists(dst):
    for suffix in ('', '-wal', '-shm'):
        try:
            os.unlink(dst + suffix)
        except OSError:
            pass

shutil.copy2(src, dst)
# Copy WAL/SHM if they exist
for suffix in ('-wal', '-shm'):
    try:
        shutil.copy2(src + suffix, dst + suffix)
    except OSError:
        pass

print(f'Scratch copy ready at {dst}')