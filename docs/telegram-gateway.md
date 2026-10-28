# Telegram gateway

**Status:** the feature lives on branch `feat/telegram-gateway` and is **not on
`main` yet** — this page ships with that branch.

Owner-only phone bridge to amux workers: text prompts, voice notes, an inline
menu, and manager mode (a plain message starts a manager agent that answers
back in the chat).

This is a discovery page. **Setup steps, env vars and full behaviour live in
[`integrations/telegram-gateway/README.md`](../integrations/telegram-gateway/README.md)**
— that file is the feature doc; do not duplicate it here. The integration index
is [`integrations/README.md`](../integrations/README.md).

## Why it needs its own bot

Telegram delivers `getUpdates` to exactly one consumer per token. amux already
ships a built-in connector (`TELEGRAM_BOT_TOKEN`), so sharing that token would
make the two pollers fight over the same update stream — HTTP 409, stolen
updates. The gateway therefore runs as its own @BotFather bot with its own
token (`TELEGRAM_GATEWAY_BOT_TOKEN`); the built-in connector is untouched.

## Architecture

```
Telegram  <->  python-telegram-bot (integrations/telegram-gateway)  <->  amux HTTP API (:8824)
                        |
                        +-> Groq whisper-large-v3-turbo (voice notes only)
```

An isolated integration: everything lives under `integrations/telegram-gateway/`,
it talks to the local server over HTTP (`/api/sessions`, `/api/workers/...`),
and it changes no amux source.

## Surfaces

- **Menu buttons** (`/start`, `/menu`): new session · peek/status · send
  prompt · stop · manager settings (provider + model).
- **Plain message** = a **new manager** agent in `MANAGER_CWD`.
- **Quote-reply** to a bot message = continue that session.
- **Forum topics** (Topics-enabled groups): **General** → new session + new
  topic; **inside a topic** → continue that topic's session (the topic is the
  session selector, no reply needed); **unmapped topic** → a fresh manager
  adopts it. Answers post with `message_thread_id` and never reply across
  threads.
- **Voice anchor rule**: the transcription is posted as a reply to the audio,
  and the agent's answer anchors on the *transcription* (readable), never the
  audio bubble; quoting the transcription continues that session.
- **Idle stop + same-session revive**: after `TELEGRAM_GATEWAY_IDLE_STOP_S`
  (default 1800s, `0` disables) of quiet on both sides, a pinned manager lane
  is stopped to free RAM — the topic binding is kept, and the next message
  there restarts the *same* OpenCode conversation. Stop (⏹️) still releases the
  topic.
- **Topic names follow OpenCode's session title**: `TELEGRAM_GATEWAY_TOPIC_SYNC_S`
  (default 60s, `0` disables) renames a gateway-minted topic once, from the
  session name (`mgr-…`) to OpenCode's title. Hand-created topics are never
  renamed.

## Security

- `ALLOWED_TELEGRAM_USER_IDS` **hard-refuses startup when empty** — the bot
  does not run without an allow-list.
- Every handler is gated by that allow-list (message, button and voice note
  alike); denials reply `⛔ Access denied` and are logged as `security_denied`
  with user, chat and handler.
- Credentials live only in `~/.amux/server.env` (names in the repo, values
  never).
- The gateway's control port (`POST /topic`, `GET /health`) binds
  **`127.0.0.1:8834`, loopback-only**; `TELEGRAM_GATEWAY_CONTROL_PORT=0`
  disables it entirely.

## Operations

- Runs as a systemd **user** unit: `amux-telegram-gateway.service`
  (`Restart=always`, persistent venv, log appended to
  `~/.amux/logs/telegram-gateway.log`).
- Config/smoke check: `integrations/telegram-gateway/bin/telegram-gateway --check`
  (validates config, allowed IDs, voice, and reaches `GET /health`; non-zero
  exit on any problem).
- Log lines worth grepping: `gateway_started` (startup banner),
  `relay_no_boundary` (a reply could not be extracted from the pane),
  `topic_created` / `topic_renamed`, `security_denied`.

## Tests

```bash
cd integrations/telegram-gateway
python3 -m pytest -q
```

No network: the amux client runs against an `httpx.MockTransport` and voice
transcription is injected as a fake callable.
