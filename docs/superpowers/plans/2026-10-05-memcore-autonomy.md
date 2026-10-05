# MemCore Autonomy (Seed Canon + Guarded Auto-Accept) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make corroboration fire for the first time via a seeded canon set, with every auto-accept passing a contradiction pre-check.

**Architecture:** No schema change. One new read-only helper in `core.py` (`pre_accept_conflict_check`) called from the existing auto-accept entries; two content-free observability additions (`doctor` journal-age, `stats` autonomy counts); seed canon is operational (curate + re-dispatch), not code.

**Tech Stack:** Python stdlib only, SQLite + WAL + FTS5, pytest (harness + integration suites).

**Spec:** `docs/superpowers/specs/2026-10-05-memcore-autonomy-design.md` — the plan argues from the spec, so the spec travels with it; executors read both.

## Global Constraints

- Stdlib only — no new dependency, no embeddings, no network on any path.
- SQLite + WAL + FTS5 stays the store; daemonless; no UI of any kind (owner 2026-10-03).
- Recall baseline floor: p@3 >= 0.62 (exact 1.00 / paraphrase 0.50 / negation 0.20) — guard, not goal.
- `post_llm_call` assistant observations stay candidate-only forever (ADR-0019) — no task widens this.
- Terminal lifecycles (`rejected`, `disabled`, `superseded`) are never auto-acceptable; tombstone veto always wins.
- After ANY edit under `integrations/hermes/memcore/`, run `python scripts/deploy_hermes_plugin.py` then `--check` before any gate.
- Tests must use an isolated store — never write the live `~/.memcore/memory.db` (follow `integrations/hermes/memcore/tests/test_dispatch_roundtrip.py` fixture pattern).
- Content-free discipline: `doctor` / `stats` / `journal-stats` outputs never carry memory text, only counts, ages, fingerprints.
- No auto-snapshot / cron in this plan (owner withdrew cron 2026-10-03); manual `backup --keep 14` only.

## Review Focus

- Thai glued negatives (`ห้ามใช้` vs `ใช้`) sharing a subject key — expect `polarity` hit, never silent accept.
- Numeric claims (`port 20128` vs `port 8080`) on one subject — expect `numeric` hold even when wording is otherwise identical.
- Claims with empty `subject_key` (pure numbers / particles only) — expect gate to fail closed (hold), not skip.
- Gate race: tombstone lands between pre-check and commit — expect commit to refuse via existing tombstone check, no `accepted` row.
- Golden over-pinning: seed set at ~10 must not push recall-block canon past budget — expect funnel + recall baseline green.

---

### Task 1: Contradiction pre-check helper in `core.py`

**Files:**
- Modify: `memcore/core.py` (add `pre_accept_conflict_check` near `scan_contradictions`, ~`core.py:667`)
- Test: `harness/test_contradiction_gate.py` (new)

**Interfaces:**
- Consumes: `contradiction.subject_key(content, max_tokens=4) -> str`, `contradiction.is_contradiction_pair(a, b) -> tuple[bool, str]` (`memcore/contradiction.py:49,116`).
- Produces: `core.pre_accept_conflict_check(conn, project_id, content: str, exclude_memory_id: str | None = None) -> list[tuple[str, str]]` returning `[(live_memory_id, reason)]`; empty list = clean. Later tasks call exactly this.

- [ ] **Step 1: Write failing tests** in `harness/test_contradiction_gate.py`: `test_precheck_polarity_hold` (seed `ใช้ X` + candidate `ห้ามใช้ X` → non-empty, reason `polarity`), `test_precheck_numeric_hold` (`port 20128` vs `port 8080` → reason `numeric`), `test_precheck_clean` (unrelated subjects → `[]`), `test_precheck_empty_key_holds` (content whose `subject_key` is `''` → non-empty hold, never `[]`-skip).
- [ ] **Step 2: Run to verify they fail.** Run: `python -m pytest harness/test_contradiction_gate.py -v`. Expected: FAIL (name not defined).
- [ ] **Step 3: Implement `pre_accept_conflict_check(conn, project_id, content, exclude_memory_id=None)` in `memcore/core.py`.** Read-only: compute `subject_key(content)`; empty key → return `[('__empty_subject__', 'empty_subject_hold')]`; else load live (`candidate`,`accepted`) rows of this project with their current-version content, keep same-key rows (minus `exclude_memory_id`), test each with `is_contradiction_pair`, collect hits. No writes, no lifecycle change.
- [ ] **Step 4: Run tests.** Run: `python -m pytest harness/test_contradiction_gate.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git add memcore/core.py harness/test_contradiction_gate.py && git commit -m "feat(core): pre-accept contradiction check (read-only)"`

### Task 2: Wire the gate into every auto-accept entry

**Files:**
- Modify: `memcore/ingest.py` (explicit durable lane ~`ingest.py:691-706`, semantic high-confidence lane ~`ingest.py:314-331`), `memcore/core.py` (`maybe_auto_corrob`, `core.py:842`)
- Test: extend `harness/test_contradiction_gate.py`

