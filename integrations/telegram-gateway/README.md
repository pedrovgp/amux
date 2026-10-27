# telegram-gateway

Owner-only Telegram bridge for amux. Two-way chat with amux workers from your
phone: text prompts, voice notes, an inline menu, and automatic replies back to
the chat.

It is an **isolated integration**: everything lives in this directory, it talks
to the local server over HTTP (`/api/workers/...`), and it changes no amux
source. It runs as its **own Telegram bot**, separate from amux's built-in
`TELEGRAM_BOT_TOKEN` connector — Telegram delivers `getUpdates` to one consumer
per token, so sharing one token would make the two pollers fight (HTTP 409).

```
Telegram  ⇄  telegram-gateway (this module)  ⇄  amux server (https://localhost:8824)
                     │
                     └─ Groq whisper-large-v3-turbo (voice notes only)
```

## What it does

| Surface | Behaviour |
|---|---|
| `/start`, `/menu` | Inline keyboard: 🚀 new session · 👁️ peek/status · ✍️ send prompt · ⏹️ stop · 🧠 manager settings |
| **Plain text or voice note** | Starts a **new manager agent** in `MANAGER_CWD`. In a forum group (Topics enabled) each manager gets **its own topic**; elsewhere it replies in that message's thread |
| **Quote-reply** to a bot message | Stays in the session that message belongs to |
| Voice note | Downloaded, transcribed by Groq, echoed as `🗣️ Transcribed: …` **as a reply to the audio**, then treated like the text would be; the agent's answer replies to the transcription (readable), never to the audio bubble, and quoting the transcription continues that session. The temp `.ogg` is deleted immediately |
| Replies | A relay peeks the session every ~20s and posts new output **as a reply to your message** |
| Access | Every message, button and voice note must come from `ALLOWED_TELEGRAM_USER_IDS`; denials are explicit and logged as `security_denied` |

### Manager mode

A message that is not a quote-reply starts a fresh **manager agent**: a fleet
lane in `~/Documents/pv/projetos`, briefed with `manager_prompt.md` (override
with `MANAGER_INSTRUCTIONS_FILE`). The role is deliberate: answer questions
briefly, delegate real work to other lanes through the amux CLI/API
(`amux board`, `amux send <name> --stdin`, `POST $AMUX_URL/api/board`), research
is fine, hands-on building rarely is. A non-isolated lane inherits
`AMUX_URL`/`AMUX_SESSION`, the `amux` CLI and amux MCP, so it can file cards and
task peers on its own.

Which agent starts is yours to choose: **🧠 Manager settings** lists providers
(`MANAGER_PROVIDER`, default `opencode`) and each provider's models from
`GET /api/models`. Idle managers stay running until you stop them (⏹️).

The thread rule is strict and Telegram-shaped: **quote any bot message — the
answer, the "started" ack — to continue that session; any other message starts
a new one.**

### Forum topics (groups with Topics enabled)

In a forum group the **topic replaces the quote**:

- **General** → a fresh manager; the gateway creates a topic named after the
  session (`mgr-…`) and every answer lands in it.
- **Inside a topic** → the message continues that topic's session. No reply or
  quote needed — the topic *is* the session selector.
- **An unmapped topic** (created by hand, or state loss) → a fresh manager
  that adopts the topic.
- Answers post with `message_thread_id`; an anchor outside the topic (turn 1
  of a new topic, or a quote from General) is never replied across threads —
  the answer goes in plain, inside the topic.
- A stopped or dead session **releases its topic**, so the next message there
  starts a fresh manager instead of hitting a dead lane.

Voice notes follow the same routing; the transcription stays where the voice
was sent (readable there), the answer lands in the topic.

### Idle stop, same-session revive

A topic must not cost RAM forever. `TELEGRAM_GATEWAY_IDLE_STOP_S` (default
**1800** = the owner's 30 minutes, `0` disables) stops a manager lane when BOTH
have been quiet for that long: you (no message) and the agent (no new output —
a long task is never cut off mid-turn).

- **Only pinned lanes are ever stopped.** A new opencode manager is created
  with `flags="--session ses_tg…"`; amux stores that as the lane's `CC_FLAGS`
  and replays it on every start, so the pin is what makes a restart resume the
  *same* OpenCode conversation. Managers created before this change, and
  managers on any other provider, keep running exactly as they always did.
- **The topic binding is kept.** The stop never releases the topic: the relay
  stops peeking the dead pane, and your next message there starts the same
  lane again (`start` → wait → send) and OpenCode reopens the conversation its
  pin names. Same topic, same session, only the RAM was freed.
- **Lanes that own a waiting board card are skipped** (`idle_stop_skipped_cards`
  in the log): amux's bootstrap would restart a stopped worker that owns a
  Todo/Doing card, so stopping one is a fight nobody wins.
- ⏹️ Stop from the menu keeps its old meaning: it releases the topic, and the
  next message there starts a fresh manager.

Logs to watch: `idle_stop`, `idle_stop_undone`, `idle_stop_skipped_cards`,
`revive`, `manager_pinned`, `opencode_model_dropped`.

### Meaningful topic names

A topic you start in General is born named after the lane (`mgr-1027-1536-8b4e`)
— while OpenCode titles the same conversation from its content a few seconds
after the first turn. `TELEGRAM_GATEWAY_TOPIC_SYNC_S` (default **60**, `0`
disables) makes the topic follow that title: every tick, a topic the gateway
minted for a PINNED session whose recorded name is still the session name is
renamed once to OpenCode's title (`editForumTopic`, clamped to Telegram's 128
characters) and recorded in `state.json`.

- **Topics you created by hand are never renamed** (only gateway-minted ones are
  eligible — the `topic_created` flag in state), and neither are topics opened
  through `POST /topic`, whose names are deliberate.
