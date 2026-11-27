# Fix: steering stall — a held amux draft freezes delivery forever

**Dependencies**: `progress/tg-idle-clock-and-parked-delivery.md` (observability
half + the idle-clock fix), `progress/tg-idle-stop-opencode-resume.md`
**Enables**: an accepted owner/GM message reaches the lane or is returned with a
clear reason; no parked message is frozen by idle stop.

Dispatch: general-mgr at the owner's request — "All steering skills seem to be
stalled. The steel is not working." Worker `tg-fix-steering-stall` was itself a
victim (its lane was idle-stopped with a queued message); its investigation never
reached a commit, so this file is the source of truth.

## What Works (evidence read, not assumed)

- Topic bindings are CORRECT (`~/.amux/telegram-gateway/state.json`): 579 →
  `ocusage` → `ses_disp11251506e272e5`, 619 → `tg-fix-steering-stall` →
  `ses_disp112516522e6f9f`, 572 → `tg-fix-relay-topic-release`.
- Gateway half already logs `send_parked` and repeats amux's hourly stall WARN
  into the topic (`stall_notice_sent`) — `tg-idle-clock-and-parked-delivery.md`.
- `idle_clock_reset` already fixed the "revived lane killed 60 s later" defect.

## Reproduced state (read-only, 2026-11-26)

- `steering_queue` rows: `boot-delivery:ocusage:…` and
  `boot-delivery:tg-fix-steering-stall:…`, both `queued_at=1795646888`
  (2026-11-25 22:48 UTC / 19:48 -03), `sender=''` (boot-delivery path),
  `last_skip=not-running`, oldest 731 min at 08:00 UTC.
- Lane meta (`~/.amux/sessions/tg-fix-steering-stall.meta.json`):
  `composer_preview="[fromTelegram@whoamiPV]Eaí,resolveu?"`,
  `composer_stuck_since=1795646940`, `last_send_text="[from Telegram @whoamiPV] E aí, resolveu?"`.
  Both normalize to the SAME whitespace-squashed text: this is amux's OWN paste,
  not a human draft.
- Both lanes are `CC_PROVIDER=opencode` → `writes_claude_transcript` is false →
  `clear_delivered_duplicate_draft` returns false at its first gate.
- `server-rs.log` 2026-11-25T19:36:48Z:
  `verdict="steering_draft_preserved"` on `ocusage`; 19:36:57Z onward hundreds of
  `direct_draft_idle_enter_not_confirmed`, ending in
  `direct_draft_idle_submit_gave_up` / `direct_draft_lane_stopped`.
- `telegram-gateway.log`: `send_parked … held in the input box` and hourly
  `stall_notice_sent … last_skip=not-running`.

## Root cause (three links, one chain)

1. **Rust (`crates/amux-server/src/api/session_verbs.rs`)**. A composer draft
   that is amux's own *stale paste from an earlier send* is recognized by
   neither self-heal: `composer_holds_this_delivery` only compares the draft to
   the delivery happening **now**, and `clear_delivered_duplicate_draft` only
   clears drafts found in a **Claude** JSONL user turn
   (`writes_claude_transcript` gate — always false for opencode). The lane's
   `composer_stuck_since` and `meta.last_send_text` prove provenance but nothing
   reads them. Every later message therefore returns
   `send-refused: composer` and stays in `steering_queue`.
2. **Gateway (`integrations/telegram-gateway/telegram_gateway/bot.py`)**.
   `idle_sweep_once` stops a pinned lane on owner+agent silence with no check
   for a pending/held steering message. Once stopped,
   `lane_block_reason → not-running`; no deadline reaps it
   (`steer_dead_letter_verdict` covers only `no-env-file`/`archived`), so the
   message can wait forever. The revive-on-prompt path cannot help while link 1
   still parks the send.
3. **Feedback**. The stall WARN reaches the topic but its generic text hides
   `last_skip=not-running` / `send-refused: composer`, i.e. the one fact the
   owner needs to act. `stallwatch.Stall` carries only `session`.

## What's Left to Build

### Milestone A — Reproduce as failing tests (no production change)
- [x] A1 Read-only reproduction above recorded with exact files/lines.
- [x] A2 Rust unit tests: stale own paste must be recognized; a human draft must
      still be preserved (control).
