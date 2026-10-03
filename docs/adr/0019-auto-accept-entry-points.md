# ADR 0019 — Auto-accept entry points

- **Status:** Accepted (2026-10-03, owner: P Choke)
- **Context:** ADR-0018 defines corroborated auto-accept. Two more entry
  points carry enough intent to skip `candidate` without corroboration.
- **Evidence:** explicit `memory_remember` calls and "จำไว้ว่า…" signals are
  deliberate durability requests, not ambient chatter.

## Decision

- **Explicit durable path → accepted directly**: deterministic
  `classify_user_text` explicit signals, `memory_remember` tool writes, and
  `memory_write/add` journal events create `lifecycle='accepted'` memories.
  Verification stays `unverified` (single source ≠ corroborated).
- **High-confidence semantic path → accepted directly**: semantic `remember`
  verdict with confidence ≥ 0.95 creates `accepted`. Below stays candidate.
- **Assistant observations never self-accept**: `post_llm_call` stays
  candidate-only. The assistant cannot promote its own claims.
- Every auto-accepted row still flows through `maybe_auto_corrob`, so a
  third/fifth independent source can lift it to accepted-with-evidence
  or Golden Rule later.

## Consequences

- Deliberate human/agent intent is respected immediately; ambient content
  still earns trust only by repetition.
- Audit reasons distinguish `explicit durable signal auto-accept` from
  `high-confidence semantic auto-accept` from `auto_corrob_accept`.
