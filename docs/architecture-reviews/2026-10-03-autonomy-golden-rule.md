# MemCore v0.7 — Autonomy + Golden Rule (single phase)

- **Status:** Approved for implementation (2026-10-03, owner: P Choke)
- **Scope:** one phase, all five parts together. No Desktop/UI afterwards.
- **Goal (owner words):** MemCore must be smart on its own. Corroborated claims
  across sources auto-promote to Golden Rule. No human console needed —
  AI works MemCore through tools + CLI only.

## Locked decisions

1. Corroboration N = **3 distinct agents → auto-accept** (project scope,
   verification `source_backed`); **5 distinct agents → Golden Rule**
   (accepted + pinned + critical, always injected).
2. Auto-accept = **corroborated path + explicit durable path only**:
   (a) explicit user signal ("จำไว้ว่า…", `memory_remember` tool,
   `memory_write/add`) → accepted directly;
   (b) semantic `remember` with confidence ≥ 0.95 → accepted directly;
   everything else stays candidate.
3. **Full UI removal**: delete `dashboard/plugin_api.py`,
   `dashboard/manifest.json`, `desktop/plugin.js`, `tests/test_dashboard_api.py`.
   No REST operator console remains. Operator surface = AI tools
   (`memory_*`) + `python -m memcore` CLI. `doctor` deploy check drops UI files.
4. Recall stays deterministic lexical (FTS5 + Thai substring). No vectors in v0.7.
5. All auto-mutations are audited (`auto_*` actions) and reversible
   (supersede / reject / tombstone veto still win).

## Part 1 — Corroboration → Golden Rule (engine, `core.py`)

- New constants: `CORROBORATE_ACCEPT_N = 3`, `GOLDEN_N = 5`.
- New helper `corroboration_members(conn, project_id, claim_fp)`:
  all memories in project with this fingerprint, lifecycle in
  (candidate, accepted, conflict), not tombstone-blocked; returns
  (id, scope, owner, lifecycle, created_at).
- New `maybe_auto_corrob(conn, project_id, claim_fp, actor)` — called after
  every successful `create_memory` (tool, ingest explicit, ingest semantic,
  CLI remember) and by CLI `corroborate` sweep:
  - count DISTINCT `owner_agent_id`. Tombstone-blocked → no-op.
  - ≥3: pick canonical = oldest (project scope preferred, then earliest
    `created_at`). Set it `scope='project'`, `lifecycle='accepted'`,
    `verification='source_backed'` (unless already `user_authoritative`).
    Audit `auto_corrob_accept` with member list.
  - ≥5: additionally set canonical `pinned=1, critical=1`.
    Audit `auto_golden_promote`. Golden = the only pinned+critical lane
    besides the 5 hand-picked canon rows (ADR-0016 stays valid).
  - `supersede` changes fingerprint → old count naturally stops growing.
    No counter to reset.
- New `accept_memory()` internal transition used by both paths above.
- New `set_golden(memory_id, golden: bool)` helper for CLI/tests.
- New `apply_freshness_decay(conn, aging_days=30, stale_days=90)`:
  `current` → `aging` → `stale` by `updated_at` age. Never changes lifecycle,
  never tombstones. Stale rows rank last in recall (existing CASE ordering).

## Part 2 — Auto-accept entry points (`ingest.py`, `plugin.py`)

- `ingest.process_event` explicit-durable branch → `create_memory(...,
  lifecycle='accepted')` with reason `explicit durable signal auto-accept`,
  then `maybe_auto_corrob`.
- `ingest.apply_semantic_analysis` remember branch:
  confidence ≥ 0.95 → `lifecycle='accepted'` (reason `high-confidence
  semantic auto-accept`), else candidate as today. Then `maybe_auto_corrob`
  on the resulting (or duplicate) fingerprint.
- `plugin.tool_memory_remember` (explicit agent tool, always project scope)
  → `lifecycle='accepted'`, verification stays `unverified` until
  corroborated (single source ≠ corroborated).
- `post_llm_call` observations stay candidate-only (assistant claims never
  self-accept). Unchanged.

