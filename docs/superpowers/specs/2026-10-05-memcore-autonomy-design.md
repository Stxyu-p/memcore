# MemCore Autonomy Design — Seed Canon + Guarded Auto-Accept (Path 1)

- **Date:** 2026-10-05
- **Status:** Draft for P Choke review (not implemented)
- **Owner decision:** Path 1 — seed canon + publish flow (P Choke, 2026-10-05 chat)
- **Supersedes discussion in:** `docs/superpowers/specs/2026-10-03-memcore-v0.8-roadmap-draft.md` §2 P0-1 (chooses option (a)+(c) hybrid; option (b) evidence-counting deferred)
- **Standing ADRs:** 0016 (five critical pins), 0018 (corroboration N=3 / Golden N=5), 0019 (auto-accept entry points), 0020-era stdlib-only pitch
- **Explicitly NOT changed by this spec:** stdlib-only, SQLite+WAL+FTS5, daemonless, no-UI (owner 2026-10-03), recall p@3 ≥ 0.62 floor, `integrations/hermes/memcore/` → `deploy` + `--check` before any gate

## 1. Objective

Make corroboration fire for the first time without widening any trust rule silently.

- **C (fleet brain):** ≥10 fleet canon facts each held by ≥3 distinct agents → funnel `at_2 > 0`, then `at_3 > 0` with the first `auto_corrob_accept` audit row.
- **A (decide alone, with brakes):** low-risk auto-accept runs unattended; high-risk stays human-queued; every auto-mutation is audited and reversible.
- **Success =** `doctor` reports `health=ok` for one week with zero P Choke clicks, and recall baseline p@3 does not drop below 0.62.

## 2. Current state (FACTs, verified 2026-10-05)

- v0.8.0 closed: harness 327 OK (2 pre-existing expected failures), integration 98 OK, `doctor` exit 0 (`progress.md:25-31`).
- Carry-forward: corroboration funnel `at_2=0`; mika holds 84/97 memories (`progress.md:31`); CHANGELOG 0.8.0 §Known records 88 single-source fingerprints.
- Thresholds in code: `CORROBORATE_ACCEPT_N = 3`, `GOLDEN_N = 5`, `HIGH_CONFIDENCE_ACCEPT = 0.95` (`memcore/core.py:745-750`).
- Same claim = same `claim_fingerprint` (NFC-normalized sha256-16); sources = DISTINCT `owner_agent_id` in (`candidate`,`accepted`,`conflict`), tombstone-blocked excluded (ADR-0018).
- Golden = canonical copy gets `pinned=1, critical=1`; the only sanctioned pinned+critical lane besides the ADR-0016 five (ADR-0018 Consequences).
- `promote()` = private→project, owner-or-project-owner only, audited (`memcore/core.py:432-473`).
- `accept_memory()` = candidate/conflict→accepted, audited as `auto_accept`, tombstone-blocked refuses (`memcore/core.py:770-813`).
- Contradiction = `scan_contradictions()` read-only + `mark_contradiction()` separate governed step; propose-never-resolve (`memcore/core.py:667-707`).
- Owner withdrew cron 2026-10-03: backups stay manual; no silent auto-snapshot in this spec.

## 3. Design C — seed canon + publish flow (no schema change)

### 3.1 Canon set (~10 facts)

MIKA curates exactly ~10 facts the fleet already uses daily, each with memory ID + **verbatim canonical text**:

- The ADR-0016 five (SOUL v2.1 fleet rules; lnwjud bridge; `hermes profile use`; Discord REST wall; fleet roster).
- Plus five operational facts already in the store: 9router gateway mechanics; MemCore repo path + CLI surface; token-budget config; bot-to-bot dispatch ops rules (no cap, per-profile serialization, no `-m` override); fleet SOUL identity-drift sections.

P Choke approves the list once (one human click). That approval is the only human step in the whole design.

### 3.2 Fingerprint-convergence rule (load-bearing)

