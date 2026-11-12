"""MemCore — Trading Knowledge Pack.

A thin domain layer over the primitives that already exist: every record is a
governed memory carrying knowledge topics and subject/predicate/object triples.
No new tables, no parallel storage — the pack only fixes the *shape* of trading
knowledge (thesis → trade → review) so agents record it consistently.

External systems stay external: prices, orders and fills belong to a broker or
market-data service. What lands here is the reasoning, the decision and the
lesson — the part that must survive and be recallable later.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store
from .topic import create_topic, get_topic
from .write import create_memory
from .write_support import MemCoreError


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return store._table_exists(conn, table)

#: Canonical trading topic tree seeded on demand (idempotent).
TRADING_TOPICS: dict[str, str] = {
    "trading": "Market analysis, execution and risk knowledge",
    "trading/technical-analysis": "Price action, indicators, chart structure",
    "trading/fundamental-analysis": "Company, sector and macro fundamentals",
    "trading/strategies": "Entry/exit playbooks and the conditions they need",
    "trading/risk-management": "Position sizing, stops and exposure limits",
    "trading/journal": "Individual trades, executions and post-trade reviews",
}


def ensure_trading_topics(conn: sqlite3.Connection) -> list[str]:
    """Create the canonical trading topic tree if absent. Returns the slugs."""
    created = []
    for slug, description in TRADING_TOPICS.items():
        if get_topic(conn, slug) is None:
            create_topic(conn, slug, description=description)
        created.append(slug)
    return created


def record_thesis(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    symbol: str,
    thesis: str,
    *,
    invalidation: str | None = None,
    entry: str | None = None,
    risk: str | None = None,
    topic: str = "trading/strategies",
    lifecycle: str = "accepted",
) -> dict[str, Any]:
    """Record why a symbol is interesting, with the conditions that void it.

    Returns {'memory_id', 'symbol', 'topic', 'triples'}.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise MemCoreError("symbol is required")
    if not (thesis or "").strip():
        raise MemCoreError("thesis must be a non-empty claim")
    body = f"{symbol} thesis: {thesis.strip()}"
    if entry:
        body += f"\nEntry: {entry.strip()}"
    if invalidation:
        body += f"\nInvalidation: {invalidation.strip()}"
    if risk:
        body += f"\nRisk: {risk.strip()}"

    triples = [[symbol, "thesis", thesis.strip()]]
    if invalidation:
        triples.append([symbol, "invalidates_if", invalidation.strip()])
    if risk:
        triples.append([symbol, "risk", risk.strip()])

    mid, _ = create_memory(
        conn, project_id, agent_id, body,
        scope="project", memory_type="thesis", lifecycle=lifecycle,
        topics=[topic], triples=triples,
    )
    return {"memory_id": mid, "symbol": symbol, "topic": topic, "triples": len(triples)}


def record_trade(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    symbol: str,
    side: str,
    entry: str,
    *,
    exit_price: str | None = None,
    size: str | None = None,
    note: str | None = None,
    topic: str = "trading/journal",
    lifecycle: str = "accepted",
) -> dict[str, Any]:
    """Record an executed trade (the fact, not the order routing).

    Returns {'memory_id', 'symbol', 'side', 'triples'}.
    """
    symbol = (symbol or "").strip().upper()
    side = (side or "").strip().lower()
    if not symbol or not side:
        raise MemCoreError("symbol and side are required")
    if side not in ("buy", "sell"):
        raise MemCoreError("side must be 'buy' or 'sell'")
    if not str(entry or "").strip():
        raise MemCoreError("entry is required")

    body = f"{symbol} {side} at {entry}"
    if size:
        body += f" size {size}"
    if exit_price:
        body += f", exit {exit_price}"
    if note:
        body += f" — {note.strip()}"

    triples = [[symbol, "side", side], [symbol, "entry", str(entry)]]
    if exit_price is not None:
        triples.append([symbol, "exit", str(exit_price)])
    if size is not None:
        triples.append([symbol, "size", str(size)])

    mid, _ = create_memory(
        conn, project_id, agent_id, body,
        scope="project", memory_type="trade", lifecycle=lifecycle,
        topics=[topic], triples=triples,
    )
    return {"memory_id": mid, "symbol": symbol, "side": side, "triples": len(triples)}


def review_trade(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str,
    trade_memory_id: str,
    symbol: str,
    lesson: str,
    *,
    outcome: str | None = None,
    topic: str = "trading/journal",
    lifecycle: str = "accepted",
) -> dict[str, Any]:
    """Record the post-trade lesson, linked back to the trade it came from.

    The referenced trade must exist in this project; its symbol must match the
    review's symbol. Returns {'memory_id', 'trade_memory_id', 'symbol'}.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol or not (lesson or "").strip():
        raise MemCoreError("symbol and lesson are required")

    trade = conn.execute(
        "SELECT project_id FROM memory WHERE id = ?", (trade_memory_id,)
    ).fetchone()
    if trade is None or trade[0] != project_id:
        raise MemCoreError(
            f"trade memory not found in this project: {trade_memory_id}"
        )
    if _table_exists(conn, 'memory_triple'):
        # The trade's own triples carry its symbol as the subject.
        row = conn.execute(
            "SELECT subject FROM memory_triple WHERE memory_id = ? AND predicate = 'side' LIMIT 1",
            (trade_memory_id,),
        ).fetchone()
        if row is not None and str(row[0]).strip().upper() != symbol:
            raise MemCoreError(
                f"symbol mismatch: trade {trade_memory_id} is {row[0]}, review says {symbol}"
            )

    body = f"{symbol} review: {lesson.strip()}"
    if outcome:
        body += f"\nOutcome: {outcome.strip()}"

    triples = [[symbol, "review_of", trade_memory_id], [symbol, "lesson", lesson.strip()]]
    if outcome:
        triples.append([symbol, "outcome", outcome.strip()])

    mid, _ = create_memory(
        conn, project_id, agent_id, body,
        scope="project", memory_type="lesson", lifecycle=lifecycle,
        topics=[topic], triples=triples,
    )
    return {"memory_id": mid, "trade_memory_id": trade_memory_id, "symbol": symbol}
