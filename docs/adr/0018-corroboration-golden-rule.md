# ADR 0018 — Corroboration thresholds and Golden Rule

- **Status:** Accepted (2026-10-03, owner: P Choke)
- **Context:** MemCore must be smart on its own. A claim repeated from
  independent sources should earn trust without a human clicking accept.
- **Evidence:** live store 2026-10-03: 74/106 memories stuck in `candidate`;
  journal back at `operator_attention`. Manual curation does not converge.

## Decision

- Same claim = same `claim_fingerprint` (existing NFC-normalized sha256-16).
- Sources = DISTINCT `owner_agent_id` holding that fingerprint in
  lifecycle (candidate, accepted, conflict), not tombstone-blocked.
- **N=3 distinct agents → auto-accept**: canonical copy (oldest,
  project-scope preferred) becomes `scope='project'`,
  `lifecycle='accepted'`, `verification='source_backed'`
  (never downgrades `user_authoritative`). Audit `auto_corrob_accept`.
- **N=5 distinct agents → Golden Rule**: canonical additionally gets
  `pinned=1, critical=1` — always injected in every recall block.
  Audit `auto_golden_promote`.
- Tombstone veto always wins: blocked fingerprint → no-op.
- `supersede` changes the fingerprint, so corrected claims recount from zero.
  No counter to reset by design.
- Non-canonical copies stay as-is; recall dedups by fingerprint (Part 3).

## Consequences

- Trust is earned by independent repetition, never by single-source volume.
- The 5 hand-picked canon rows (ADR-0016) coexist; Golden is the only other
  pinned+critical lane.
- Every auto-mutation is audited and reversible via supersede/reject.
