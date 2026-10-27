"""``--check`` must exercise the REAL AmuxClient surface.

Regression: `list_workers` was renamed to `list_sessions` (f37654bb7) and the
`_check` caller was never updated — `telegram-gateway --check` died with an
AttributeError (and an unhandled traceback, because only `AmuxError` is caught)
on every run. This test pins `_check`'s client calls so the next rename cannot
pass unnoticed.
"""
import asyncio

from telegram_gateway import cli
from telegram_gateway.config import Config


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.closed = False

    async def health(self):
        return {"status": "ok", "commit": "deadbeefcafe"}

    async def list_sessions(self):
        return [{"name": "lane-1"}, {"name": "lane-2"}]

    async def aclose(self):
        self.closed = True


def test_check_uses_the_live_client_surface(tmp_path, monkeypatch, capsys):
    created = {}

    def factory(*args, **kwargs):
        created["client"] = FakeClient(*args, **kwargs)
        return created["client"]

    monkeypatch.setattr(cli, "AmuxClient", factory)
    cfg = Config(
        bot_token="1:test-token",
        allowed_user_ids=frozenset({1}),
        default_workdir=str(tmp_path),
    )

    rc = asyncio.run(cli._check(cfg))
    out = capsys.readouterr().out

    assert rc == 0
    assert "✓ amux health" in out
    assert "✓ sessions visible  2" in out
    assert "OK" in out.splitlines()[-1]
    assert created["client"].closed  # the client is always closed