Corroboration keys on byte-identical normalized text. Different wording = different fingerprint = no count. Therefore:

- Every re-dispatch brief carries the **exact canonical sentence** each writer must store verbatim, plus the absolute repo path for context.
- Writers use the explicit durable path (`memory_remember` tool / `classify_user_text` explicit signal) so each copy lands `accepted, verification=unverified` per ADR-0019, then `maybe_auto_corrob` lifts it when the 3rd/5th distinct writer lands.
- Any pre-existing `private` copy of the same claim is moved via `promote()` (owner or project-owner identity only) — never by direct SQL (ADR-0016 documents the only sanctioned direct-SQL write; this spec adds none).
- Negative control (test): three agents writing *paraphrases* of one claim MUST NOT corroborate. The round-trip test asserts this.

### 3.3 Re-dispatch plan

- Writers: MIKA + NUA + SORA + MILIM (≥3 distinct `owner_agent_id` per claim; 4 gives headroom for one absence).
- Briefs in English, carrying: canonical sentence verbatim, project id, scope expectation (`remember` → private candidate or explicit durable → accepted per path), and the constraint "do not reword".
- Sequencing: one claim at a time per writer identity is unnecessary (distinct agents, distinct stores paths in test; production writes are one row each). In production the writes are cheap single rows — no concurrency cap applies per standing fleet rule.
- After each claim reaches 3 writers: `corroborate --apply` (existing CLI, `__main__.py:644`) promotes the canonical copy (oldest, project-scope preferred) to `accepted + source_backed` with audit `auto_corrob_accept`; at 5 writers, `auto_golden_promote` pins it critical.

## 4. Design A — auto-accept with brakes (no rule widened silently)

### 4.1 Allowed unattended (via `accept_memory`, audited)

| Entry | Condition | Audit reason |
|---|---|---|
| Corroboration | distinct writers ≥ 3, tombstone clear, contradiction pre-check clean | `auto_corrob_accept` |
| Golden | distinct writers ≥ 5, same pre-checks | `auto_golden_promote` |
| High-confidence semantic | `remember` verdict confidence ≥ 0.95, pre-check clean | `high-confidence semantic auto-accept` |
| Explicit durable signal | `memory_remember` / "จำไว้ว่า…" / `memory_write/add` journal event, pre-check clean | `explicit durable signal auto-accept` |
| Duplicate merge | same fingerprint already accepted & unblocked; new copy superseded into canonical | `duplicate-merge` (new audit string) |

### 4.2 Blocked from auto (stay candidate + human queue)

