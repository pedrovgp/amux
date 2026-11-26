# Phase/Feature: one stall notice per undelivered message (not one per hour)

**Dependencies**: `tg-steering-stall-root-cause.md` (the stall notice now names
`last_skip`/`blocked_now`), `tg-idle-clock-and-parked-delivery.md` (the notice
exists at all).
**Enables**: the owner can tell a NEW problem from the same old one, and stops
receiving the same "stalled" line every hour for a message he already knows
about.

## What Works

- amux WARNs `steering queue STALLED` once per (session, condition, hour bucket)
  into `~/.amux/logs/server-rs.log` — that is for greps and is correct.
- The gateway tails that log and posts ONE Telegram notice per new line, with
  the reason (`stall_notice_sent … reason=last_skip=…, blocked_now=…`).
- `GET /api/debug/steering` publishes, per lane, the durable identity of the
  undelivered work (`last_skip_id`), `queued`, `oldest_age_s`,
  `blocked_reason`, `deliverable` and `measured` (AF-320 contract).

## What's Left to Build

### Milestone 1 — page per undelivered ROW, not per hourly WARN

- [x] `AmuxClient.steering_lanes()` — the measured per-lane view (`None` when
      the probe did not run; transport errors never read as "queue empty").
- [x] `stall_watch_once`: suppress a notice whose row key the owner was already
      paged about for that (chat, session); `stall_notice_suppressed` is logged.
- [x] Close the loop: when the lane no longer has queued rows, forget the key
      and log `stall_notice_cleared` — a later stall pages again.
- [x] The notice states the ACTIONABLE fact per reason, not a generic line: a
      stopped lane says the message waits for the lane to start and that no
      deadline forces it through.
- [x] A DEPLOY does not re-page: a row older than this process is seeded
      (`stall_notice_seeded`), a row queued after the start still pages.

## Current Status

DONE + LIVE (2026-11-26 11:08:58). Root cause confirmed live: two GM boot-delivery rows
(2026-11-25 19:48 -03) sit on lanes whose opencode process is *Stopped* in the
pane (shell prompt), so `lane_block_reason=not-running` is measurably true and
`steer_dead_letter_verdict` deliberately never reaps `not-running` (the
2026-08-19 panic: 41 lanes down 6.5h, every row delivered on restart). amux
therefore re-WARNs hourly, and the gateway re-paged hourly, forever.

## Detailed Plan / Design

The defect is the DOUBLE dedupe: amux dedupes per hour, the gateway treats each
hour as a new stretch. To the owner that is one stuck message and forty
notifications.

- The ROW (`last_skip_id`) is the identity, not the log line and not the hour.
  `oldest_age_s` grows every hour and `queued` may stay at 1, so neither can be
  the key; the id cannot drift.
- The API is the measured truth; the log line is its copy. When the API is
  unmeasured, fall back to a condition key (`last_skip`+`blocked_now`) so an
  unchanged condition still pages once per process rather than once per hour.
  A probe that failed is never read as "nothing queued".
- In-memory only: the reader starts at EOF, so a restart does not replay, and
  one extra page after a restart is honest (the operator restarted the pager).
- Nothing is deleted or dead-lettered here. Dropping a `not-running` row on age
  is a semantics change with a documented counter-incident; the owner decides.
  The gateway's job is to say it once, accurately.

## Execution Steps

### Step 1: read the lanes
**Changes:** `telegram_gateway/amux.py`
**Done when:** unmeasured/error answers `None`, never an empty dict; queued
lanes carry `last_skip_id`.
**Status:** done

### Step 2: dedupe + close the loop + honest advice
**Changes:** `telegram_gateway/bot.py` (`_stall_notified`, `_stall_notice_key`,
`_stall_lanes`, `_stall_clear_resolved`, `stall_notice_text`)
**Done when:** tests in `tests/test_stall_notice_dedupe.py` pass.
**Status:** done

### Step 3: prove the contracts
- same row twice → one notice, second suppressed;
- new row id → a second notice;
- queue drains → `stall_notice_cleared`, next stall pages again;
- unmeasured queue → one notice per condition, never one per hour;
- `/topic`, `/message`, relay tests stay green.
**Status:** done — `tests/test_stall_notice_dedupe.py` (9) + suite 422 passed

