# Active context — amux Telegram gateway (worktree)

**Repo:** `/home/pv/Documents/pv/projetos/amux-telegram-gateway`
**Branch:** `feat/telegram-gateway` (based on `upstream/main` @ `8db48a461`)
**Memory bank is LOCAL-ONLY** (fork decision #1): excluded via `.gitignore`
(`memory-bank/`), never staged, never committed.

## Current task

**DONE + LIVE (this session) — STALL NOTICE PAGES ONCE PER MESSAGE (detail →
`progress/tg-stall-notice-dedupe.md`).** Owner: "continuo recebendo mensagem de
que está stalled e eu estou evoluindo no terminal". Cause: two GM boot-delivery
rows (2026-11-25 19:48) on lanes whose opencode is *suspended* in the pane
(`[1]+ Stopped` → `not-running`, measurably true), `steer_dead_letter_verdict`
never reaps `not-running` by design, amux re-WARNs hourly and the gateway
re-paged every hour (10 each). Fix: the ROW (`last_skip_id`) is the identity —
same row suppressed (`stall_notice_suppressed`), new row pages, drained queue
logs `stall_notice_cleared`; a row older than the process is seeded on deploy
(`stall_notice_seeded`) so a restart never re-pages; the notice now says a
stopped lane waits for a start instead of "its terminal shows what it is
blocked on". **Verified:** suite 422 passed (413 + 9 new), restarted 11:08:58,
`/health` ok, both rows seeded live (no new Telegram message). The two rows were then dropped
on the owner's "apaga" (`DELETE …/steer`, queue 0, `stall_notice_cleared` ×2).

**Older (one line each — full detail in the linked progress file):**
- **2026-11-26** relay releases a deleted topic → `progress/tg-relay-thread-missing-release.md` (413 green, live).
- **2026-11-26** steering stall root cause → `progress/tg-steering-stall-root-cause.md` (Rust half NOT deployed).

**Older (one line each — full detail in the linked progress file):**
- **2026-11-14** transcribe-only topic (`db5a1c925`, deployed 18:33:46) → `progress/tg-transcribe-only-topic.md`. Owner verify still open: one audio in thread 1711; pre-existing topic 1701 still wakes a lane.
- **2026-11-14** photo input rides the text path (`86b291895`, deployed 11:59:02) → `progress/tg-photo-input.md`. Owner verify open: resend the photo in topic 1548.

- **2026-11-07** muted-relay Revision 3 (`38b07ee16`, deployed 21:10:58) → `progress/muted-relay.md` §Revision 3. 5-day check: 320 relays → 38 notified / 282 muted, zero `anchor_held`; `relay_turn_quiet reason=no_sentinel` fires for workers that never stamp.
- **FIXED 2026-11-26 (was OPEN, found 2026-11-07):** the RELAY path never
  released a dead topic binding (`relay_send_failed … Message thread not found`
  68× on Nov 7, again live on the owner's deleted GM topic 544 on Nov 25). Both
  relay paths now release + deliver in General →
  `progress/tg-relay-thread-missing-release.md`.
- **2026-11-07** idle-clock reset + parked/stall observability (384 green, not pushed) → `progress/tg-idle-clock-and-parked-delivery.md`. Knobs `AMUX_SERVER_LOG`, `TELEGRAM_GATEWAY_STALL_WATCH_S`. Its deferred amux-side self-heal is now DONE in this session's progress file.

**Older (one line each — full detail in the linked progress file):**
- **2026-11-02** reconnect+intro default & fuzzy session search (`1ba9a052b`,
  PUSHED) → `progress/tg-reconnect-topic-and-session-search.md` (that commit
  also deployed the muted relay's turn-scope fix); muted-relay first ship
  (`d1b97cb70`, 325 → 339 green) → `progress/muted-relay.md`.
- **2026-10-31** pinned menu (`f3fcce35f`, 325 green) + dispatch reports →
  their own topic + 🧩 session browser (`c92fbf7d6`, 313 green) →
  `progress/tg-pinned-gm-menu.md`, `progress/tg-dispatch-report-and-session-ui.md`
  (owner verify still open: pinned menu, 🧩 adopt).
- **2026-10-31/30** M11.9 connect-existing (`5a6247c72`) · M11.8 title-follow
  (`47fd7e0e1`) · M10 registry + M11 + M11.5–11.7 → `progress/telegram-gateway.md` §10.1–11.9.

**Track B next (amux repo, owner-decided):** ONE fleet-wide dispatcher
(Alternative A+ii, see Next steps) — design doc first; hand-off: wire M7's
`set_model` into amux's config PATCH (our serve-API path is the hot switch
until then). Prior milestones: detail in `progress/telegram-gateway.md`
(2026-10-29 relay/pollwatch/echo · M9 `d13a26f49` · M8 · 10-27 dedup).

## Next task

**Owner's item (c) — DEFERRED, do last:** why two manager sessions died at
boot under load (evidence: `amux.db` session_events + gateway log 09:29).
Idle stop + same-session revive is built/deployed (2026-10-27, `idle_stop_s=1800`,
detail → `progress/tg-idle-stop-opencode-resume.md`); its live Telegram E2E is
still pending. The `--model`-breaks-opencode bug stays deferred (owner).

## Decisions locked

- **Topic rule**: forum General → the sender's dispatcher (no mint); topic →
  continue; unmapped → adopt; quote-reply keeps its old meaning elsewhere.
- **Topic lifecycle**: create-failure / 404 send / `thread_missing` on
  `POST /message` / confirmed stop → `topic_released`, the next message
  adopts fresh there.
- **Tracked topic path** `127.0.0.1:8834`: `POST /topic` (create+bind+echo)
  and `POST /message` (deliver into the BIND topic — never a caller thread
  id), loopback-only, `CONTROL_PORT=0` disables. `GatewayState` is the only
  writer of `state.json` while the gateway runs (**Rebind rule**: re-point
  only while STOPPED).
- **Session identity**: pin-first (`topic_pins` → `--session ses_…`), name
  fallback; the resolver NEVER adopts; renames rekey every map and pop
  `topic_created`.
- **Opencode controls = its own serve API** (M9+M10): lane child → SHARED
  `opencode serve --service` (registry `~/.local/state/opencode/service.json`)
  → amux PATCH/Escape fallbacks; model+effort in ONE hot POST (even a STOPPED
  lane); never an effort PATCH (rides `CC_FLAGS`, kills the lane).
- **Fresh sessions** (M10): conversation disposable, board/notes durable;
  reset = RESPAWN; sweeps defer when in use/blind/card-owning; a dead tracked
  manager pages the owner ONCE (`dispatcher_down`/`owner_paged`).
- **Browser adopts** (new): reuse a lane already pinning the `ses_…`, else
  mint `adopt-…` pinned in the conversation's project dir; topic through
  `create_topic` in-process; intro = last 3 completed assistant messages via
  `/topic`'s `prompt`; ids in callbacks, project indexes resolve against the
  list AS RENDERED.
- **Brief self-heal** (new): existing dispatcher re-briefed only on PROVEN
  drift (`instructions()` non-None ≠ packaged); save-only, never disturbs a
  live conversation.
- **Own bot token** — one `getUpdates` consumer per token. Dedup: 30 s
  window, per chat, in-memory. Reply boundary `—— answer ——`.
- Outage 09:2x root-cause chain → `progress/telegram-gateway.md` step 7.7.

## Next steps

0. **Owner verify (2026-11-07, this turn):** `grep -a
   'idle_clock_reset\|clock_reset=1' ~/.amux/logs/telegram-gateway.log` after a
   revive (the lane must then survive a sweep — no `idle_stop … idle_s=33…`
   within a minute of it); a prompt held behind a composer draft posts ONE line
   in its topic (`grep -a send_parked\|parked_notice_sent`); a NEW
   `steering queue STALLED` line in `server-rs.log` for a topic-bound session
   posts ONE line there (`grep -a stall_notice_sent`).
0. **Owner verify (this turn):** dispatch a task — the topic fills with the
   worker's progress lines SILENTLY (no buzz), then the `—— answer ——` answer
   buzzes exactly ONCE (a long, chunked answer still once);
   `grep -a relay_sent ~/.amux/logs/telegram-gateway.log | tail` shows
   `notify=False` per progress line and `notify=True` on the answer.
0. **Owner verify (2026-10-31):** open the Telegram group — the **🤖 amux
   gateway** menu is pinned at the top (tap → General); `/gmmenu` re-asserts
   it and reports `pinned` (`grep -a gm_menu
   ~/.amux/logs/telegram-gateway.log` → `gm_menu_pinned … msg_id=293`,
   always the same id).
0. **Owner verify (2026-10-31 earlier):** tap **🧩 Sessions by project**
   (or `/browse`) → adopt a conversation → topic with its last messages as
   intro; dispatch something and its report appears in the worker's topic
   too (`grep -a topic_message_posted …`). Leftover stopped lane
   `adopt-1031-1943-f91b` can be deleted from the dashboard.
1. **Track B (next work, amux repo):** the ONE dispatcher manager — design
   doc first (Alternative A+ii), then implementation there. The gateway side
   is ready: `POST /topic`, `POST /message`, shared-service client, reset,
   paging, the browser UI.
2. **Owner: promote @homuxBot to group admin (manage topics)** if not done —
   inbound from the group still needs it.
3. **Push `feat/telegram-gateway` when the owner says so** (carries M8–M11 +
   this feature + the upstream/serve-transport merges; ship order:
   feat/serve-transport → upstream/main → this branch).
4. **Rebase vs upstream/main (owner call):** keep upstream's
   `drop_model_flags` family over this branch's older `--model` strip; the
   branch is pushed, so it is a force-push decision.
5. Owner-verify lists for M8–M11 (dispatcher General routing, 🎛 panel,
   model/effort/interrupt, 🔄 Fresh session, paging) →
   `progress/telegram-gateway.md` §Human Verification (greps live in the log
   today). ~~Human-only cleanup~~ done 2026-10-30; ~~gateway-minted topic
   names~~ dormant by design since 11.5.
