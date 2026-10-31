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
| `201 {"thread":…,"echoed":true}` | created, task echoed as its first message + anchor | proceed; never post into it yourself |
| `201 {"thread":…,"echoed":false}` | created, echo failed | proceed; the topic has no context — mention the task in your owner summary |
| `200 {"created":false}` | the session already has its topic | proceed (idempotent; prompt echoes only on creation) |
| `409 name_exists` | another topic carries `name` | pick a distinct name, retry once |
| `400 chat_id required` | the session is not bound to any chat yet | add `"chat_id":-1004274385104` |
| `502 {"error":…}` | Telegram refused — group missing or bot not admin | §3 |

Never call the Bot API directly (no `createForumTopic`, no `sendMessage`, no
bot token): a topic outside the gateway binds nothing, so the owner's message
in it starts a duplicate worker and the relay has no anchor.

## 2. Dispatch a task end to end

1. **Pick the project**: `~/Documents/pv/projetos/<…>` — confirm purpose from
   its README/AGENTS.md (`~/.amux/notes/worker/general-mgr/root-projects.md`
   is a snapshot, not the truth).
2. **Spawn the worker** — `POST $AMUX_URL/api/sessions` with a name that says
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
5. **Tell the owner one line**: what went, where it lives, what signal to
   expect. Then you are free.

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

## 5. Staying free

If answering would take longer than dispatching, dispatch. Your thread is a
queue of decisions, not a workbench — the next customer demand must find you
idle.
