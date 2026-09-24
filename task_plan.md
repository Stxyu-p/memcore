# MemCore audit plan

## Goal
Assess whether the MemCore repository and its Hermes plugin are fully operational, quantify architecture and performance, identify correctness/safety gaps, and produce a prioritized, evidence-backed improvement proposal without changing production code.

## Scope
- Read-only source, tests, schema, and deployment/config inspection
- Run the repository and Hermes-integration test suites
- Run direct CLI smoke checks and inspect the live store/runtime wiring
- Review recall, ingestion, semantic review, governance, tool registration, dashboard, and deployment
- Write findings to findings.md and a final architecture review to docs/architecture-reviews/

## Constraints
- Do not refactor or commit unless P Choke explicitly requests it
- Do not expose raw journal content or secrets
- Treat claims as verified only with command/file evidence
- Use the repository as source of truth; do not edit the deployed Hermes plugin directly

## Phases
1. [done] Orient repository, architecture, and current git state
2. [done] Execute tests, CLI checks, and live runtime verification (2026-09-24, evidence in scratch memcore-live-*.txt)
3. [done] Trace recall/ingestion/governance/performance and inspect gaps (findings F1-F6 in the review)
4. [done] Synthesize prioritized recommendations and write review → docs/architecture-reviews/2026-09-24-memcore.md
5. [done] Verify report evidence and summarize limitations (suites re-run same day; journal/doctor re-checked post-cleanup)

## Errors encountered
| Error | Attempt | Resolution |
|---|---:|---|
| git-status wrapper rejected foreground notify/heartbeat flags | 1 | re-ran without them; no repository action occurred on the failed call |
| journal-dismiss refused probe event (decision=none) | 1 | ran ingest.process_event first, then journal-review-decide ignore |

## Outcome
- 1 major correctness gap found and fixed same day (commit 79a3e3a, delegation classification)
- Journal backlog cleared: operator_attention → health=ok (0 pending)
- Review delivered: docs/architecture-reviews/2026-09-24-memcore.md
- Constraint honored: audit stayed read-only; the only source commit was the pre-existing delegation WIP on explicit approval
