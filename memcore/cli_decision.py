"""MemCore CLI — governed decision records (Tier-1 higher layer)."""
import json
import sys

from . import core, decision
from .cli_support import (
    _agent_identity_or_exit,
    _open,
    _open_readonly,
    _project_or_exit,
)
from .write_support import MemCoreError


def cmd_decision_create(args):
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {agent_id} does not exist; create it first')
        options = None
        if getattr(args, 'options', None):
            try:
                options = json.loads(args.options)
            except json.JSONDecodeError:
                sys.exit('error: --options must be valid JSON')
        try:
            dec_id = decision.create_decision(
                conn,
                project_id=project_id,
                agent_id=agent_id,
                title=args.title,
                rationale=args.rationale,
                scope=args.scope,
                context=getattr(args, 'context', None),
                options=options,
                chosen=getattr(args, 'chosen', None),
                consequences=getattr(args, 'consequences', None),
                status=getattr(args, 'status', 'proposed'),
                lifecycle=getattr(args, 'lifecycle', 'candidate'),
            )
        except MemCoreError as e:
            sys.exit(f'error: {e}')
        print(f'decision created: {dec_id}')
    finally:
        conn.close()


def cmd_decision_list(args):
    conn = _open_readonly(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {agent_id} does not exist')
        rows = decision.recall_decisions(
            conn, project_id=project_id, agent_id=agent_id,
            query=getattr(args, 'query', None),
            status=getattr(args, 'status', None),
            lifecycle=getattr(args, 'lifecycle', None),
            limit=getattr(args, 'limit', 20),
        )
    finally:
        conn.close()
    if not rows:
        print('(no decisions)')
        return
    for r in rows:
        print(f"[{r['id']}] ({r['status']}/{r['lifecycle']}) {r['title']}")
        print(f"    rationale: {r['rationale'][:120]}")


def cmd_decision_show(args):
    conn = _open_readonly(args)
    try:
        agent_id, _ = _agent_identity_or_exit(conn, args.agent)
        d = decision.get_decision(conn, args.decision_id, agent_id)
    finally:
        conn.close()
    if d is None:
        print('(not found)')
        return
    for k in ('id', 'title', 'status', 'lifecycle', 'verification', 'freshness',
              'context', 'chosen', 'rationale', 'consequences', 'created_at'):
        v = d.get(k)
        if v is not None:
            print(f"{k}: {v}")
    if d.get('options'):
        print(f"options: {json.dumps(d['options'], ensure_ascii=False)}")
    if d.get('links'):
        print(f"links: {json.dumps(d['links'], ensure_ascii=False)}")


def cmd_decision_supersede(args):
    conn = _open(args)
    try:
        agent_id, agent_exists = _agent_identity_or_exit(conn, args.agent)
        if not agent_exists:
            sys.exit(f'error: agent {agent_id} does not exist')
        try:
            new_id = decision.supersede_decision(
                conn, args.old_id, agent_id, args.title, args.rationale,
                reason=getattr(args, 'reason', None),
            )
        except MemCoreError as e:
            sys.exit(f'error: {e}')
        print(f'superseded: {args.old_id} -> {new_id}')
    finally:
        conn.close()
