# Phase/Feature: relay releases a deleted topic (thread_missing)

**Dependencies**: `telegram-gateway.md` (relay, `POST /topic`, `POST /message`),
`tg-dispatch-report-and-session-ui.md` (topic bindings), `muted-relay.md`.
**Enables**: a dispatch whose topic the owner deleted heals itself instead of
parking the GM's answers in limbo until a human calls `POST /message`.

## What Works

- `/message` releases the binding on `Message thread not found`
  (`control.py`, `topic_released … reason=thread_missing source=message`, 502
  with `released:true`).
- `/topic` `reconnect` releases a DEAD binding and recreates it
  (`source=connect`).
- The relay paths (`_relay_api`, pane path) only log `relay_send_failed` and
  give up for this cycle — they keep the dead binding, so every later answer
  fails the same way. **This file fixes that.**

## What's Left to Build

### Milestone 1 — one detector, one release, a retry without the thread

- [ ] Move the dead-topic predicate to `relay.py` (`thread_missing(exc)`) so
      the control routes and the relay share ONE implementation.
- [ ] `bot.py`: on `thread_missing` while sending a relayed answer, release the
      (chat, session) binding and log
      `topic_released … reason=thread_missing source=relay path=<api|pane>`.
- [ ] Retry the SAME message once with `thread_id=None` and `reply_to=None`
      (the anchor lived in the deleted topic) so it lands in General instead
      of failing again; the rest of the batch follows the healed route.
- [ ] If the retry fails for another reason, keep today's behavior: one WARN
      (`relay_send_failed … retried_without_thread=true`) and the message stays
      pending (`stored` / `last_relayed` do not advance).

## Current Status

Planned. Root cause confirmed in code (both relay send sites, 2026-11-26).

## Detailed Plan / Design

Live evidence (owner, 2026-11-25 12:09–12:13): the owner deleted topic 544
("General manager dispatch workflow"); the owner's General message reached the
GM (`voice_transcribed … route=dispatcher`) but every relayed answer failed
`relay_send_failed chat_id=-1003797327543 session=general-mgr-823440998
error=Message thread not found` for four minutes. The GM only unblocked after
a manual `POST /message`, which returned `502 {"error": "Message thread not
found", "released": true}` and freed the binding — the final answer then landed
in General.

So the CONTROL path already heals and the RELAY path does not, for the same
Telegram error. The asymmetry is the bug: the relay is the path that is
supposed to be hands-off, and it is the one that parks a live answer behind a
binding only a human could clear.

Design decisions:

1. **`thread_missing` lives in `relay.py`.** It is a pure string predicate with
   no imports; `control.py` and `bot.py` both import it. Two copies of the
   "which error proves the topic is gone" rule is how the two paths drifted in
   the first place.
2. **Release, then retry ONCE.** A dead thread proves the topic does not exist,
   so the retry cannot double-post: nothing was delivered. Posting the same
   text into General is the answer the owner was waiting for, and it keeps the
   dedupe honest (`oc_relayed_id` / `last_relayed` advance only on success).
3. **`reply_to=None` on the retry.** The turn anchor was a message IN the
   deleted topic; replying to it would fail with a second error class
   (`message to be replied not found`). The answer goes to General unanchored,
   exactly like a session with no topic.
4. **WARN, not INFO.** A dead topic is a state a sweep must see: the line
   carries `reason=thread_missing source=relay path=<api|pane>`.
5. **Nothing else changes.** `/topic` and `/message` keep their own release
   paths (already tested in `tests/test_control.py`); the relay only gains the
   missing half.

## Execution Steps

### Step 1: Share the dead-topic predicate
**Changes:** `telegram_gateway/relay.py`, `telegram_gateway/control.py`
**Done when:** `thread_missing("Message thread not found")` is True, a rights
error is False, and `tests/test_control.py` is green.
**Status:** done

