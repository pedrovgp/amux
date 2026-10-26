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
| `/start`, `/menu` | Inline keyboard: 🚀 new session · 👁️ peek/status · ✍️ send prompt · ⏹️ stop |
| Text message | Sent to the selected session as `[from Telegram @you] …` |
| Voice note | Downloaded, transcribed by Groq, echoed as `🗣️ Transcribed: …`, then sent as a prompt; the temp `.ogg` is deleted immediately |
| Replies | A relay peeks the selected session every ~20s and pushes new output back to the chat |
| Access | Every message, button and voice note must come from `ALLOWED_TELEGRAM_USER_IDS`; denials are explicit and logged as `security_denied` |

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

### 7. Keep it running (optional, systemd user unit)

```ini
# ~/.config/systemd/user/amux-telegram-gateway.service
[Unit]
Description=amux Telegram gateway
After=network-online.target

[Service]
WorkingDirectory=%h/path/to/amux/integrations/telegram-gateway
ExecStart=%h/path/to/amux/integrations/telegram-gateway/bin/telegram-gateway
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now amux-telegram-gateway
journalctl --user -u amux-telegram-gateway -f
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
- Plain text (outside the prompt state) goes to the selected session.
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
