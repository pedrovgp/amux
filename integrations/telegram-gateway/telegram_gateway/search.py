"""Finding sessions: fuzzy by name, filtered by a created/updated range.

The owner's ask (2026-11-02): *"search sessions either globally or within
projects with a fuzzy search by name … filter by created date or last
updated date within a timestamp range … final timestamp by default being
now or a little bit in the future to get everything and the initial
timestamp a few days before, like five days before."*

Everything here is PURE — no Telegram, no network, no state — so the rules
are unit-testable and the bot layer only renders:

- :func:`fuzzy_score` — substring beats subsequence beats ``difflib``
  ratio; matched against the title AND the ``ses_…`` id (a pasted id finds
  its own row), best score wins;
- :func:`default_range` — the owner's defaults: now−5d → now+1d (the
  future edge "gets everything" the spec asked for);
- :func:`parse_range` — the typed range: two endpoints separated by comma,
  ``to`` or whitespace; each ``YYYY-MM-DD[ HH:MM]`` or ``now``; a bare
  date spans that whole day; ``None`` is an honest failure (the caller
  shows an example, never guesses);
- :func:`search_sessions` — scope (directory) + date field + range +
  fuzzy term, sorted best-first (or newest-first when no term).
"""
from __future__ import annotations

import difflib
import re
import time
from datetime import datetime

DEFAULT_WINDOW_DAYS = 5  # the owner's "a few days before, like five days"
DEFAULT_FUTURE_DAYS = 1  # "now or a little bit in the future"
MIN_SCORE = 0.5  # below this a fuzzy match is noise, not a hint
FIELD_CREATED = "created"
FIELD_UPDATED = "updated"
FIELDS = (FIELD_CREATED, FIELD_UPDATED)

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}:\d{2}))?$")


def default_range(now_ms: float | None = None) -> tuple[float, float]:
    """``(from, to)`` in ms epoch: the last 5 days, ending tomorrow."""
    now = float(now_ms) if now_ms is not None else time.time() * 1000.0
    day = 86400_000.0
    return (now - DEFAULT_WINDOW_DAYS * day, now + DEFAULT_FUTURE_DAYS * day)


def _subsequence(needle: str, haystack: str) -> bool:
    """Every char of ``needle`` in order inside ``haystack`` (fzf-lite)."""
    pos = 0
    for char in needle:
        pos = haystack.find(char, pos)
        if pos < 0:
            return False
        pos += 1
    return True


def fuzzy_score(term: str, text: str) -> float:
    """How well ``term`` matches ``text``: 1.0 substring, 0.8 subsequence,
    else the ``difflib`` ratio; ``0.0`` when either side is empty."""
    needle = (term or "").lower().strip()
    haystack = (text or "").lower()
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if _subsequence(needle, haystack):
        return 0.8
    return difflib.SequenceMatcher(None, needle, haystack).ratio()


def row_score(term: str, row: dict) -> float:
    """The best score against the row's title or its ``ses_…`` id — an id
    pasted by the owner must find its own conversation."""
    if not term:
        return 1.0  # no term = browse, not filter: every row qualifies
    return max(
        fuzzy_score(term, str(row.get("title") or "")),
        fuzzy_score(term, str(row.get("id") or "")),
    )


def _endpoint(token: str, now_ms: float, edge: str) -> float | None:
    """One range endpoint in ms epoch, or ``None`` (unparseable).

    ``""`` → the default for that edge (5 days ago / tomorrow), ``now`` →
    now, ``YYYY-MM-DD`` → the start (or the END, 23:59:59.999) of that
    local day, ``YYYY-MM-DD HH:MM`` → that exact local minute.
    """
    text = (token or "").strip()
    default_from, _ = default_range(now_ms)
    _, default_to = default_range(now_ms)
    if not text:
        return default_from if edge == "from" else default_to
    if text.lower() == "now":
        return now_ms
    match = _DATE_RE.match(text)
    if not match:
        return None
    try:
        day = datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    if match.group(2):
        try:
            clock = datetime.strptime(match.group(2), "%H:%M")
        except ValueError:
            return None
        moment = day.replace(hour=clock.hour, minute=clock.minute)
        return moment.timestamp() * 1000.0
    if edge == "to":
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.timestamp() * 1000.0


def parse_range(text: str, now_ms: float | None = None) -> tuple[float, float] | None:
    """``(from, to)`` ms epoch from the owner's typed range, else ``None``.

    Two endpoints separated by a comma, the word ``to``, or plain
    whitespace; each endpoint is ``YYYY-MM-DD[ HH:MM]``, ``now`` or empty
    (the default for that edge). ``None`` means "I could not read it" —
    never a guessed range.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if "," in raw:
        parts = [part.strip() for part in raw.split(",")]
    elif re.search(r"\s+to\s+", raw, flags=re.IGNORECASE):
        parts = re.split(r"\s+to\s+", raw, flags=re.IGNORECASE)
    else:
        parts = raw.split()
    if len(parts) != 2:
        return None
    now = float(now_ms) if now_ms is not None else time.time() * 1000.0
    from_ms = _endpoint(parts[0], now, "from")
    to_ms = _endpoint(parts[1], now, "to")
    if from_ms is None or to_ms is None:
        return None
    if from_ms > to_ms:
        return None  # the owner's initial must precede their final
    return (from_ms, to_ms)


def format_date(ms) -> str:
    """A date in the range label: ``2026-11-02`` (or ``2026-11-02 14:30``
    when the value carries a time); ``""`` for a missing value."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    moment = datetime.fromtimestamp(float(ms) / 1000.0)
    if moment.hour or moment.minute:
        return moment.strftime("%Y-%m-%d %H:%M")
    return moment.strftime("%Y-%m-%d")


def search_sessions(
    rows: list[dict],
    *,
    term: str = "",
    field: str = FIELD_UPDATED,
    t_from: float | None = None,
    t_to: float | None = None,
    directory: str | None = None,
) -> list[dict]:
    """The rows matching scope + date field + range + fuzzy term.

    - ``directory`` set → only that project (the "within projects" scope);
    - ``field`` picks WHICH date the range filters (``created``/``updated``);
      a row without that date cannot prove it is in range, so it is
      EXCLUDED rather than guessed in or out;
    - no term → every in-range row (browse by date), newest first; with a
      term → best score first, then newest.
    """
    field = field if field in FIELDS else FIELD_UPDATED
    if t_from is None or t_to is None:
        t_from, t_to = default_range()
    scored: list[tuple[float, float, dict]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if directory is not None and str(row.get("directory") or "") != directory:
            continue
        value = row.get(field)
        if not isinstance(value, (int, float)):
            continue  # no date for the chosen field: cannot place it in range
        if not (float(t_from) <= float(value) <= float(t_to)):
            continue
        score = row_score(term, row)
        if term and score < MIN_SCORE:
            continue
        scored.append((score, float(value), row))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [row for _, _, row in scored]