## Part 3 — Recall precision (`core.search`, `plugin.build_recall_block`)

- Rank order unchanged (pinned → accepted → verification → freshness →
  recency) — already correct; v0.7 adds two fixes:
  - **Fingerprint dedup**: `build_recall_block` dedups by
    `core.fingerprint(content)` in addition to memory id, so 3 corroborating
    copies collapse to one line (canonical first).
  - **F5 word-boundary per-row cap**: `MAX_ROW_CHARS = 220`. Rows exceeding
    the remaining budget are truncated at the last space before the limit
    with `…` instead of skipped entirely. Thai (no spaces) falls back to
    hard cut at the limit. Never splits mid-word for space-separated text.
- New harness `test_recall_quality.py`: Thai query set
  (gateway / profile-switch / Discord token / fleet roster / SOUL rules)
  asserting accepted outranks candidate and Golden pins surface first.
  Quality is now measured, not just latency.

## Part 4 — Full UI deletion

Delete (git rm): `integrations/hermes/memcore/dashboard/plugin_api.py`,
`dashboard/manifest.json`, `desktop/plugin.js`, `desktop/` dir,
`dashboard/` dir, `integrations/hermes/memcore/tests/test_dashboard_api.py`.
Update: `scripts/deploy_hermes_plugin.py` RUNTIME_FILES (6 files),
`harness/test_hermes_plugin_source.py` (allowlist + version test),
`integrations/hermes/memcore/README.md`, `plugin.yaml` → 0.7.0,
`doctor` deploy check (auto-follows allowlist).
Deployed copy: remove `dashboard/` + `desktop/` from
`%LOCALAPPDATA%/hermes/plugins/memcore/` on redeploy.
Rationale (owner): Dashboard was never opened in practice; an unread console
is dead code with a FastAPI attack surface. AI + CLI is the console.

## Part 5 — Journal hygiene (no more operator_attention)

- `ingest.auto_dismiss_stale_builtin(conn, days=7)`:
  `builtin_*_unresolved_target / missing_old_text / requires_review /
  replace_missing_content` older than 7d → `ignored /
  builtin_memory_mutation_auto_dismissed` + `journal_auto_dismiss` audit.
- `ingest.auto_resolve_defer_cap(conn, max_defers=3)`:
  `semantic_deferred` events with ≥3 `defer` analyses → `ignored /
  semantic_defer_cap_reached` + audit. Ends eternal-pending defers.
- CLI `journal-sweep [--apply]`: dry-run lists, `--apply` runs both sweeps.
- `journal_stats` health: `operator_attention` only when unresolved builtin
  is YOUNG (<7d); old ones are sweepable, not alarming.

## CLI additions (`__main__.py`)

- `corroborate --project X [--apply]`: scan fingerprints, show
  what would promote; `--apply` runs `maybe_auto_corrob` per fingerprint.
- `golden-list --project X`: pinned+critical rows (the Golden set).
- `journal-sweep [--apply] [--builtin-days 7] [--max-defers 3]`.
- `decay [--apply] [--aging-days 30] [--stale-days 90]`.

## Tests

- Harness: `test_corroboration_golden.py` (3→accept, 5→golden,
  tombstone veto, supersede resets), `test_recall_quality.py` (Thai set),
  extend `test_ingest.py` (explicit auto-accept, high-conf accept,
  defer-cap, builtin auto-dismiss), update `test_hermes_plugin_source.py`.
- Integration: update `test_plugin.py` (remember→accepted, block truncation),
  delete dashboard tests. Gates stay green:
  `python -m harness` + `python -m unittest discover -s integrations/hermes/memcore/tests`.

## Rollout

1. Implement engine + ingest + plugin + CLI.
2. Delete UI dirs, bump 0.7.0, update docs/deploy/tests.
3. Harness + integration gates green.
4. `deploy_hermes_plugin.py` redeploy; remove stale `dashboard/ desktop/`
   from installed plugin dir; `doctor` clean (no OUT OF SYNC).
5. Live `corroborate --apply` + `journal-sweep --apply` once by AI on behalf
   of owner; report counts.
