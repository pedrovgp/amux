"""Voice-note transcription via Groq (whisper-large-v3-turbo).

Telegram voice notes are OGG/OPUS, which Groq accepts directly — no ffmpeg
step.  The temporary file is deleted in ``finally`` so a failed transcription
cannot leave audio on disk.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

DEFAULT_MODEL = "whisper-large-v3-turbo"


async def transcribe_voice(voice, transcriber, tmp_dir: str | None = None) -> str:
    """Download a Telegram voice note, transcribe it, and always clean up.

    ``voice`` is a ``telegram.Voice``; ``transcriber`` is an async callable
    taking the local ``Path`` and returning text.  Kept injectable so tests
    never touch the network.
    """
    path: Path | None = None
    try:
        telegram_file = await voice.get_file()
        fd, name = tempfile.mkstemp(
            dir=tmp_dir, prefix=f"tg-voice-{uuid.uuid4().hex[:8]}-", suffix=".ogg"
        )
        path = Path(name)
        os.close(fd)
        await telegram_file.download_to_drive(custom_path=str(path))
        text = await transcriber(path)
        return (text or "").strip()
    finally:
        if path is not None and path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def groq_transcriber(api_key: str, model: str = DEFAULT_MODEL):
    """Build the real Groq transcriber (async, uploaded as OGG)."""

    async def _transcribe(path: Path) -> str:
        from groq import AsyncGroq

        client = AsyncGroq(api_key=api_key)
        data = path.read_bytes()
        response = await client.audio.transcriptions.create(
            file=(path.name, data),
            model=model,
        )
        return getattr(response, "text", "") or ""

    return _transcribe