- `post_llm_call` assistant observations: candidate-only, forever (ADR-0019 — this spec does not touch it).
- Terminal lifecycles (`rejected`, `disabled`, `superseded`): `accept_memory` already refuses; no new path around it.
- Tombstone-blocked fingerprints: no-op, always.
- High-risk content (deletes, scope changes, verification upgrades, anything touching another agent's private rows): human queue only.

### 4.3 Contradiction gate on every auto-accept

Before any row in §4.1 commits, run the subject-key group check for that claim only (not a full sweep — keeps the write path cheap):

1. Compute the claim's subject key (`contradiction.subject_key`).
2. Test pairs within that group via `is_contradiction_pair`.
3. On hit: do NOT accept — `mark_contradiction` → both rows `conflict`, human queue, audit `contradiction-hold`.
4. On clean: proceed with §4.1.

Full `scan_contradictions` stays a scheduled read-only report, never on the hot path.

### 4.4 Veto window (reversibility, not a new UI)

- Every auto-mutation is reversible via existing `supersede`/`reject`/`restore`; tombstones keep veto power.
- During seed week MIKA reports daily counts (auto-accepted / golden / contradiction-holds / pending age) to P Choke in chat. No dashboard, no clicks required.
- Rollback drill (acceptance test): supersede one auto-accepted seed claim → fingerprint recounts from zero per ADR-0018; `doctor` stays green.

## 5. Observability delta (content-free, CLI-only)

Already shipped in 0.8.0: corroboration funnel in `doctor`. This spec adds only:

- `doctor` journal-age check: oldest pending in days (count exists; age trend does not — roadmap P0-3).
- `stats` golden + auto-promoted counts per day (autonomy visible as a rising number).
- All three content-free, matching `journal-stats` discipline. No dashboard, no new surface.

## 6. Explicitly out of scope

- Recall/ranking changes (direction B: Thai segmentation, hybrid ranker, negation detector) — separate spec, needs NUA capability matrix + benchmark guard.
- SORA audit F2 (dual recall), F3 (dual lifecycle), P0 test gaps — await the engineering audit, not smuggled in here.
- Evidence-counted corroboration (roadmap P0-1 option (b)) — deferred unless §3 fails to produce distinct writers in practice.
- Auto-snapshot/cron — excluded per owner 2026-10-03 withdrawal. Manual `backup --keep 14` only.
- Multi-provider abstraction, replacing SQLite, any UI.

## 7. Acceptance gates

1. `corroborate` funnel: `at_2 > 0` within seed week, `at_3 > 0` with ≥1 `auto_corrob_accept` audit row.
2. `doctor` exit 0, `health=ok`, 0 pending older than 7 days, for 7 consecutive days.
3. Recall baseline p@3 ≥ 0.62 (exact 1.00 / paraphrase 0.50 / negation 0.20 floor) — no recall code changes, so this is a guard, not a goal.
4. Suites green: harness 327 + integration 98 (or higher if new tests added), `deploy --check` in sync.
5. Negative controls green: paraphrase≠corroboration; terminal lifecycle un-acceptable; tombstone veto wins; contradiction-hit blocks auto-accept.
6. Rollback drill green (§4.4).

## 8. Test plan (new coverage)

- `test_seed_canon_roundtrip.py` (integration): 3 distinct test agents write the verbatim seed sentence → assert one `auto_corrob_accept` audit row, canonical copy `project/accepted/source_backed`; paraphrase trio → assert no promotion.
- `test_auto_accept_gates.py` (harness): table-driven over §4.1/§4.2 matrix incl. terminal-lifecycle refusal + tombstone veto (extends existing `test_dispatch_roundtrip.py` pattern — must use isolated store, never the live `~/.memcore/memory.db`).
- `test_contradiction_gate.py` (harness): seeded conflicting pair → auto-accept attempt → assert `conflict` + `contradiction-hold`, no `accepted` row.
- Mutation check on the gate: remove the pre-check call → at least one gate test must fail (follows the L4 activation-path precedent in `progress.md:20`).

## 9. Team split & sequencing

| Order | Owner | Work |
|---|---|---|
| 1 | MIKA | Curate the 10-claim canon list (IDs + verbatim text) → P Choke one-click approval |
| 2 | SORA | Implement §4.3 gate + §4.1 `duplicate-merge` audit string + §5 checks + §8 tests |
| 3 | MIKA | Re-dispatch seed writes to NUA/SORA/MILIM (English briefs, verbatim text) |
| 4 | MIKA | Daily seed-week report (counts only, content-free) |
| 5 | ALTIMA | Review gate per phase: spec → implementation → deployed-copy match → `doctor` green |

SORA's F2/F3/P0 and NUA's recall query-set work proceed on their own tracks; neither blocks this spec.

## 10. Risks

- **Wording drift breaks fingerprints** (writers paraphrase despite the brief) → mitigated by verbatim-text rule + negative-control test; fallback is option (b) evidence-counting, which needs a new ADR.
- **Golden over-pinning** (too many claims hit N=5 and flood every recall block) → mitigated by keeping the seed set at ~10 and the recall budget math from ADR-0016; `doctor` funnel makes the approach visible before it hurts.
- **Auto-accept of a confidently-wrong seed** → mitigated by the contradiction gate + tombstone veto + reversibility + daily report; blast radius is one claim, one `supersede` away from clean.
