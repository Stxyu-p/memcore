# ADR 0013 — Critical pins as the only recall injection lane

- **Status:** Accepted (2026-09-17)
- **Context:** ADR-0012 · **Supersedes:** the old all-pins-bypass rule
- **Evidence:** commit `13cd241`, live-store probe, 243-test gate

## Status

Accepted, deployed to the live Hermes plugin and verified byte-for-byte by
`scripts/deploy_hermes_plugin.py --check`.

## Context

Recall used to force-inject every pinned row into every prefetch. On the live
store, 8 unrelated pins consumed 919 of a 1200-character budget on queries that
had nothing to do with them, starving the facts that actually matched the
query. Pins were meant to be a guarantee, not a sieve for everything.

## Decision

Only rows that are **pinned AND critical** bypass query relevance. Ordinary
pins compete in ranked hits. Search over-fetches by the number of injected
critical pins so pin-duplicates cannot shadow real hits, and the recall block
stays hard-capped at the configured budget.

## Consequences

- On-topic queries rank their facts first; unrelated pins never consume the
  budget of unrelated questions (proven on the live store: 919 → 0 chars on
  the Thai gateway-port probe, on-topic answers unchanged).
- Ordinary pins can now rank lower than a fresh, relevant hit — intended.
- Filling all 5 critical slots is a deliberate curation act (see ADR 0016).