### Step 2: Release + retry in both relay send sites
**Changes:** `telegram_gateway/bot.py` (`_release_missing_thread`,
`_send_relay_text`, `_relay_api`, pane path in `relay_once`)
**Done when:** the new tests in `tests/test_relay_topic_release.py` pass.
**Status:** done

### Step 3: Prove the three contracts
**Changes:** `tests/test_relay_topic_release.py`
**Done when:** (a) a deleted-thread send releases the binding, (b) the answer
lands once in General with `thread=None`, (c) `/topic` + `/message` tests stay
green and a rights failure still never releases.
**Status:** done

### Step 4: Run the suite, restart the gateway, verify health
**Changes:** none (ops)
**Done when:** `python3 -m pytest` green; `systemctl --user restart
amux-telegram-gateway`; `ActiveState=active`; `GET /health` ok.
**Status:** done

### Step 5: Commit code + this progress file's reference (memory-bank stays local)
**Changes:** `integrations/telegram-gateway/telegram_gateway/{relay,control,bot}.py`,
`integrations/telegram-gateway/tests/test_relay_topic_release.py`
**Done when:** one commit with `Progress: memory-bank/progress/tg-relay-thread-missing-release.md`.
**Status:** done (`1b6d15813`)

## Success Criteria

- A relayed answer whose topic was deleted is released and delivered to General
  in the SAME cycle, with one grepable `topic_released … source=relay` line.
- The next cycle does not retry the dead thread (binding is gone).
- `/topic` and `/message` behave exactly as before.
- A non-thread Telegram failure still leaves the binding untouched.

## Discoveries

- 2026-11-26: `activeContext.md` already carried this as an OPEN FINDING from
  2026-11-07 (`relay_send_failed … Message thread not found` 68× on Nov 7):
  the relay never released a dead binding. The owner hit the same defect live
  on 2026-11-25. Same bug, two sightings, one root cause.

## Implementation Notes

- The retry reuses `_send_markdown`, so the Markdown→plain fallback and the
  chunking/`notify` rules are identical to the first attempt.
- Both relay paths call one helper (`_send_relay_text`), so the pane and API
  paths cannot drift again.
- `path=api|pane` rides the log line so a sweep can tell which channel healed.

## Known Limitations

- A topic deleted BETWEEN two chunks of one multi-chunk answer replays the
  whole text in General on the retry (the chunk that already landed is sent
  again). Deliberate: losing the answer is the defect being fixed, and the
  window is milliseconds. `_send_markdown` does not return partial progress.

## Deferred Work

- `reply_to` pointing at a deleted MESSAGE while the thread is alive answers
  `message to be replied not found`; those sends still fail until the anchor is
  reset. Different error class, not measured live, out of this fix.
- The topic is released, not recreated: replies continue in General (the
  owner's stated preference in the incident) and a later `/topic` reconnect
  can open a fresh topic.

## Execution Log

| Step | Planned | Actual | Status | Notes |
|------|---------|--------|--------|-------|
| 1 | move predicate to relay.py | `thread_missing` in relay.py; control imports it | done | docstring keeps the live evidence |
| 2 | helper + both send sites | `_release_missing_thread`, `_send_relay_text` | done | release + one retry without thread |
| 3 | new tests | 5 tests in `test_relay_topic_release.py` | done | api, pane, rights, no-duplicate |
| 4 | suite + restart | 413 passed (408 before + 5 new); restart + /health ok | done | 6 pre-existing PTB warnings |
| 5 | commit | `1b6d15813` on `feat/telegram-gateway` (not pushed) | done | memory-bank force-added, like `f8cf229ad` |

## Human Verification

### Quick Check (< 2 min)

```bash
grep -a 'topic_released .*reason=thread_missing source=relay' \
  ~/.amux/logs/telegram-gateway.log | tail
```

### Expected Results

- After deleting a topic that has a live session, the next answer appears in
  General instead of `relay_send_failed` repeating every cycle.
- Exactly one `topic_released … source=relay path=…` line per deleted topic.

### Known Limitations

- The verification above needs a deleted topic; it was proven by tests here,
  not by deleting one of the owner's topics.
