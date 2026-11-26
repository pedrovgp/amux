# Project decisions — amux Telegram gateway worktree

Standing constraints for this task. Read before planning.

## 1. The memory bank is committed, force-added over `.gitignore` (CORRECTED 2026-11-26)

This checkout is a public fork of `mixpeek/amux` and branches are proposed
upstream as PRs, so `.gitignore` still lists `memory-bank/`. The RULE, however,
is the global one (§12 / §16 of the agents' guidelines plus the owner's
dispatch of 2026-11-26): the plan, the progress file and the session's
prompt-history are committed WITH the code, in the same commit, and the commit
message carries `Progress: memory-bank/progress/<name>.md`.

Because the directory is ignored, this takes an explicit
`git add -f memory-bank/<file>` — never `git add -A`, and never a blanket
force-add of the whole tree (that would commit scratch that was never meant to
travel). Precedent: `f8cf229ad` (2026-11-26) and every gateway commit after it.

The earlier text here said "local-only, never committed". That described the
first fork's practice and is now wrong; it is corrected rather than deleted so
a reader who remembers the old rule sees why it changed.

## 2. The gateway is an isolated module

- All code lives under `integrations/telegram-gateway/`. No changes to `crates/`.
- It composes existing primitives over HTTP (`/api/workers/...`); it does not add
  a new amux primitive.
- It owns its own bot token and its own state dir (`~/.amux/telegram-gateway/`).
- No credentials in the repo: values go to `~/.amux/server.env` (README names
  variables only).

## 3. Security guardrail is non-negotiable

`ALLOWED_TELEGRAM_USER_IDS` must be set and non-empty; the process refuses to
start otherwise. Every handler (message, callback, voice) is gated. Denials are
answered explicitly and logged.

## 4. Verified API shapes (2026-10-26)

| Purpose | Call |
|---|---|
| base | `canonical_url` from `~/.amux/endpoint.json` (`https://localhost:8824`) |
| list | `GET /api/workers` -> `{items:[{id,name,display_name,status,running,state,lifecycle,...}],total}` |
| create | `POST /api/workers` `{display_name, cwd?, provider?, model?, backend?}` -> 201 row |
| start | `POST /api/workers/{key}/start` |
| stop | `POST /api/workers/{key}/stop` |
| send | `POST /api/workers/{key}/send` `{text, no_board?}` -> `{ok:true,...}` |
| peek | `GET /api/workers/{key}/peek?lines=N` -> `{output, lines_requested, ...}` |
| health | `GET /health` |

Local TLS is self-signed: `verify=False` unless `AMUX_TLS_VERIFY=1`.

## 5. Gates

`python3 -m pytest` for this module; repo Rust gates are untouched (no `.rs`
changes). Do not `git add -A`; stage by name.
