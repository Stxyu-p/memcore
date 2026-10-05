# Seed Canon List — 2026-10-05 (Task 6, MIKA-owned)

- **Status:** Approved by P Choke 2026-10-06. Batch 1 (claims 1-5) dispatched to NUA/SORA/MILIM; batch 2 (claims 6-10) follows after funnel verification.
- **Project:** `proj-shared-platform` (all 10 rows, read-only verified 2026-10-06)
- **Rule:** writers must store each sentence **verbatim** — do not reword. Corroboration keys on byte-identical normalized text; paraphrase = different fingerprint = no count.
- **Writers:** MIKA + NUA + SORA + MILIM (≥3 distinct `owner_agent_id` per claim)
- **After each claim reaches 3 writers:** `corroborate --apply` per claim; record funnel `at_2 / at_3`.

## ADR-0016 five

| # | Memory ID | Verbatim canonical text |
|---|-----------|-------------------------|
| 1 | `mem-6af3ac177392` | SOUL v2.1 fleet rules (ALTIMA-approved 2026-08-31): relay claims with verification status; verified claims attach evidence; replies inline/scannable; briefs carry absolute paths + constraints; no dispatch cap (override 2026-09-05); bad news early. |
| 2 | `mem-55bbd631306e` | lnwjud (224 tools): call ONLY via C:/Users/BlankScreen/Workspace/lnwjud-bridge/lnwjud_call.py (native MCP registration fails). Every request needs params._meta {protocolVersion, clientCapabilities} or -32022 rejects. computer_use needs workspaceId. Verified live 2026-09-02. |
| 3 | `mem-8d909e90be49` | HERMES_PROFILE env does not switch profiles (overwrites main config). Use `hermes profile use NAME` (sticky); back with `hermes profile use default`. Discord home_channel needs `platform: discord` or gateway KeyErrors. |
| 4 | `mem-68be1f685ef6` | Direct Discord REST: token = DISCORD_BOT_TOKEN from the Hermes .env file. Must set User-Agent header DiscordBot (see discord-api-docs) or Cloudflare returns 403 code 1010. discord_admin has no create_channel - use POST guilds channels + channels messages endpoints. |
| 5 | `mem-bfe7663281e4` | Hermes fleet = 5 agents (2026-08-31): MIKA = orchestrator (default), NUA = evidence, SORA = software, ALTIMA = review (cx gpt-5.6-terra-review, verdicts APPROVE REQUEST CHANGES REJECT + INCOMPLETE), MILIM = experience (joined late Aug). |

## Operational five

| # | Memory ID | Verbatim canonical text |
|---|-----------|-------------------------|
| 6 | `mem-6f95d1a4a2e5` | P Choke uses Hermes Agent as his one and only agent tool - no Claude Code CLI, Codex CLI, or other coding CLIs. All models are reached through the 9router gateway at localhost:20128 (provider custom 'Local (localhost:20128)', model.default = Deepseek combo). |
| 7 | `mem-9d04f45b254b` | Bot-to-bot dispatch (kanban retired 2026-08-21): write file with prefix "Message from [bot] <name>: "+body, run `hermes -p <p> chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <file>`. Reply in session. English commands. No concurrency cap (override 2026-09-05). |
| 8 | `mem-74b08adece91` | MemCore at C:/Users/BlankScreen/Workspace/memcore/ — stdlib-only Python package: memcore/{store,core,ingest,engine,native_provider}. Deployed plugin at LOCALAPPDATA/hermes/plugins/memcore. Run: python -m memcore {init,project,agent,member,remember,search,doctor}. |
| 9 | `mem-2fcfcad97e5b` | P Choke's dispatch decision (2026-09-05): use each profile's configured model; never pass -m to override it. If a restored Bot Chat fails with 401/404, report the failure instead of silently pinning another model. |
| 10 | `mem-36ab7921c349` | Dispatch concurrency (current rule): no concurrency cap - P Choke overrode the old 2-3 limit on 2026-09-05. The 2026-08-31 incident (10 parallel delegate_task subagents via 9router glm-5.3-flash all died with 429/502 within ~60s) is history only: on 429/502, back off and retry instead of pre-limiting parallelism. |

## Notes

- All 10 verified read-only from live store `C:/Users/BlankScreen/.memcore/memory.db` on 2026-10-06 (mode=ro, no writes).
- Current holders are single-source (`agent-mika`); funnel `at_1=89 at_2=0` — seeding to ≥3 writers is what makes `at_3 > 0` reachable.
- Token-budget config (60000/256000) and fleet SOUL identity-drift sections were not found as shared-platform rows; the three dispatch-ops rows above (7, 9, 10) cover the dispatch half of spec §3.1. If P Choke wants those two swapped in, name the memory IDs.
