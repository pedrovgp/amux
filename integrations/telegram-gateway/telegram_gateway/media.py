"""Incoming photos: saved for the worker to read, never OCR'd here.

The gateway's job is transport. It keeps the bytes on disk in the state dir
and forwards a ``caption`` + ``[imagem salva em <path>]`` prompt like any
other text, so the RECEIVING model reads the image itself — multimodal
readers only get better, an OCR in this process would be the ceiling
(CLAUDE.md ethos).

Files live in ``<state_dir>/photos`` until the retention sweep removes them;
they must outlive the worker reading them, hence ``RETENTION_DAYS`` (voice
temps die immediately because a transcript replaces them — a photo IS the
content). Filenames key on chat + message id, so a Telegram redelivery
overwrites the same evidence instead of duplicating it.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 30


def photo_prompt(path: Path | str, caption: str = "") -> str:
    """The prompt a saved photo becomes: caption (if any) + the file path."""
    body = f"[imagem salva em {path}]"
    clean = (caption or "").strip()
    return f"{clean}\n{body}" if clean else body


def sweep(media_dir: Path | str, *, now: float | None = None, max_age_days: int = RETENTION_DAYS) -> int:
    """Delete media older than ``max_age_days``; bounded, and logged when it fires."""
    media_dir = Path(media_dir)
    if not media_dir.exists():
        return 0
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    removed = 0
    for entry in sorted(media_dir.iterdir()):
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            try:
                entry.unlink()
                removed += 1
            except OSError as exc:  # keep the sweep bounded: one bad file ≠ crash
                log.warning("photo_sweep_unlink_failed path=%s error=%s", entry, exc)
    if removed:
        log.info("photo_sweep dir=%s removed=%d", media_dir, removed)
    return removed


async def save_photo(photo, media_dir: Path | str, *, unique: str) -> Path:
    """Download a PhotoSize/Document into ``media_dir`` and return its path."""
    media_dir = Path(media_dir).expanduser()
    media_dir.mkdir(parents=True, exist_ok=True)
    sweep(media_dir)
    path = media_dir / f"{unique}.jpg"
    telegram_file = await photo.get_file()
    await telegram_file.download_to_drive(custom_path=str(path))
    return path
