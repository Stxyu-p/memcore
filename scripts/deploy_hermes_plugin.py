"""Deploy the Git-tracked MemCore Hermes plugin to the local Hermes plugin dir.

The repository copy under integrations/hermes/memcore is the source of truth.
This script copies only an explicit runtime allowlist, never __pycache__, tests,
or unknown files. Every deployed file is SHA-256 verified after replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import sys
import tempfile


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / 'integrations' / 'hermes' / 'memcore'
RUNTIME_FILES = (
    '__init__.py',
    'plugin.yaml',
    'plugin.py',
    'native_provider.py',
    'semantic_analyzer.py',
    'README.md',
)


def default_target() -> pathlib.Path:
    explicit = os.environ.get('MEMCORE_HERMES_PLUGIN_DIR')
    if explicit:
        return pathlib.Path(explicit).expanduser()
    hermes_home = os.environ.get('HERMES_HOME')
    if hermes_home:
        return pathlib.Path(hermes_home).expanduser() / 'plugins' / 'memcore'
    local = os.environ.get('LOCALAPPDATA')
    if local:
        return pathlib.Path(local) / 'hermes' / 'plugins' / 'memcore'
    return pathlib.Path.home() / '.hermes' / 'plugins' / 'memcore'


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def deployment_plan(target: pathlib.Path):
    plan = []
    for relative in RUNTIME_FILES:
        source = SOURCE_ROOT / pathlib.PurePosixPath(relative)
        destination = target / pathlib.PurePosixPath(relative)
        if not source.is_file():
            raise FileNotFoundError(f'canonical plugin source missing: {source}')
        source_hash = sha256(source)
        if not destination.is_file():
            state = 'missing'
            destination_hash = None
        else:
            destination_hash = sha256(destination)
            state = 'same' if destination_hash == source_hash else 'different'
        plan.append({
            'relative': relative,
            'source': source,
            'destination': destination,
            'source_hash': source_hash,
            'destination_hash': destination_hash,
            'state': state,
        })
    return plan


def _atomic_copy(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name + '.', suffix='.tmp', dir=str(destination.parent)
    )
    os.close(fd)
    temp_path = pathlib.Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        try:
            os.replace(temp_path, destination)
        except PermissionError:
            # Windows can deny rename/replace while a loader has an open handle
            # even though overwriting the file contents is still permitted.
            # Fall back narrowly, then rely on the mandatory SHA-256 verify.
            shutil.copyfile(source, destination)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def print_plan(plan, target: pathlib.Path) -> None:
    print(f'source: {SOURCE_ROOT}')
    print(f'target: {target}')
    for item in plan:
        print(f"  {item['state']:<9} {item['relative']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Deploy the Git-tracked MemCore Hermes plugin runtime.'
    )
    parser.add_argument(
        '--target', type=pathlib.Path, default=None,
        help='Hermes memcore plugin directory (default: local Hermes plugins/memcore)',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='verify installed runtime matches Git source; perform no writes')
    mode.add_argument('--dry-run', action='store_true',
                      help='show what would change; perform no writes')
    args = parser.parse_args(argv)

    target = (args.target or default_target()).expanduser().resolve(strict=False)
    plan = deployment_plan(target)
    print_plan(plan, target)
    changed = [item for item in plan if item['state'] != 'same']

    if args.check:
        if changed:
            print(f'check: OUT OF SYNC ({len(changed)} file(s))')
            return 1
        print('check: OK')
        return 0
    if args.dry_run:
        print(f'dry-run: {len(changed)} file(s) would change')
        return 0

    for item in changed:
        _atomic_copy(item['source'], item['destination'])

    verify = deployment_plan(target)
    mismatches = [item for item in verify if item['state'] != 'same']
    if mismatches:
        for item in mismatches:
            print(f"verify failed: {item['relative']}", file=sys.stderr)
        return 2
    print(f'deployed: {len(changed)} changed, {len(RUNTIME_FILES)} verified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