### Step 4: suite + restart + verify
**Status:** done — restart 11:08:58, both obsolete rows SEEDED live

### Step 5: commit (+ push, owner asked) and the projectDecisions correction
**Status:** pending

## Success Criteria

- Two hourly WARNs about the SAME undelivered message produce exactly one
  Telegram notice.
- A genuinely new blocked message produces a new notice.
- A stopped lane's notice says the message waits for the lane to start.
- No Telegram notice is ever sent without a matching log line.

## Discoveries

- The owner reads these notices as "something is strange" precisely because he
  IS working in his terminal: the lane's amux pane holds a *suspended*
  opencode (`[1]+ Stopped`), so amux's `not-running` is true while his own
  terminal session is alive. The notice has to say which of the two it means.

## Implementation Notes

- `stall_watch_once` keeps amux's log as the TRIGGER; the API only supplies the
  identity. No new poller, no second source of truth.

## Evidence preserved before the owner's drop (2026-11-26)

Owner: "apaga" (drop the two obsolete rows). Recorded verbatim from
`steering_queue` BEFORE deleting, so the evidence survives the delete:

```
boot-delivery:ocusage:1795635401:Owner:510292caf88dbbac7ac68d12a297602fbd81dbe1fea6a0de65ea26a9373dbe55
  queued_at 1795646888 (2026-11-25 22:48 UTC / 19:48 -03)  guard=''  sender=''
  "Retomada (general-mgr). Suas mensagens do Telegram das 15:33/15:38/16:00/16:36
   ficaram presas no steering do amux (`send-refused: composer`) ... Continue a
   tarefa OCUsage do ponto em que parou ..."

boot-delivery:tg-fix-steering-stall:1795636378:Owner:2ef173b3edcc6920a6254146c6f469cd4d0460dd016c2efed4ed05aa032ceb8f
  queued_at 1795646888  guard=''  sender=''
  "Retomada (general-mgr). A sua mensagem das 17:43 ficou presa no steering ...
   Retome a correção da causa raiz do `steering queue STALLED` ..."
```

Dropped through the SANCTIONED route (`DELETE /api/workers/<lane>/steer` with
`{"id": …}`), not by hand-editing `amux.db`.

## Deferred Work

- Auto-start (or dead-letter) a `not-running` hold: owner decision, unchanged
  semantics of `steer_dead_letter_verdict`.
- The two obsolete GM rows (2026-11-25) were DROPPED on the owner's "apaga"
  (2026-11-26 13:26), through `DELETE /api/workers/<lane>/steer`; their text is
  preserved above. Nothing else was touched.

## Execution Log

| Step | Planned | Actual | Status | Notes |
|------|---------|--------|--------|-------|
| 1 | lanes reader | `steering_lanes()`, `steering_queued_sessions` built on it | done | |
| 2 | dedupe + advice | row key, seed-on-deploy, cleared loop, NOT RUNNING advice | done | |
| 3 | tests | 9 new; suite 422 passed (413 before) | done | |
| 4 | suite + restart | `seeded … oldest_age_s=55249` for both rows, no new notice | done | |
| 5 | commit + push | `fix` `7cc949690` + `docs` `e2715d418`, pushed | done | |
| 6 | owner drop of the two rows | `DELETE …/steer` ×2 → cleared 1+1; queue 0; `stall_notice_cleared` ×2 live | done | evidence in this file |

## Human Verification

### Quick Check (< 2 min)

```bash
grep -a 'stall_notice_suppressed\|stall_notice_cleared' \
  ~/.amux/logs/telegram-gateway.log | tail
```

### Expected Results

- After the next hourly amux WARN about an already-reported row: a
  `stall_notice_suppressed` line and NO Telegram message in the topic.
- When the row is delivered or dropped: one `stall_notice_cleared` line.

### Known Limitations

- In-memory dedupe, seeded on deploy: a row that predates a restart is not
  re-paged (`stall_notice_seeded`), but a row's *suppression* list is lost if
  the process dies before its first measured tick — then one more notice can go
  out. Bounded by construction: one per deploy, not one per hour.
- The two obsolete GM rows are intentionally still queued (owner's call): the
  notices stop, the rows do not disappear from `/api/debug/steering`.
