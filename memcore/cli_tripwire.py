"""MemCore CLI — Tripwire Registry."""
from __future__ import annotations

import sys

from . import tripwire
from .cli_support import (
    _agent_identity_or_exit,
    _open,
    _open_readonly,
    _project_or_exit,
)
from .write_support import MemCoreError


def cmd_tripwire_arm(args):
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, getattr(args, "project", None))
        if not project_id:
            sys.exit("error: project is required")
        agent_id, agent_exists = _agent_identity_or_exit(conn, getattr(args, "agent", None))
        if not agent_exists:
            sys.exit(f"error: agent {agent_id} does not exist; create it first")
        try:
            tw_id = tripwire.arm_tripwire(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                error_pattern=args.error,
                failed_action=args.failed,
                root_cause=getattr(args, "root_cause", None),
                file_path=getattr(args, "file", None),
                kind=getattr(args, "kind", "break_loop"),
            )
        except MemCoreError as e:
            sys.exit(f"error: {e}")
        print(f"tripwire armed: {tw_id}")
    finally:
        conn.close()


def cmd_tripwire_check(args):
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, getattr(args, "project", None))
        if not project_id:
            sys.exit("error: project is required")
        try:
            rows = tripwire.check_tripwires(
                conn,
                project_id=project_id,
                query=getattr(args, "query", None),
                file_path=getattr(args, "file", None),
                kind=getattr(args, "kind", None),
                limit=getattr(args, "limit", 10),
                include_disarmed=getattr(args, "all", False),
            )
        except MemCoreError as e:
            sys.exit(f"error: {e}")

        if not rows:
            print("no matching tripwires found")
            return

        for r in rows:
            status = "DISARMED" if r["disarmed_at"] else "ARMED"
            kind_tag = f"[{r['kind']}]"
            file_tag = f" ({r['file_path']})" if r["file_path"] else ""
            print(f"{r['id']} {kind_tag} [{status}]{file_tag}")
            print(f"  Hazard / Error : {r['error_pattern']}")
            print(f"  Failed action  : {r['failed_action']}")
            if r["root_cause"]:
                print(f"  Root cause     : {r['root_cause']}")
            if r["disarmed_at"]:
                print(f"  Disarmed       : {r['disarmed_at']} — {r['disarmed_reason']}")
            print()
    finally:
        conn.close()


def cmd_tripwire_disarm(args):
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, getattr(args, "project", None))
        if not project_id:
            sys.exit("error: project is required")
        agent_id, agent_exists = _agent_identity_or_exit(conn, getattr(args, "agent", None))
        if not agent_exists:
            sys.exit(f"error: agent {agent_id} does not exist")
        try:
            disarmed = tripwire.disarm_tripwire(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                tripwire_id=args.tripwire_id,
                reason=args.reason,
            )
        except MemCoreError as e:
            sys.exit(f"error: {e}")
        if disarmed:
            print(f"tripwire {args.tripwire_id} disarmed")
        else:
            print(f"tripwire {args.tripwire_id} already disarmed")
    finally:
        conn.close()
