"""MemCore CLI — governance knowledge topics (global catalog CRUD)."""
from __future__ import annotations

import json
from typing import Any

from . import store
from .topic import create_topic, delete_topic, get_topic, list_topics, update_topic


def _open(args: Any):
    return store.open_store(args.db)


def _print_topic(t: dict[str, Any], json_out: bool = False) -> None:
    if json_out:
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return
    print(f"{t['id']}  ({t['name']})")
    if t.get("description"):
        print(f"    {t['description']}")
    print(f"    parent: {t.get('parent_id') or '-'}")


def cmd_topic_create(args):
    conn = _open(args)
    try:
        t = create_topic(
            conn,
            args.topic_id,
            name=args.name,
            description=args.description,
        )
        _print_topic(t, args.json)
    finally:
        conn.close()


def cmd_topic_list(args):
    conn = _open(args)
    try:
        topics = list_topics(conn, parent_id=args.parent_id)
        if args.json:
            print(json.dumps(topics, ensure_ascii=False, indent=2))
            return
        for t in topics:
            _print_topic(t)
    finally:
        conn.close()


def cmd_topic_show(args):
    conn = _open(args)
    try:
        t = get_topic(conn, args.topic_id)
        if t is None:
            print(f"topic not found: {args.topic_id}")
            return
        _print_topic(t, args.json)
    finally:
        conn.close()


def cmd_topic_update(args):
    conn = _open(args)
    try:
        kwargs: dict[str, Any] = {}
        if args.name is not None:
            kwargs["name"] = args.name
        if args.description is not None:
            kwargs["description"] = args.description
        if args.parent_id is not None:
            kwargs["parent_id"] = args.parent_id
        if args.freshness_aging_days is not None:
            kwargs["freshness_aging_days"] = args.freshness_aging_days
        if args.freshness_stale_days is not None:
            kwargs["freshness_stale_days"] = args.freshness_stale_days
        t = update_topic(conn, args.topic_id, **kwargs)
        _print_topic(t, args.json)
    finally:
        conn.close()


def cmd_topic_delete(args):
    conn = _open(args)
    try:
        res = delete_topic(conn, args.topic_id, cascade=args.cascade)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"deleted {res['deleted']} topic(s), unlinked {res['memories_unlinked']} memory link(s)")
    finally:
        conn.close()
