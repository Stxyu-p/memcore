# ADR 0017 — Semantic auto-review throughput: 5 events per turn

- **Status:** Accepted (2026-09-17)
- **Context:** ADR-0012 · **Tunes:** `semantic.auto_review.max_events_per_turn`
- **Evidence:** config line 204 patched → YAML re-parse OK

## Status

Accepted, applied to `%LOCALAPPDATA%/hermes/config.yaml` on 2026-09-17. Takes
full effect after the next Hermes app restart (already pending for the plugin
runtime reload).

## Context

With `max_events_per_turn: 1`, the review backlog drained one event per turn —
too slow to converge; the pending queue sat at dozens of events (63 as of the
2026-09-17 audit). The engine already hard-validates the knob to 1..5 and
applies bounded batches with a failure circuit breaker
(`failure_threshold: 2`, `cooldown_seconds: 60`), so the blast radius of a
bad analyzer run is contained regardless of batch size.

## Decision

Raise `max_events_per_turn` from 1 to **5** (the engine's maximum). Queue
depth decays 5× faster; the circuit breaker still caps analyzer damage on
provider outages.

## Consequences

- Up to 5 LLM review calls can happen in one background memory-sync pass —
  cost is bounded by validation and the breaker, accepted by the owner.
- Expect the pending queue to drain within days at current turn volume; if
  steady-state inflow exceeds 5/turn, the answer is better deterministic
  classification upstream (ADR 0014), not a bigger knob.
