"""MemCore CLI — Trading Knowledge Pack (thesis / trade / review)."""
from __future__ import annotations

import json
from typing import Any

from . import store
from .cli_support import _agent_identity_or_exit, _project_or_exit
from .trading import ensure_trading_topics, record_thesis, record_trade, review_trade
from .write_support import MemCoreError


def _open(args: Any):
    return store.open_store(args.db)


def _out(res: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    for k, v in res.items():
        print(f"{k}: {v}")


def _run(args: Any, body) -> None:
    """Open the store, resolve identity, run the pack call, print the result."""
    conn = _open(args)
    try:
        project_id = _project_or_exit(conn, args.project)
        agent_id, exists = _agent_identity_or_exit(conn, args.agent)
        if not exists:
            raise SystemExit(f"error: agent {agent_id} does not exist; create it first")
        ensure_trading_topics(conn)
        res = body(conn, project_id, agent_id)
        conn.commit()
        _out(res, args.json)
    except MemCoreError as e:
        raise SystemExit(f"error: {e}")
    finally:
        conn.close()


def cmd_trade_thesis(args):
    _run(args, lambda conn, pid, aid: record_thesis(
        conn, pid, aid, args.symbol, args.thesis,
        invalidation=args.invalidation, entry=args.entry, risk=args.risk))


def cmd_trade_record(args):
    _run(args, lambda conn, pid, aid: record_trade(
        conn, pid, aid, args.symbol, args.side, args.entry,
        exit_price=args.exit_price, size=args.size, note=args.note))


def cmd_trade_review(args):
    _run(args, lambda conn, pid, aid: review_trade(
        conn, pid, aid, args.trade_memory_id, args.symbol, args.lesson,
        outcome=args.outcome))