**Interfaces:**
- Consumes: Task 1 `core.pre_accept_conflict_check`; existing `core.accept_memory(conn, memory_id, agent_id, reason, _manage_transaction=True)` (`core.py:770`), `core.mark_contradiction(conn, memory_id_a, memory_id_b, agent_id, reason, _manage_transaction=True)` (`core.py:707`).
- Produces: gate behavior — on hit: no `accepted` row, both rows `conflict` when the actor may mark, else candidate + audit `contradiction-hold`. No new exception type.

- [ ] **Step 1: Write failing tests** in `harness/test_contradiction_gate.py`: `test_explicit_lane_blocked` (seed live `ใช้ X`, ingest explicit durable `ห้ามใช้ X` → no `accepted`, audit `contradiction-hold` present), `test_semantic_lane_blocked` (same via confidence 0.99 path), `test_corrob_blocked` (third writer completing a set while a live contradiction exists → `maybe_auto_corrob` returns `contradiction_hold`, no `auto_corrob_accept` row), `test_tombstone_between_precheck_and_commit` (pre-check clean, then tombstone created for the fingerprint before commit → commit refuses, no `accepted` row: tombstone veto wins regardless of gate outcome).
- [ ] **Step 2: Run to verify they fail.** Run: `python -m pytest harness/test_contradiction_gate.py -v`. Expected: FAIL (new behavior absent).
- [ ] **Step 3: Implement the gate** at the top of each entry (explicit lane, semantic lane, `maybe_auto_corrob` before the canonical promotion at `core.py:874`): call `pre_accept_conflict_check`; on non-empty, try `mark_contradiction(new_id, hit_id, actor, reason)`; on `PermissionDenied` (actor cannot mark another owner's row) leave rows as-is; either way `_audit(conn, 'contradiction-hold', actor, new_id, project_id, {'hits': hits})` and return without accepting. Tombstone check stays where it is — gate adds to it, never replaces it.
- [ ] **Step 4: Run tests.** Run: `python -m pytest harness/test_contradiction_gate.py harness/test_corroboration_golden.py -v`. Expected: PASS, no existing golden test broken.
- [ ] **Step 5: Commit.** `git add memcore/ingest.py memcore/core.py harness/test_contradiction_gate.py && git commit -m "feat(ingest): contradiction gate on all auto-accept entries"`

### Task 3: `duplicate-merge` audit string on dedup paths

**Files:**
- Modify: `memcore/ingest.py` (semantic duplicate ~`ingest.py:300-312`, native duplicate ~`ingest.py:677-689`)
- Test: `harness/test_auto_accept_gates.py` (new, table-driven)

**Interfaces:**
- Consumes: existing `_audit(conn, action, actor, memory_id=None, project_id=None, detail=None)` (`core.py:56`), `core.fingerprint(content) -> str` (`core.py:40`).
- Produces: audit action string `duplicate-merge`Queryable in Task 4 counts; nothing else reads it.

- [ ] **Step 1: Write failing tests** in `harness/test_auto_accept_gates.py`: table over (explicit durable → `private_accepted`), (semantic ≥0.95 → `semantic_private_accepted`), (semantic <0.95 → candidate), (duplicate same fingerprint → `duplicate` + `duplicate-merge` audit, no new memory row), (terminal lifecycle accept attempt → raises, no state change), (tombstone-blocked accept → refuses).
- [ ] **Step 2: Run to verify they fail.** Run: `python -m pytest harness/test_auto_accept_gates.py -v`. Expected: FAIL (`duplicate-merge` audit missing).
- [ ] **Step 3: Implement** — in both dedup branches, after the existing `ingest_derivation` insert, add `_audit` via `core._audit(conn, 'duplicate-merge', agent_id, existing, project_id, {'event_id': event_id})` inside the same transaction. No lifecycle change, no new row.
- [ ] **Step 4: Run tests.** Run: `python -m pytest harness/test_auto_accept_gates.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git add memcore/ingest.py harness/test_auto_accept_gates.py && git commit -m "feat(ingest): audit duplicate-merge on dedup paths"`

### Task 4: Observability — `doctor` journal-age + `stats` autonomy counts

**Files:**
- Modify: `memcore/__main__.py` (`cmd_doctor`, ~`__main__.py:1056`; `cmd_stats`, ~`__main__.py:493`), backing queries beside `ingest.journal_stats` (`ingest.py:741`) and `store.corroboration_funnel` (`store.py:698`)
- Test: `harness/test_cli.py` (extend) or new `harness/test_observability.py`

**Interfaces:**
- Consumes: `ingest_event(created_at, status, decision)`, `audit_event(action, created_at)` tables; audit strings `auto_corrob_accept`, `auto_golden_promote`, `explicit durable signal auto-accept`, `high-confidence semantic auto-accept`, `duplicate-merge`, `contradiction-hold`.
- Produces: `doctor` report key `journal_age: {oldest_pending_days: int | None}`; `stats` key `autonomy_per_day: {YYYY-MM-DD: {action: count}}`. Both content-free.

- [ ] **Step 1: Write failing tests**: `test_doctor_journal_age` (seed one 9-day-old pending + one fresh → `oldest_pending_days == 9`), `test_stats_autonomy_counts` (seed audit rows across 2 days → per-day counts match, no content strings in output).
- [ ] **Step 2: Run to verify they fail.** Run: `python -m pytest harness/test_observability.py -v` (or `-k "journal_age or autonomy"` if extended in `test_cli.py`). Expected: FAIL (keys absent).
- [ ] **Step 3: Implement** — `journal_age`: `SELECT MIN(created_at) FROM ingest_event WHERE status='pending'`, convert to whole days vs `_now()`, `None` when no pending; print one line in `cmd_doctor` beside the journal line. `autonomy_per_day`: `SELECT date(created_at), action, COUNT(*) FROM audit_event WHERE action IN (...) GROUP BY 1,2`; attach under `cmd_stats`. No memory text selected anywhere.
- [ ] **Step 4: Run tests.** Run: `python -m pytest harness/test_observability.py harness/test_cli.py harness/test_journal_cli.py -v`. Expected: PASS.
- [ ] **Step 5: Commit.** `git add memcore/__main__.py harness/test_observability.py && git commit -m "feat(obs): doctor journal-age plus stats autonomy counts"`

### Task 5: Acceptance tests — seed round-trip, negative controls, rollback drill, recall guard

**Files:**
- Create: `integrations/hermes/memcore/tests/test_seed_canon_roundtrip.py`
- Test data: one verbatim seed sentence (fixed in the test, e.g. `"MemCore seed canon probe 2026-10-05: corroboration fires on verbatim repetition."`)

**Interfaces:**
- Consumes: Tasks 1–3 behavior; `core.maybe_auto_corrob`, `core.supersede` (`core.py:331`), recall baseline `harness/test_recall_baseline.py`.
- Produces: the spec §7 acceptance evidence. No production code.

- [ ] **Step 1: Write failing tests**: `test_verbatim_trio_corrobates` (3 distinct test agents write the verbatim sentence via the explicit durable path → exactly one `auto_corrob_accept` audit row; canonical copy is `scope=project, lifecycle=accepted, verification=source_backed`), `test_paraphrase_trio_does_not` (3 paraphrases → no promotion), `test_supersede_resets_count` (supersede the canonical → funnel recounts from zero for that fingerprint, `doctor` green), `test_recall_floor` (recall baseline still p@3 >= 0.62).
- [ ] **Step 2: Run to verify the positive test fails pre-seed.** Run: `python -m pytest integrations/hermes/memcore/tests/test_seed_canon_roundtrip.py -v`. Expected: FAIL (no corroboration path exercised yet in test store — documents the gap, not the code).
- [ ] **Step 3: Make them pass with the Task 1–4 code** (no new production code in this task — fix test wiring only: isolated store fixture per `test_dispatch_roundtrip.py`, distinct `agent-...` identities, explicit-durable write path).
- [ ] **Step 4: Run the full gate.** Run: `python -m pytest harness integrations/hermes/memcore/tests -q`, then `python scripts/deploy_hermes_plugin.py --check`, then `python -m memcore doctor` (exit 0). Expected: green across all three.
- [ ] **Step 5: Commit.** `git add integrations/hermes/memcore/tests/test_seed_canon_roundtrip.py && git commit -m "test: seed canon round-trip plus acceptance controls"`

### Task 6: Seed canon curation + fleet re-dispatch (operational, MIKA-owned)

**Files:**
- Create: `docs/superpowers/specs/2026-10-05-seed-canon-list.md` (10 rows: memory ID + verbatim canonical text + owning project)
- No production code changes in this task.

**Interfaces:**
- Consumes: ADR-0016 five + five operational facts (§3.1 of the spec); `corroborate --apply` (`__main__.py:644`).
- Produces: P Choke one-click approval artifact; English re-dispatch briefs (verbatim text, "do not reword") for NUA / SORA / MILIM.

- [ ] **Step 1: Curate the 10-claim list** with exact IDs + verbatim text copied from the live store (read-only queries, no writes).
- [ ] **Step 2: P Choke approval** — one reply approving the list as-is or with edits.
- [ ] **Step 3: Re-dispatch seed writes** (English briefs per writer, one claim batch at a time), then `corroborate --apply` per claim; record funnel `at_2 / at_3` after each.
- [ ] **Step 4: Seed-week report** — daily content-free counts (auto-accepted / golden / contradiction-holds / oldest pending age) in chat for 7 days.
- [ ] **Step 5: Commit the list.** `git add docs/superpowers/specs/2026-10-05-seed-canon-list.md && git commit -m "docs: seed canon list for corroboration"`
