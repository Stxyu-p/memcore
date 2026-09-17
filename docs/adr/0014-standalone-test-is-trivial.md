# ADR 0014 — Standalone `test` is a trivial turn

- **Status:** Accepted (2026-09-17)
- **Context:** ADR-0012 (semantic review admission) · **Amends:** trivial-input lane
- **Evidence:** commit `13cd241`, integration suite

## Status

Accepted, deployed with commit `13cd241`.

## Context

Probe turns like a bare `test`, greetings, or acknowledgements used to be
queued as `semantic_review_required` events. Each queued event later cost one
host-LLM review call even though there was nothing durable to extract — pure
waste on every probe turn, and it polluted the review queue.

## Decision

Classify standalone `test` in the same deterministic trivial lane as Thai
greetings. Trivial turns journal raw, generate **no** semantic review events,
and never reach the LLM.

## Consequences

- Probe turns cost zero LLM calls (one saved call per probe).
- The semantic queue only accumulates genuinely ambiguous turns.
- New trivial vocabulary is added to the classifier as we meet it — keep the
  list narrow; anything ambiguous must stay in the queue lane.
