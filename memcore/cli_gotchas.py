"""MemCore CLI — File-Anchored Gotchas and Hazards."""
from __future__ import annotations

import json
import sys

from . import gotchas
from .cli_support import _agent_identity_or_exit, _open_readonly, _project_or_exit
from .mcp_server import DEFAULT_AGENT
from .write_support import MemCoreError


def cmd_gotchas(args):
    conn = _open_readonly(args)
    try:
        project_ref = getattr(args, "project", None) or "default"
        project_id = _project_or_exit(conn, project_ref)
        if not project_id:
            sys.exit("error: project is required")
        agent_name = getattr(args, "agent", None) or DEFAULT_AGENT
        agent_id, _ = _agent_identity_or_exit(conn, agent_name)
        try:
            res = gotchas.get_file_gotchas(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                file_path=args.file,
                limit=getattr(args, "limit", 20),
            )
        except MemCoreError as e:
            sys.exit(f"error: {e}")

        if getattr(args, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return

        if res["total_count"] == 0:
            print(f"no recorded gotchas or tripwires for {res['file_path']}")
            return

        print(f"⚡ Hazards & Invariants for {res['file_path']} ({res['total_count']}):\n")

        if res["tripwires"]:
            print(f"[TRIPWIRES ({len(res['tripwires'])})]")
            for tw in res["tripwires"]:
                print(f"  • [{tw['id']}] [{tw['kind']}] Hazard: {tw['error_pattern']}")
                print(f"    Failed fix: {tw['failed_action']}")
                if tw["root_cause"]:
                    print(f"    Root cause: {tw['root_cause']}")
            print()

        if res["memories"]:
            print(f"[ANCHORED MEMORIES ({len(res['memories'])})]")
            for m in res["memories"]:
                stale_tag = " [STALE]" if m["stale"] else ""
                print(f"  • [{m['id']}] [{m['type']}]{stale_tag} {m['content']}")
            print()
    finally:
        conn.close()