- [x] A3 Gateway tests: a lane with queued steering is not idle-stopped; a stall
      line's reason reaches the topic.

### Milestone B — Rust: own stale draft is recoverable
- [x] B1 Pure predicate `draft_matches_own_delivery(draft, deliveries)` with
      whitespace-squashed exact match, min length, and a control for human text.
- [x] B2 Provenance `own_delivery_texts(state, name)`: `pane_prompts` ring +
      `cmd_history` + durable `meta.last_send_text` (survives server restart;
      this incident). A store-less variant serves the spawned watcher.
- [x] B3 `submit_own_steering_draft`: recognize a stale own paste, clear it with
      verified C-u, log WARN `steering_own_stale_draft_cleared` (preview +
      `measured/n_considered`), emit `session.own_stale_draft_cleared`, then the
      caller re-reads the frame so the *current* queued message is delivered.
- [x] B4 Control unchanged: a draft that matches neither the current delivery
      nor any own delivery still returns `steering_draft_preserved` (no human
      text is ever overwritten).
- [x] B5 `spawn_direct_draft_idle_submit`: a draft matching an own delivery is
      not declared `HumanChanged`; a bounded, verified clear is logged
      (`direct_draft_own_stale_paste_cleared`) instead of leaving a poison draft
      after the watch gives up.
- **Done when:** `cargo test -p amux-server --lib session_verbs` green (new tests
  + existing draft tests), `cargo check --workspace` clean.
- **Dependencies:** A2
- **Enables:** C

### Milestone C — Gateway: no pending message may be idle-stopped
- [x] C1 `AmuxClient.steering_queued_sessions()` → `GET /api/debug/steering`,
      returns `None` on `measured:false` and raises on transport errors — never
      an empty set that would authorize a stop.
- [x] C2 `idle_sweep_once` fetches queue depth once per sweep and skips any lane
      with `queued>0`, logging `idle_stop_skipped_pending` once per stretch.
- [x] C3 Tests in `test_idle.py`: pending lane stays running; unmeasured/error
      response does not silently authorize a stop; normal idle still stops.
- **Done when:** gateway pytest green.
- **Dependencies:** B (the queue must be able to drain once running)
- **Enables:** D

### Milestone D — Gateway: clear error to the owner
- [x] D1 `stallwatch.Stall` also carries `last_skip` and `blocked_now` parsed
      after the `STALL_MARKER` position (absent → empty string, no guessing;
      `send-refused: composer` keeps its space).
- [x] D2 `stall_watch_once` posts the reason in the topic notice and logs it;
      no change to dedupe ("once per stretch").
- [x] D3 Tests: reason present in the posted notice; a line without the fields
      still produces the generic notice.
- **Done when:** gateway pytest green.
- **Dependencies:** A3
- **Enables:** E

### Milestone E — Verify, document, commit
- [x] E1 Full gateway suite + Rust crate tests + clippy; exact results below.
- [x] E2 README log list + `activeContext.md` + prompt-history entry.
- [ ] E3 Commit code + progress + prompt-history by name (no `git add -A`).
- **Done when:** `git show --stat HEAD` lists all three; no push/deploy claimed.

## Current Status

A–D done and verified; E3 (commit) pending. Nothing pushed or deployed: the
Rust fix becomes live only after main/CI, the Python half needs a gateway
restart.

## Detailed Plan / Design

The safety invariant is unchanged: **an unrecognized draft is never touched**.
Recognition is deliberately narrow — whitespace-squashed EXACT match against a
text amux itself typed (ring / `cmd_history` / `meta.last_send_text`), min 8
chars. `meta.last_send_text` is the durable source that covers this incident:
it is written by the API send path (`session_verbs.rs` update_meta) and survives
a server restart, unlike the in-memory ring. A stale own paste is CLEARED (C-u,
verified by a fresh capture) rather than submitted, because its text was already
announced to the owner; the queued message is then delivered normally. Both
branches log a verdict with `measured` and `n_considered` so a sweep can catch a
silent regression.

`idle_stop` is a gateway-side transport decision, so the guard lives in the
gateway (the Rust drain has no business second-guessing the topic sweeper). The
endpoint `/api/debug/steering` already publishes `lanes[].queued` and `measured`;
use it rather than inventing a ninth primitive.

## Key Research Questions

