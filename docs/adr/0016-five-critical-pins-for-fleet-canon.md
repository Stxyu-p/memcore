# ADR 0016 — Five critical pins for the fleet canon

- **Status:** Accepted (2026-09-17)
- **Context:** ADR 0013 (critical pins are the only injected lane) · **Depends on:** ADR 0013
- **Evidence:** direct SQLite update on the live store, verified before/after

## Status

Accepted, applied to the live store on 2026-09-17.

## Context

ADR 0013 narrowed injection to pinned+critical rows, which turns the critical
flag into a scarce five-slot budget: everything marked critical rides into
every turn. Nothing was critical yet, so the guarantee lane existed but was
empty while the most operationally load-bearing facts still competed (and
sometimes lost) in ranked hits.

## Decision

Mark exactly these five project memories as pinned+critical — the minimum set
whose absence breaks fleet work:

| Memory | Why it is canon |
|---|---|
| `mem-6af3ac177392` | SOUL v2.1 fleet rules (verification labels, brief format, bad news early) |
| `mem-55bbd631306e` | lnwjud bridge: only via `lnwjud_call.py`, `_meta` protocol required |
| `mem-8d909e90be49` | `HERMES_PROFILE` env overwrites config — use `hermes profile use` |
| `mem-68be1f685ef6` | Discord REST: `DISCORD_BOT_TOKEN`, Cloudflare `User-Agent` 1010 wall |
| `mem-bfe7663281e4` | Fleet roster: MIKA / NUA / SORA / ALTIMA / MILIM |

`mem-6f95d1a4a2e5` (9router gateway) stays pinned-only: gateway mechanics are
recallable on demand and the five above are cheaper to keep always-on.

## Consequences

- The five canon rows appear in every recall block; budget math must assume
  ~the whole canon on every turn (currently well under the 1200-char cap).
- Adding a sixth critical row requires retiring or demoting an existing one —
  treat this table as the closed set until the owner re-decides.
- This ADR documents the *only* sanctioned direct-SQL write to `memory`
  flags; normal curation keeps going through the governed API.
