# Phase: a parked message has a life — tracked, revived, or returned with an error

**Dependencies**: `progress/tg-idle-clock-and-parked-delivery.md` (the `send_parked`
WARN + topic line + stall watch), `progress/tg-steering-stall-root-cause.md`
(the idle sweep's queue probe and the Rust stale-draft self-heal),
`progress/tg-stall-notice-dedupe.md` (one notice per undelivered row)
**Enables**: the gateway's promise "amux will deliver it at its next turn boundary"
becomes a bounded contract — a parked message is TRACKED until it is delivered,
the lane holding it is revived instead of idle-stopped, and a message that dies
with its lane is returned to the owner with a clear error. Never queued forever.

## Incident (2026-11-26 → 2026-11-28, the owner's "steering is stalled")

Evidence read from `~/.amux/logs/telegram-gateway.log` and
`~/.amux/logs/server-rs.log` (both grepped with `-a`):

- The gateway says the right words and then FORGETS:
  `send_parked … session=ocusage reason=queued detail=queued (held in the input
  box; submitted automatically when this turn ends)` — and nothing ever
  re-checks that hold.
- amux's own watcher for that hold has a bounded life and every exit is a WARN
  in a log the owner does not read: `verdict="direct_draft_idle_submit_gave_up"`
  (30 min) or `verdict="direct_draft_lane_stopped"` (the pane died with the text
  in it). Measured 2026-11-25 18:36 … 20:06 for `ocusage`:
  five `…gave_up` and two `…lane_stopped` — seven owner messages returned
  from the composer to nobody.
- The idle sweep stops the lane 30 min later even though a message is pending,
  because the guard it has reads only `/api/debug/steering`
  (`idle_stop_skipped_pending`), and a send that amux ACCEPTED is deleted from
  `steering_queue` at acceptance (`session_verbs.rs` ~25194: `DELETE FROM
  steering_queue` under `if ok`) — so the accepted-but-unsubmitted hold is
  invisible to the sweep. `idle_stop … session=ocusage idle_s=1812` at
  2026-11-27 18:17 came 34 min after a parked message.
- Once the lane is stopped the queue gate is `not-running`, and
  `steer_dead_letter_verdict` deliberately does not reap it (only
  `no-env-file`/`archived`): the row waits for a start that the gateway only
  performs on a NEW owner prompt. Hourly `steering queue STALLED … blocked_now=
  not-running` was the symptom, not the cause.

Root cause, in one sentence: **the gateway treats "amux accepted the text" as
the end of its responsibility; amux treats "accepted" as "delivered"; the only
party that can retry (the gateway) keeps no record, and the only party that
knows the delivery failed (amux) tells a log file.**

## Design (this repo — the gateway half; no Rust change)

The gateway already owns the lane lifecycle (idle stop + revive on prompt), so
it is the right place to own a parked message's lifecycle too:

1. **A hold is recorded, durably** (`state.json`): `{preview, reason, kind,
   thread, since, tries, last_try, escalated}` under
   `chats.<id>.parked.<session>`. Durable because the incident spanned gateway
   restarts; `state.json` is the gateway's own rebind-owned store.
2. **`parked_sweep_once`** (new loop, `TELEGRAM_GATEWAY_PARKED_SWEEP_S`,
   default 60 s) reads `/api/debug/steering` (measured contract: `None` =
   never read, decide nothing) + `/api/sessions` once, and for every hold:
   - **lane not running, queue row present** → START the lane (bounded to one
     attempt per `TELEGRAM_GATEWAY_PARKED_REVIVE_S`, default 300 s):
     `parked_lane_started`. This is the `blocked_now=not-running` stall: the
     host can resolve it, so it must not become a permanent wait (A4).
   - **lane not running, no queue row** → the text died with the pane; page
     the owner once with a clear failure and drop the hold
     (`parked_failed reason=lane-stopped`).
   - **lane running, queue row present** → still waiting; the stall watch
     already owns the owner-facing notice for a queue row, so stay quiet.
   - **reason `draft`** → a HUMAN's text is in the composer; amux will not
     overwrite it and neither will we. Keep the hold (it blocks idle_stop) and
     page once at the deadline (`parked_failed reason=draft`), naming the
     composer as the blocker.
   - **hold older than `TELEGRAM_GATEWAY_PARKED_HOLD_MAX_S` (default 1800 s,
     amux's own watcher budget) and still unconfirmed** → one honest line
     (`parked_expired`: accepted, never confirmed, send it again) and drop the
     hold, so the hold can never pin a lane forever.
3. **The idle sweep skips a lane with a live hold**
   (`idle_stop_skipped_parked`), in addition to its queue probe. Expired holds
   no longer block, so this cannot leak RAM forever.
4. **A confirmed delivery clears the hold** (`parked_cleared
   reason=delivered`), the same way it already clears the once-per-stretch
   notice.

Every path is a greppable line: `parked_tracked`, `parked_cleared`,
`parked_lane_started`, `parked_start_failed`, `parked_failed`,
`parked_expired`, `parked_sweep_unreadable`, `idle_stop_skipped_parked`.

## Milestones

### Milestone 1 — reproduce (offline, pinned by tests)
- [ ] 1.1 `state.py` roundtrip: `mark_parked` / `parked` / `clear_parked` /
      `parked_holds` survive a reload and are bounded like `activity`.
- [ ] 1.2 Reproduce the freeze in a test: a parked prompt + an idle lane + a
      live hold → today's sweep stops the lane (the incident). Written RED
      first against the current code, then GREEN after 3.1.
- [ ] 1.3 Reproduce the stranding: a hold + a stopped lane + a queue row →
      no `start` is issued today; the row has no expiry (`not-running` is not
      dead-lettered).

### Milestone 2 — delivery fix (the cause)
- [ ] 2.1 `_deliver_prompt` records the hold on park (kind from amux's own
      wording) and clears it on a confirmed send.
- [ ] 2.2 `parked_sweep_once`: revive a stopped lane that has a queue row,
      bounded + logged.
- [ ] 2.3 `parked_sweep_once`: a dead hold (lane stopped, no row) is RETURNED
      to the owner as an error in its topic, then dropped.
- [ ] 2.4 `parked_sweep_once`: the deadline paths (direct/draft/expired) page
      once and stop blocking.

### Milestone 3 — idle_stop never freezes a pending message
- [ ] 3.1 `idle_sweep_once` skips a lane with a live hold
      (`idle_stop_skipped_parked`), once per stretch.
- [ ] 3.2 The skip does not outlive the hold: an expired/cleared hold lets the
      next sweep stop the idle lane.

### Milestone 4 — logs, config, wiring
- [ ] 4.1 `config.py`: `parked_sweep_s`, `parked_hold_max_s`,
      `parked_revive_s` (+ env docs and the AF-320-style "0 disables").
- [ ] 4.2 `post_init` / `post_shutdown` spawn and cancel the loop;
      `gateway_started` prints its tick; `cli.py --check` shows it.

### Milestone 5 — regression suite + docs
- [ ] 5.1 New `tests/test_parked_holds.py` (state, sweep, idle guard,
      unmeasured queue, revive bound, failure page).
- [ ] 5.2 `python -m pytest -q` green (baseline 422 in this worktree).
- [ ] 5.3 README "logs to watch" + this progress file's status.

## Success Criteria

- A parked message is recorded within the same delivery call that parks it.
- A stopped lane holding a queue row is STARTED by the gateway within one
  sweep tick (bounded per hold), and `parked_lane_started` names it.
- A hold whose text died with its lane produces exactly ONE
  `parked_failed` line in the topic and is then forgotten.
- `idle_stop` never stops a lane with a live hold; an expired hold stops
  blocking.
- No hold lives past `parked_hold_max_s` without an owner-visible line.
- Suite green.

## Execution Steps

### Step 1: progress file (this file)
**Changes:** `memory-bank/progress/tg-parked-hold-liveness.md`
**Done when:** it exists before any code change.
**Status:** done

### Step 2: `state.py` parked holds
**Changes:** `telegram_gateway/state.py` (`mark_parked`, `parked`,
`clear_parked`, `parked_holds`, `note_parked_try`, `tracked_sessions` key)
**Done when:** roundtrip + bound tests green.
**Status:** done

### Step 3: record/clear in `_deliver_prompt`
**Changes:** `telegram_gateway/bot.py` (`_deliver_prompt`, `parked_kind`)
**Done when:** a park leaves a hold in `state.json`; a confirmed send clears it.
**Status:** done

### Step 4: `parked_sweep_once` + loop
**Changes:** `telegram_gateway/bot.py`
**Done when:** revive/dead/expiry tests green.
**Status:** done

### Step 5: idle sweep guard
**Changes:** `telegram_gateway/bot.py` (`idle_sweep_once`)
**Done when:** the incident test flips from stop → skip.
**Status:** done

### Step 6: config + wiring + docs
**Changes:** `config.py`, `bot.py` (`post_init`/`post_shutdown`),
`cli.py`, `README.md`
**Done when:** `--check` and `gateway_started` show the tick; suite green.
**Status:** done

## Key Research Questions (answered)

- Where does a parked message actually live? Two places, and the gateway saw
  neither after the fact: a steering row (`steering_queue`) while amux has not
  accepted it, and the composer once it has. Acceptance DELETES the row
  (`session_verbs.rs` ~25206), so "queue empty" is not "delivered".
- Can the gateway see amux's composer watcher? No: `direct_draft_watches` is
  in-process state with no endpoint; its exits are WARN lines only. So the
  gateway's own record is the only durable trace of the promise it made.
- Is there a per-message receipt? `GET /api/history/{id}` exposes
  `delivery`/`delivered_at`/`queue_id`, but the send response carries no
  `cmd_history` id (only `queue_id` when amux queued it), so the gateway cannot
  join a send to a receipt today. Named as deferred work, not faked.

## Deferred Work

- **amux repo, `crates/amux-server/src/api/session_verbs.rs`:** a direct hold
  that dies (`direct_draft_lane_stopped`, ~15839, and
  `direct_draft_idle_submit_gave_up`, ~15830) exits with a WARN only; the
  sender is never told, and `/api/debug/steering` has no row for a direct hold
  (the row was deleted at acceptance). A `message.voided` event (or a
  `held` section in the steering debug route) would let any sender — not just
  this gateway — learn that its accepted text was dropped. Not edited here:
  the gateway fix makes the promise bounded on its own side.
- A per-message receipt: return the `cmd_history` id from `POST /send` and the
  gateway can join `GET /api/history/{id}` instead of inferring.

## Execution Log

| Step | Planned | Actual | Status | Notes |
|------|---------|--------|--------|-------|
| 1 | progress file | created | done | before code |
| 2 | state holds | `mark_parked`/`parked`/`clear_parked`/`parked_holds`/`note_parked_try`, bounded, in `tracked_sessions` | done | roundtrip + preview tests |
| 3 | record/clear | `_note_parked` records (`parked_tracked`), `_deliver_prompt` clears (`parked_cleared`) | done | notice now names the deadline |
| 4 | parked sweep | `parked_sweep_once`/loop: start, return, expire, unmeasured = decide nothing | done | 6 tests |
| 5 | idle guard | `idle_stop_skipped_parked` before the cards probe | done | incident test + release test |
| 6 | config/docs | `parked_sweep_s`/`parked_hold_max_s`/`parked_revive_s`, wiring, `--check`, `gateway_started` | done | 431 passed |

## Human Verification

### Quick Check (< 2 min)
`grep -a 'parked_tracked\|parked_lane_started\|parked_failed\|parked_expired\|idle_stop_skipped_parked' ~/.amux/logs/telegram-gateway.log | tail`

### Expected Results
- A message sent to a busy/held lane logs `parked_tracked … session=… kind=…`
  and ONE topic line that says when the owner will hear back.
- A lane stopped with a queued message logs `parked_lane_started … session=…`
  within a tick and the queue drains.
- A message whose pane died logs `parked_failed … reason=lane-stopped` and the
  topic gets one line: not delivered, send it again.
- No `idle_stop` for a lane with a live hold.

### Known Limitations
- Delivery itself is still inferred (queue row + lane state), never receipted;
  the honor of "did the worker read it" remains amux's.
- A human's real draft (`reason=draft`) is never cleared by the gateway: the
  owner is told at the deadline and the text stays where the human put it.
