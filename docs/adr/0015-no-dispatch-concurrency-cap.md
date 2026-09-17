# ADR 0015 — Concurrency: no dispatch cap, back off on 429/502

- **Status:** Accepted (2026-09-17)
- **Context:** ADR-0012 · **Supersedes:** the 2026-08-31 2–3 parallelism cap lesson
- **Evidence:** `python -m memcore supersede mem-36ab7921c349 ...` → `ver-8d57c1ee2edb`

## Status

Accepted. The live store's stale lesson is superseded; this ADR is the durable
canonical record.

## Context

On 2026-08-31, ten parallel `delegate_task` subagents through 9router
(glm-5.3-flash) all died with 429/502 within ~60 seconds. The initial lesson
was "cap parallelism at 2–3". On 2026-09-05, P Choke explicitly overrode that:
bot-to-bot and dispatch concurrency is unlimited — the gateway side is his to
manage, and pre-limiting throughput was costing more than the retries.

## Decision

Dispatch with **no concurrency cap**. When 429/502 does hit, the correct
reaction is back off and retry the failed tasks — not to pre-limit
parallelism. The old cap-only lesson stays as history via supersede; the
current rule lives in `mem-36ab7921c349` (superseding version) and here.

## Consequences

- Heavier bursts through 9router are accepted as a deliberate trade-off by
  the owner; do not re-litigate the cap without new information.
- Retry/back-off is the required failure path, and it must be visible in
  dispatch tooling instead of a silent serialization.
