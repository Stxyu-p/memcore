"""MemCore storage — managed snapshots: create, prune, verify."""
import os
import pathlib
import re
import sqlite3
import time

from typing import Any

from .store_errors import (
    StoreError,
)




#: Backup freshness contract. A store with no recent verified backup has no
#: recovery path, which is exactly the condition that let a zero-filled
#: memory.db go unnoticed for months on 2026-10-03.
BACKUP_DIRNAME = 'backups'

BACKUP_MAX_AGE_DAYS = 7

BACKUP_MIN_COUNT = 3


#: Snapshot filenames this module owns: ``<store-stem>-<YYYYmmddTHHMMSSZ>[_<seq>].db``.
#: Anything else in the backup directory is a hand-made or legacy file and must
#: not be counted as a managed recovery point.
_SNAPSHOT_RE = re.compile(
    r'^(?P<stem>.+)-(?P<stamp>\d{8}T\d{6}Z)(?:_(?P<seq>\d+))?\.db$'
)



def _is_managed_snapshot(path: pathlib.Path, stem: str) -> bool:
    match = _SNAPSHOT_RE.match(path.name)
    return bool(match) and match.group('stem') == stem



def _managed_snapshots(backup_dir: pathlib.Path, stem: str):
    """Snapshots written by ``backup_store``, newest first, by modification time."""
    if not backup_dir.is_dir():
        return []
    candidates = [
        p for p in backup_dir.glob('*.db')
        if p.is_file() and _is_managed_snapshot(p, stem)
    ]
    return sorted(
        candidates,
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )



def backup_store(source_db: str, *, keep: int = 14) -> pathlib.Path:
    """Snapshot a store with SQLite's own online backup API.

    Uses ``Connection.backup`` rather than a file copy: it is transaction-safe
    against concurrent writers and produces a consistent image even while WAL
    traffic is in flight. A plain ``shutil.copy`` of a WAL-mode database can
    capture a torn state.

    The snapshot is written to a temporary file, integrity-checked, then moved
    into place, so a failure never leaves a corrupt file in the backup dir.
    """
    src_path = pathlib.Path(source_db).expanduser()
    if not src_path.is_file():
        raise StoreError(f'store does not exist: {src_path}')
    backup_dir = src_path.parent / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    dest = backup_dir / f'{src_path.stem}-{stamp}.db'
    if dest.exists():
        seq = 1
        while (backup_dir / f'{src_path.stem}-{stamp}_{seq}.db').exists():
            seq += 1
        dest = backup_dir / f'{src_path.stem}-{stamp}_{seq}.db'
    tmp = dest.with_suffix('.db.partial')

    src = sqlite3.connect(str(src_path), timeout=10, isolation_level=None)
    try:
        dst = sqlite3.connect(str(tmp), timeout=10, isolation_level=None)
        try:
            src.backup(dst)
            row = dst.execute('PRAGMA integrity_check').fetchone()
            if not row or row[0] != 'ok':
                raise StoreError(
                    f'backup snapshot failed integrity check: {row[0] if row else "no result"}'
                )
        finally:
            dst.close()
        os.replace(tmp, dest)
    except Exception:
        for leftover in (tmp,):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    finally:
        src.close()

    _prune_backups(backup_dir, keep, src_path.stem)
    return dest




def _prune_backups(backup_dir: pathlib.Path, keep: Any, stem: str) -> None:
    """Retain the ``keep`` newest managed snapshots; never touch other files."""
    if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
        return
    for stale in _managed_snapshots(backup_dir, stem)[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass



def verify_backups(source_db: str, *, max_age_days: Any = BACKUP_MAX_AGE_DAYS,
                   min_count: Any = BACKUP_MIN_COUNT) -> dict:
    """Content-free recovery-readiness report for a store's backup set.

    ``recovery_ready`` means exactly one thing: a verified snapshot of this store
    exists and is younger than ``max_age_days``. Snapshot count is reported but
    deliberately does NOT gate readiness — every snapshot passes an integrity
    check at creation time, so one good snapshot is a usable recovery path.
    Requiring several would make a freshly provisioned store permanently
    "unhealthy" for no real gain.

    Only files this module wrote are counted, so a hand-placed .bak in the
    directory cannot make a store look recoverable. Never reads memory text.
    """
    src_path = pathlib.Path(source_db).expanduser()
    backup_dir = src_path.parent / BACKUP_DIRNAME
    report = {
        'backup_dir': str(backup_dir),
        'backup_dir_exists': backup_dir.is_dir(),
        'snapshot_count': 0,
        'newest_snapshot': None,
        'newest_age_days': None,
        'oldest_age_days': None,
        'max_age_days': max_age_days,
        'min_count': min_count,
        'recovery_ready': False,
        'problems': [],
    }
    if not report['backup_dir_exists']:
        report['problems'].append('no_backup_dir')
        return report

    snapshots = _managed_snapshots(backup_dir, src_path.stem)
    report['snapshot_count'] = len(snapshots)
    if not snapshots:
        report['problems'].append('no_snapshots')
        return report

    now = time.time()
    ages = []
    for path in snapshots:
        try:
            ages.append((now - path.stat().st_mtime) / 86400.0)
        except OSError:
            continue
    if not ages:
        report['problems'].append('unreadable_snapshot_metadata')
        return report

    report['newest_age_days'] = round(min(ages), 2)
    report['oldest_age_days'] = round(max(ages), 2)
    report['newest_snapshot'] = snapshots[0].name

    eff_max_age = (
        max_age_days
        if isinstance(max_age_days, (int, float)) and not isinstance(max_age_days, bool) and max_age_days > 0
        else BACKUP_MAX_AGE_DAYS
    )
    eff_min_count = (
        min_count
        if isinstance(min_count, int) and not isinstance(min_count, bool) and min_count >= 1
        else BACKUP_MIN_COUNT
    )

    if min(ages) > eff_max_age:
        report['problems'].append(f'stale_backup:{report["newest_age_days"]}d')
        report['recovery_ready'] = False
    else:
        report['recovery_ready'] = True
        if len(snapshots) < eff_min_count:
            # Informational only: a verified snapshot is a recovery path on its
            # own; the target count matters for depth, not for recoverability.
            report['problems'].append(f'below_target_snapshots:{len(snapshots)}')
    return report
