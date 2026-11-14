"""Photos ride the text path (owner incident 2026-11-14: a dropped image
answered nothing — silence is never a contract).

Contract covered here:
1. ``photo_prompt`` = caption + ``[imagem salva em <path>]`` (empty caption
   still yields a usable prompt — the path IS the content);
2. the file lands in ``<state_dir>/photos/<chat>_<msgid>.jpg`` and the prompt
   goes to the session the TOPIC binds (no OCR — the worker reads the image);
3. a failed save answers with a visible ❌, never silently;
4. the retention sweep removes stale media and keeps fresh files.
"""

import logging
import time
from pathlib import Path
from types import SimpleNamespace

from telegram_gateway import bot as botmod
from telegram_gateway.media import photo_prompt, save_photo, sweep
from telegram_gateway.state import GatewayState

from test_dedup import amux
from test_topics import CHAT, FakeBot, FakeMessage, fake_context, fake_update, make_cfg, run

PAYLOAD = b"JPEG-fake-photo-bytes"


class FakeFile:
    def __init__(self, payload):
        self.payload = payload

    async def download_to_drive(self, custom_path):
        Path(custom_path).write_bytes(self.payload)


class FakePhotoSize:
    def __init__(self, payload=PAYLOAD, file_size=None):
        self.payload = payload
        self.file_size = len(payload) if file_size is None else file_size

    async def get_file(self):
        return FakeFile(self.payload)


class BrokenPhoto:
    file_size = 1

    async def get_file(self):
        raise RuntimeError("telegram said no")


def test_photo_prompt_with_and_without_caption(tmp_path):
    path = tmp_path / "photos" / "-100_7.jpg"
    assert photo_prompt(path, "  gastei 9,99 hoje  ") == (
        f"gastei 9,99 hoje\n[imagem salva em {path}]"
    )
    assert photo_prompt(path) == f"[imagem salva em {path}]"


def test_sweep_removes_old_keeps_fresh(tmp_path):
    old = tmp_path / "old.jpg"
    new = tmp_path / "new.jpg"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    stale = time.time() - 40 * 86400
    import os

    os.utime(old, (stale, stale))
    removed = sweep(tmp_path)
    assert removed == 1
    assert not old.exists() and new.exists()


def test_save_photo_writes_the_bytes(tmp_path):
    path = run(
        save_photo(FakePhotoSize(), tmp_path / "photos", unique=f"{CHAT}_300")
    )
    assert path == tmp_path / "photos" / f"{CHAT}_300.jpg"
    assert path.read_bytes() == PAYLOAD


def test_on_photo_routes_into_the_bound_topic(tmp_path, caplog):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "gnucash-expense")
    sent, created = [], []
    client = amux(sent, created)
    created.append("gnucash-expense")  # the lane exists on the "server"
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    message = FakeMessage(
        message_id=300,
        thread=42,
        photo=[FakePhotoSize(file_size=10), FakePhotoSize()],  # largest wins
        caption="gastei 9,99 hoje",
    )
    update = fake_update(message, forum=True)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        run(botmod.on_photo(update, context))

    assert len(sent) == 1
    session, text = sent[0]
    assert session == "gnucash-expense"
    saved = tmp_path / "state" / "photos" / f"{CHAT}_300.jpg"
    assert saved.read_bytes() == PAYLOAD
    assert f"[imagem salva em {saved}]" in text
    assert "[from Telegram @owner]" in text
    assert "gastei 9,99 hoje" in text
    assert any("photo user=" in rec.message for rec in caplog.records)


def test_on_photo_save_failure_answers_visibly(tmp_path, caplog):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "gnucash-expense")
    sent, created = [], []
    client = amux(sent, created)
    context = fake_context(cfg, client, state, FakeBot())

    message = FakeMessage(message_id=301, thread=42, photo=[BrokenPhoto()])
    update = fake_update(message, forum=True)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        run(botmod.on_photo(update, context))

    assert sent == [], "a failed save must not deliver a half-truth"
    assert any("❌" in r["text"] for r in message.replies)
    assert any("photo_save_failed" in rec.message for rec in caplog.records)


def test_on_photo_in_general_starts_nothing_silently(tmp_path):
    """A photo with no thread, reply or forum goes through manager mode —
    here we only pin that it does not crash and routes into a delivery path."""
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = amux(sent, created)
    context = fake_context(cfg, client, state, FakeBot())
    message = FakeMessage(message_id=302, thread=None, photo=[FakePhotoSize()])
    update = fake_update(message, forum=False)
    run(botmod.on_photo(update, context))  # must not raise
    # private chat, no quote → manager mode creates a lane and delivers
    assert created or sent