- **A stub waits**: `Untitled session` or an empty title leaves the topic as-is
  for the next tick, because a topic is renamed only once.
- No title is read (no process spawned) when there is nothing pending.

Logs to watch: `topic_renamed`, `topic_rename_failed`.

## Setup

### 1. Create the gateway's own bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. `/newbot`, pick a name and username (e.g. `my-amux-gateway-bot`).
3. Copy the token — this is `TELEGRAM_GATEWAY_BOT_TOKEN`.

Do **not** reuse the token already in `TELEGRAM_BOT_TOKEN` (the built-in
connector). Two pollers on one token conflict.

### 2. Get your numeric user ID

Message [@userinfobot](https://t.me/userinfobot); it replies with your numeric
id. Put it in `ALLOWED_TELEGRAM_USER_IDS` (comma-separated for more people).

### 3. (Optional) Get a Groq key for voice notes

Create a key at <https://console.groq.com/keys>. The default model is
`whisper-large-v3-turbo`, which fits the free tier for short voice notes.

### 4. Add the values to `~/.amux/server.env`

`server.env` is outside the repo and is never committed. Add:

```bash
# --- Telegram gateway (separate bot from the built-in connector) ---
TELEGRAM_GATEWAY_BOT_TOKEN=123456:your-bot-token
ALLOWED_TELEGRAM_USER_IDS=123456789
GROQ_API_KEY=gsk_your_key_here
# optional:
# AMUX_URL=https://localhost:8824
# AMUX_TLS_VERIFY=0
# AMUX_DEFAULT_WORKDIR=/home/you
# AMUX_DEFAULT_PROVIDER=opencode
# TELEGRAM_GATEWAY_RELAY_INTERVAL_S=20
# TELEGRAM_GATEWAY_IDLE_STOP_S=1800   # 0 disables the idle stop
# TELEGRAM_GATEWAY_TOPIC_SYNC_S=60     # topic titles follow OpenCode (0 = off)
```

New sessions are created with `POST /api/sessions` — a fleet lane (env file +
tmux) — because `POST /api/workers` only writes a table row and amux refuses
sends to a row that has no lane env file.

The gateway reads this file itself at startup (process environment wins), so the
server does not need a restart for the gateway to see new values.

### 5. Install dependencies

```bash
cd integrations/telegram-gateway
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### 6. Check, then run

```bash
bin/telegram-gateway --check     # validates config and reaches GET /health
bin/telegram-gateway             # starts long-polling
```

`--check` prints the allowed IDs, the amux URL, whether voice is on, and the
server's health/commit. It exits non-zero on any problem.

### 7. Keep it running (systemd user unit)

Installed on this box as `amux-telegram-gateway.service` with a persistent venv
at `~/.local/share/amux-telegram-gateway/venv`:

```ini
# ~/.config/systemd/user/amux-telegram-gateway.service
[Unit]
Description=amux Telegram gateway
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/path/to/amux/integrations/telegram-gateway
Environment=PYTHONPATH=%h/path/to/amux/integrations/telegram-gateway
ExecStart=%h/.local/share/amux-telegram-gateway/venv/bin/python -m telegram_gateway.cli
Restart=always
RestartSec=5
StandardOutput=append:%h/.amux/logs/telegram-gateway.log
StandardError=append:%h/.amux/logs/telegram-gateway.log

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now amux-telegram-gateway
systemctl --user status amux-telegram-gateway
tail -f ~/.amux/logs/telegram-gateway.log
```

## Usage

- `/start` or `/menu` — the four-button menu.
- **🚀 Start agent** — creates and starts a fleet lane named
  `tg-<MMDD-HHMM>-<hex>` in `AMUX_DEFAULT_WORKDIR` using
  `AMUX_DEFAULT_PROVIDER`. It waits for the spawn to settle and reports the real
  outcome, including a start error's detail.
- **👁️ Peek / status** — lists workers with state; tap one to see its last pane
  output.
- **✍️ Send prompt** — enters a prompt state; type the task (or send a voice
  note). `/cancel` leaves it.
- **⏹️ Stop** — pick a worker, confirm, and it is stopped. If the worker still
  owns a Todo/Doing board card, amux's bootstrap restarts it; the confirmation
  says so and names the cards before you commit to the stop, and the result
  reports when a stop was undone.
- **🧠 Manager settings** — choose which agent a new message starts (provider,
  then a model from `GET /api/models`, or the server default).
- **Plain message / voice note** — a new manager agent, answering in that
  message's thread. **Quote its answer** (or the "started" ack) to keep talking
  to the same manager; every other message starts another one.
- `/sessions` picks a session (for the menu actions); `/status` peeks it.
- `/sessions` picks a session; `/status` peeks the selected one.

Replies from the session are relayed automatically: the gateway finds the last
`[from Telegram @…]` marker in the pane and sends everything after it, stripping
ANSI and terminal chrome and chunking at Telegram's 4096-char limit.

## Security notes

- The bot refuses to start without `ALLOWED_TELEGRAM_USER_IDS`.
- Denials reply `⛔ Access denied` and log a `security_denied` warning with the
  user, chat and handler.
- Credentials live only in `~/.amux/server.env`; this README names variables,
  never values.
- The local amux server uses a self-signed certificate, so TLS verification is
  off by default for `https://localhost`. Set `AMUX_TLS_VERIFY=1` if your
  endpoint has a real certificate.

## Tests

```bash
cd integrations/telegram-gateway
pip install -r requirements-dev.txt
python3 -m pytest -q
```

The tests use no network: the amux client runs against an `httpx.MockTransport`,
and voice transcription is injected as a fake async callable.
