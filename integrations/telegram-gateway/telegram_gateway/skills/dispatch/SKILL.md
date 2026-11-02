---
name: dispatch
description: Dispatcher procedures for a general-mgr worker — open a Telegram topic for a session through the gateway (chat_id rules, response codes), dispatch a task to a spawned worker end to end, decide what to do when the forum group is missing, and decide when a topic can be deleted. Use when routing demand as the fleet's dispatcher.
---

# dispatch — the dispatcher's procedures

Companion to your standing instructions: the exact calls and the decisions
that should not be re-derived. Your notes (`~/.amux/notes/worker/general-mgr/`)
hold the current project map.

## 1. Open a topic for a session

```bash
curl -sS -X POST http://127.0.0.1:8834/topic \
  -H 'content-type: application/json' \
  -d '{"session":"<session>","chat_id":-1004274385104,"name":"<name>","prompt":"<the task, verbatim>"}'
```

| answer | meaning | do |
|---|---|---|
| `201 {"thread":…,"echoed":true}` | created, task echoed as its first message + anchor; the topic follows the worker's OpenCode title once it lands | proceed; never post into it yourself |
| `201 {"thread":…,"recreated":true}` | the old topic was DELETED in Telegram — released and recreated, intro included (owner's default, 2026-11-02) | proceed; tell the owner it was rebuilt |
| `201 {"thread":…,"echoed":false}` | created, echo failed | proceed; the topic has no context — mention the task in your owner summary |
| `200 {"created":false}` | the session already has its topic (no `reconnect`) | proceed (idempotent; prompt echoes only on creation) |
| `200 {"echoed":true}` | `reconnect`: your context landed in the LIVE topic | done — the topic is verified alive |
| `409 name_exists` | another topic carries `name` | pick a distinct name, retry once |
| `400 chat_id required` | the session is not bound to any chat yet | add `"chat_id":-1004274385104` |
| `502 {"error":…}` | Telegram refused — group missing or bot not admin | §3 |

Two flags for CONNECTING to a conversation that may already have a topic
(the owner's default behavior, 2026-11-02): pass `"reconnect": true` and
`"intro": true` — the gateway then verifies the existing topic (the send
is the detector; Bot API has no message-getter), releases + recreates it
when it was deleted, and opens it with the conversation's last messages so
the owner sees where it stopped. A plain spawn needs neither flag.

Never call the Bot API directly (no `createForumTopic`, no `sendMessage`, no
bot token): a topic outside the gateway binds nothing, so the owner's message
in it starts a duplicate worker and the relay has no anchor.

### 1b. Post a report INTO a bound topic

```bash
curl -sS -X POST http://127.0.0.1:8834/message \
  -H 'content-type: application/json' \
  -d '{"session":"<the bound session>","text":"<the report, verbatim>"}'
```

| answer | meaning | do |
|---|---|---|
| `201 {"thread":…}` | posted into the topic bound to that session, recorded | done — the report is where the work lives |
| `409 no_topic` | the session has no topic bound yet | open it with §1 first, retry once |
| `400` | missing/blank `session` or `text`, or no chat has the session | fix the payload (add `"chat_id":-1004274385104` if the session is unseen) |
| `502 {"error":…}` | Telegram refused | §3 |
| `502 {"released":true}` | the topic no longer exists (deleted in Telegram) | the binding is released for you — open it again with §1 when the work resumes |

You never name a thread — the gateway resolves the topic from its own
binding, so a stale id can never route your report into the wrong topic.
One report, one POST; then your `—— answer ——` reply to the owner stays the
short version.

## 2. Dispatch a task end to end

1. **Pick the project**: `~/Documents/pv/projetos/<…>` — confirm purpose from
   its README/AGENTS.md (`~/.amux/notes/worker/general-mgr/root-projects.md`
   is a snapshot, not the truth).
2. **Spawn the worker** — or reuse/adopt one (§5 for adopting an EXISTING
   conversation into a lane) — `POST $AMUX_URL/api/sessions` with a name that says
   the task, `dir` set to the project, `start:true`, the task as its first
   prompt/instructions, and **PINNED**: mint a conversation id
   (`ses_disp<MMDDHHMM><6 hex>`) as `flags="--session <id>"`. Pinning is what
   makes its answers relay into its topic (the gateway reads them from the
   shared OpenCode server; an unpinned worker can only be pane-scraped, and
   its answers may arrive nowhere). Reuse a live worker when one already fits
   (`amux sessions` — don't spawn a second of what exists).
3. **Open its topic** (§1) with `chat_id`, `name` = short task name, and the
   task as `prompt` (echoed as the topic's first message + turn anchor).
4. **Track it** — `POST $AMUX_URL/api/board` with `session` = the worker, so
   the work survives the conversation and your board sweep can see it.
5. **Post the dispatch report into its topic** (§1b) — what went, where it
   lives, what signal to expect — so the report is where the work lives.
   Then tell the owner ONE line in their thread (same content, short). Then
   you are free.

Board sweep (you do this when idle): open cards with no live session, or a
session that is stopped with cards assigned — that is a dispatch that needs
re-placing.

## 3. The forum group is missing or silent

Signs: `502` mentioning the chat / "message to group not found" / "not enough
rights", or `400 chat_id required` even WITH the chat id.

Then reply to the owner asking for exactly this: **create the forum group
(topics enabled) or invite @homuxBot into it, and add @homuxBot as an admin
able to manage topics.** One ask, quoted verbatim if useful; retry the topic
call after they confirm.

## 4. When to suggest deleting a topic

Suggest once, name the topic, give the reason — never nag:

- the task finished and the owner confirmed (or the worker reported done);
- the dispatch was cancelled or superseded by a newer one;
- two topics are the same conversation (name the duplicate);
- idle for weeks with no open cards behind it.

Bots cannot delete topics: your reply names what is safe to remove; the owner
deletes it in Telegram.

## 5. Connect a topic to an EXISTING conversation

**First look at the owner's button**: 🧩 *Sessions by project* (it rides on
your answers, or `/browse`) lists every project's conversations and, on a
tap, does steps 2–3 below itself — adopt into a pinned lane, open the topic,
post the last messages as the intro. Point them at it when they ask to
"resume" or "connect" something. The manual path stays yours for when you
must do it yourself (or the OpenCode service is unreachable for the button):

1. **Find the id** (every project, with titles):

   ```bash
   python3 - <<'PY'
   import json, os, base64, urllib.request
   d = json.load(open(os.path.expanduser("~/.local/state/opencode/service.json")))
   req = urllib.request.Request(d["url"] + "/api/session", headers={
       "Authorization": "Basic " + base64.b64encode(b"opencode:" + d["password"].encode()).decode()})
   for s in json.load(urllib.request.urlopen(req))["data"]:
       print(s["id"], "|", s.get("title") or "(untitled)")
   PY
   ```

   (`~/.config/opencode/service.json` holds the password alone; the state
   file above holds url+password together — read it, never invent an id.)

2. **Adopt** — `POST $AMUX_URL/api/sessions`:
   `{"name": "<task-ish name>", "provider": "opencode", "dir": "<the project
   that conversation belongs to>", "flags": "--session <ses_id>",
   "start": true}`. `--session` RESUMES an existing conversation — the same
   mechanism that revives idle managers — so it continues where it left off.
   `dir` matters: a conversation belongs to the project it was made in.
3. **Topic it** (§1, with `chat_id`, and for a connect ALWAYS pass
   `"reconnect": true, "intro": true`): the gateway verifies the topic —
   a topic deleted in Telegram is released and RECREATED with the
   conversation's last messages as its intro, a live one gets your context
   landed in it, so "where we stopped" is what the owner opens to. The pin
   is recorded automatically, so answers relay — including turns driven
   from the dashboard or CLI — and leave your orientation note with §1b if
   you have one.

## 6. Staying free

If answering would take longer than dispatching, dispatch. Your thread is a
queue of decisions, not a workbench — the next customer demand must find you
idle.