- Is `meta.last_send_text` written for queued/boot sends? Yes — `update_meta`
  in the api-send accepted branch; here it names the exact stuck text.
- Can a queued boot-delivery be dead-lettered today? No — `steer_dead_letter_verdict`
  excludes `not-running` deliberately (temporary outage), so the gateway guard
  is the right boundary.
- Does the gateway have a queue-read method today? No — add one thin method.

## Success Criteria

- Given a lane whose composer holds an old amux paste and a new queued message,
  the new message is delivered after one drain pass; `steering_draft_preserved`
  is NOT logged for it.
- Given a human draft (no own-delivery match), amux still preserves it and logs
  `steering_draft_preserved`.
- Given a pinned lane with `queued>0`, the idle sweep does not stop it and logs
  `idle_stop_skipped_pending`.
- A stall notice names `last_skip`/`blocked_now` when present.

## Discoveries

- `composer_state(...).typed()` has already squashed whitespace; that is why the
  draft preview looks like `[fromTelegram@whoamiPV]Eaí,resolveu?` while
  `last_send_text` has spaces. Any match must squash both sides.
- `jsonl_submission_since` / `clear_delivered_duplicate_draft` are Claude-only;
  the two victim lanes are opencode, so *every* transcript-based self-heal was
  inert for exactly the lanes that stalled.
- The GM's own message was parked the same way (`send_parked … waiting for fresh
  boot readiness`), so there was no rescue path outside a code fix.

## Implementation Notes

- No changes to `~/.amux/telegram-gateway/state.json`, other lanes' data, the
  fleet tmux server, or any service beyond what the tests require.
- No out-of-repo file needs editing: the Rust path is `crates/amux-server` and
  the gateway path is `integrations/telegram-gateway`, both in this workspace.
  If that changes, STOP and report file/line instead of working around it.
- Branch is `feat/telegram-gateway` (not main); commit only — pushing/deploying
  is the owner's call and is not claimed here. The Rust fix is live only once
  it lands on main and `checks` passes; the gateway half needs a service restart.

## Deferred Work

- Auto-revive a stopped lane directly from the stall watcher (rather than on the
  next owner prompt). Not needed once the idle guard keeps pending lanes up.
- Dead-letter a `not-running` hold to its sender after a very long age; changes
  intentional semantics of `steer_dead_letter_verdict`, owner decision.

## Verification results (exact commands)

```
cd integrations/telegram-gateway && uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
cd integrations/telegram-gateway && .venv/bin/python -m pytest -q
  -> 408 passed, 6 warnings in 3.59s   (baseline before changes: 402 passed)
CARGO_TARGET_DIR=/home/pv/.amux/rust-build-target scripts/safe-cargo.sh check -p amux-server --lib
  -> Finished `dev` profile; exit_code 0
CARGO_TARGET_DIR=/home/pv/.amux/rust-build-target scripts/test-contended.sh -p amux-server --lib stale_draft_tests
  -> test result: ok. 6 passed; 0 failed (4387 filtered out)
CARGO_TARGET_DIR=/home/pv/.amux/rust-build-target scripts/test-contended.sh -p amux-server --lib session_verbs
  -> test result: ok. 535 passed; 0 failed; 3 ignored (3855 filtered out)
CARGO_TARGET_DIR=/home/pv/.amux/rust-build-target scripts/safe-cargo.sh clippy -p amux-server --lib -- -D warnings
  -> Finished `dev` profile; exit_code 0
```
`session_verbs` is a FILTER: 117 integration targets under
`crates/amux-server/tests/` were not built/run by that command. The gateway
suite is the full one. `amux resources` was read before the builds: level ok,
12 cores / 126 GB RAM, no heavy-peer warning.

## Execution Log

| Step | Planned | Actual | Status | Notes |
|------|---------|--------|--------|-------|
| A1 | read-only reproduction | DB rows + meta + logs quoted above | done | no code touched |
| A2 | Rust unit tests | 2 new tests in `stale_draft_tests` | done | 6 passed |
| A3 | Gateway tests | 3 idle + 3 stall tests | done | 408 passed total |
| B1–B5 | Rust self-heal | new predicate, provenance, clear+event, watcher clear | done | clippy clean |
| C1–C3 | Gateway idle guard | new client method + sweep guard | done | unmeasured/503 refuse stops |
| D1–D3 | Reason in topic | `Stall` fields + notice + logs | done | space-bearing value parsed |
| E1 | Verify | commands/results above | done | no contention during tests |
| E2 | Docs | README + activeContext + prompt-history | done | |
| E3 | Commit | by name, `git add -f` for ignored memory-bank | pending | |

## Human Verification

### Quick Check (< 2 min)
```
sqlite3 -readonly ~/.amux/amux.db "SELECT session,COUNT(*),MIN(queued_at) FROM steering_queue GROUP BY session;"
grep -a 'steering_own_stale_draft_cleared\|idle_stop_skipped_pending' ~/.amux/logs/{server-rs,telegram-gateway}.log
```

### Expected Results
- After a new message to a lane with a stuck amux draft: one
  `steering_own_stale_draft_cleared` WARN, no `steering_draft_preserved`, and the
  queued row is drained (queue depth drops).
- A pinned lane with a queued row survives an idle sweep tick:
  `idle_stop_skipped_pending`, no `idle_stop`.
- A new stall line posts a topic notice containing its `last_skip`/`blocked_now`.

### Known Limitations
- The Rust fix is not live until deployed to main; this session does not push.
- A gateway restart is needed for the Python half to take effect.
- A human draft that happens to equal a previously delivered amux message is
  treated as amux's own (the provenance predicate cannot distinguish a perfect
  human retype). Exact match + 8-char minimum keeps this narrow.

## Summary

### What Was Done
- **Root cause (Rust)** — a composer draft that is amux's own *earlier* paste is
  now recognized from its provenance (ring / `cmd_history` / durable
  `last_send_text`), cleared with a verified C-u, and logged
  (`steering_own_stale_draft_cleared` / `direct_draft_own_stale_paste_cleared`),
  so the queued message is delivered instead of being refused forever. An
  unrecognized draft is still preserved — no human text is overwritten.
- **Root cause (gateway)** — the idle sweep reads `/api/debug/steering` before
  stopping anything. A lane with a queued message is never stopped
  (`idle_stop_skipped_pending`); an unmeasured or unreadable queue stops NO
  lane, because an unread queue is not an empty one.
- **Feedback** — the stall notice in the owner's topic now carries amux's
  `last_skip` / `blocked_now`, so "clear the draft" is distinguishable from
  "start the lane".

### Files Changed
- `crates/amux-server/src/api/session_verbs.rs` — provenance predicate, stale-paste clear in `submit_own_steering_draft` and the idle watcher, caller re-read, 2 tests.
- `crates/amux-server/src/api/pane_prompts.rs` — expose own-delivery texts (ring + `cmd_history`).
- `integrations/telegram-gateway/telegram_gateway/amux.py` — `steering_queued_sessions()` with AF-320 contract.
- `integrations/telegram-gateway/telegram_gateway/bot.py` — idle guard + reason in stall notice.
- `integrations/telegram-gateway/telegram_gateway/stallwatch.py` — parse reason fields.
- `integrations/telegram-gateway/tests/{test_idle,test_session_control,test_stallwatch}.py` — 6 new tests + fakes.
- `integrations/telegram-gateway/README.md` — new log signals.
- `memory-bank/activeContext.md`, `memory-bank/progress/tg-steering-stall-root-cause.md`, prompt-history (gitignored; force-added by name).

### How to Verify
**Quick:** `grep -a 'steering_own_stale_draft_cleared\|idle_stop_skipped_pending' ~/.amux/logs/{server-rs,telegram-gateway}.log`
**Full:** gateway `pytest -q` → 408 passed; `test-contended.sh -p amux-server --lib session_verbs` → 535 passed.

### Status
| Step | Status | Notes |
|------|--------|-------|
| A–E2 | Done | evidence above |
| E3 | Pending at write time | commit by name; no push/deploy |

### Not Done (honest)
- The two stuck lanes were NOT recovered live in this session: their queued rows
  remain (read-only queries only), and no lane/service was restarted. The Rust
  fix is not in the running server (it is on branch `feat/telegram-gateway`,
  not on `origin/main`); the gateway fix needs `systemctl --user restart
  amux-telegram-gateway`. Deploy/push was not requested and is not claimed.

## Follow-up (owner authorized push + deploy + pull, same day)

- `feat/telegram-gateway` was ALREADY pushed at `d5fcfb26f` (other workers
  committed after this fix and pushed the branch); `git push origin
  feat/telegram-gateway` → `Everything up-to-date`. All 59 commits in
  `origin/main..feat` are authored by `pv`, the owner who authorized the push.
- **Python half is live**: gateway restarted at 10:30 and 11:07 -03; the log
  now carries `stall_notice_sent … reason=last_skip=…, blocked_now=…` (new
  code). No extra restart was needed.
- **Rust half pushed to main** as `3b5918368` (only the two `.rs` files —
  `integrations/telegram-gateway/` does not exist on main). Gate:
  `owner_not_needed`, `revert_ok`, `land_gate_pass`; rust/e2e/review legs were
  explicitly `--no-*` with recorded reasons (no `cargo-nextest`, no
  `node_modules`, no `claude` CLI in this container). Equivalent local proof:
  check + clippy + 535 `session_verbs` tests; independent reviewer engaged.
  `git push origin HEAD:main` → `abfba334d..3b5918368 HEAD -> main`.
- **Live verification pending**: `/health` still reported `commit=abfba334d`
  at the last check (14 checks success, 15 pending); the builder activates only
  after `checks` passes. Not polled in a loop.
- **Pull**: local `main` advanced `abfba334d → 3b5918368` via `amux
  sync-checkout`. `feat/` was NOT merged with `origin/main`: 40 conflicting
  hunks across 6 opencode-provider files (incl. `Result<()>` vs `Result<bool>`).
  Left for a deliberate merge; the shared checkout also has peer `UU` files,
  which were not touched.
- The two stuck `steering_queue` rows were gone at the final read-only check
  (cleared by another worker), so the immediate incident is resolved.

## Merge `origin/main` into `feat/telegram-gateway` (2026-11-27)

- Owner asked to pull `origin/main` into `main`, merge `main` into `feat/`, and
  show the conflict plan first. Plan shown; owner then said to wait for the
  peer and sync once it was done.
- **Peer check:** the shared checkout still had `UU` in `amux.py`/`bot.py` and
  `AA` in `activeContext.md`, all dated 2026-11-26 15:07 -03, with no
  MERGE_HEAD/rebase state. Its workers no longer exist (`amux peek` → worker
  not found). The conflict markers showed the abandoned cherry-pick was
  `f8cf229ad` — a commit already an ancestor of the feat tip — and the `HEAD`
  side was the newer, fuller version in all three files. Evidence preserved at
  `/tmp/opencode/peer-conflict-evidence/2026-11-26T15-07/` before restoring the
  three files to `HEAD`. No peer work was discarded.
- `amux sync-checkout` then advanced the shared checkout `d5fcfb26f →
  b404df409`; `main` was already current at `3b5918368` (= `origin/main`).
- Merge executed in a clean detached worktree at `origin/feat/telegram-gateway`,
  `git merge --no-edit origin/main`. Exactly the 6 predicted files conflicted
  (40 hunks). All 6 were resolved to the `origin/main` version by
  `git checkout origin/main -- <paths>`: the feat versions are the pre-#408
  implementation of the opencode provider, which main upstreamed and then
  improved (M9/M10/M11, `set_model -> Result<bool>`, kind-based boundary rules).
  Verified: zero markers, zero unmerged, and `git diff --exit-code origin/main`
  clean for all 6.
- Anti-silent-drop checks on the merged tree: 59 gateway files present, 21
  steering-fix references in `session_verbs.rs`, 2 in `pane_prompts.rs`,
  `git diff --check` clean. `cargo check -p amux-server --lib` and
  `cargo clippy -p amux-server --all-targets -- -D warnings` both passed;
  gateway `pytest -q` → 422 passed. Full `amux-server --lib` suite via
  `scripts/test-contended.sh` → exit 0 (`test receipt: test exit 0 retained`);
  targeted modules: `opencode` 133 passed / 7 ignored, `session_verbs` 551
  passed / 3 ignored, 0 failures in both.
- **Deploy note:** `/health` still reports `commit=abfba334d` even though the
  `gate` workflow passed for `3b5918368` (the `e2e` job failed, `checks` shards
  passed). `/api/pull` reports `measured=false, repo=null, state=unmeasured`
  and the builder timer/service are inactive — so this box cannot currently
  explain or drive activation. Reported to the owner; not changed here.
