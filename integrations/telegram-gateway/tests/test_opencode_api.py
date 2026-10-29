"""OpenCode serve API controls (Milestone 9, owner ask 2026-10-28).

The contract under test:

1. **discovery** — `ss -tlnp` + `/proc` find the lane's `opencode serve`
   (port, `OPENCODE_PASSWORD`, `AMUX_SESSION`, and the pin on the TUI's
   `--session`), a headless serve without a password is skipped, and a miss
   raises `OcError` with ONE `oc_api_discovery_failed` WARN per stretch;
2. **client** — Basic `opencode:<password>`, `GET /api/model` filtered to the
   provider family and normalized to `{id, providerID, variants}`,
   `GET /api/session/<id>` unwrapped, `POST …/model` sending exactly
   `{"model": {providerID, id, variant?}}` (variant omitted for a default),
   `POST …/interrupt` returning `{"interrupted": …}`;
3. **panel** — a pinned opencode lane's 🤖 model page comes from its own
   server (indexes rendered from `ctlo` rows), 🧠 effort lists THAT model's
   variants and applies one POST, ⚡ interrupt prefers `/interrupt` and falls
   back to Escape, and every API failure falls back to Milestone 8's behavior
   with a single `oc_api_fallback` WARN — no dead buttons, no wrong-list
   index ever resolved, and NOTHING that touches the network: discovery and
   transport are injected (`FakeOc`, `NoOc`, `MockTransport`, a fixture
   `/proc` tree).
"""
import base64
import json
import logging
from pathlib import Path

import httpx
import pytest

from telegram_gateway import bot as botmod, opencode_api
from telegram_gateway.opencode_api import (
    OcError,
    OcServer,
    OpencodeClient,
    client_for,
    client_for_service,
    discover_servers,
    find_server,
    service_endpoint,
    service_password,
)

from test_session_control import (
    PIN,
    FakeAmux,
    buttons,
    context_for,
    fake_callback,
    fake_command,
    lane,
)
from test_topics import CHAT, make_cfg, run
from telegram_gateway.state import GatewayState

# ---- fixtures: catalog + session, exactly as the serve API normalizes them --

CATALOG = [
    {"id": "kimi-k3.1-pro", "providerID": "opencode-go",
     "variants": ["low", "high", "max"]},
    {"id": "minimax-m3.1-flash", "providerID": "opencode-go",
     "variants": ["low", "medium", "high", "xhigh", "max"]},
    {"id": "mimo-v2.6-flash", "providerID": "opencode-go", "variants": []},
    {"id": "flint-40b-free", "providerID": "opencode",
     "variants": ["low", "medium", "high"]},
    # the same id under BOTH providers: only a (providerID, id) index can
    # tell them apart, and only the session's own provider is its model.
    {"id": "gemini-4-argon", "providerID": "opencode",
     "variants": ["low", "medium", "high"]},
    {"id": "gemini-4-argon", "providerID": "opencode-go",
     "variants": ["low", "medium", "high"]},
    # a provider the lane's family does not own: never offered.
    {"id": "gemini-3.8-flash", "providerID": "google",
     "variants": ["low", "medium", "high"]},
]

SESSION = {
    "model": {"providerID": "opencode-go", "id": "kimi-k3.1-pro",
              "variant": "high"}
}


class FakeOcClient:
    def __init__(self, owner):
        self.owner = owner

    def _maybe_fail(self, method: str) -> None:
        exc = self.owner.fail_call.get(method)
        if exc is not None:
            raise exc

    async def list_models(self, provider=None):
        self.owner.calls.append(("list_models", provider))
        self._maybe_fail("list_models")
        out = []
        for row in self.owner.models:
            provider_id = str(row.get("providerID") or "")
            if provider and not (
                provider_id == provider or provider_id.startswith(provider + "-")
            ):
                continue
            out.append(dict(row))
        return out

    async def get_session(self, ses_id):
        self.owner.calls.append(("get_session", ses_id))
        self._maybe_fail("get_session")
        return json.loads(json.dumps(self.owner.session))

    async def set_model(self, ses_id, provider_id, model_id, variant=None):
        self.owner.calls.append(
            ("set_model", ses_id, provider_id, model_id, variant)
        )
        self._maybe_fail("set_model")
        # mirror the server: the session really changes, so the panel that
        # re-reads it after the POST shows what was just applied.
        self.owner.session["model"] = {
            "providerID": provider_id,
            "id": model_id,
            "variant": variant or "default",
        }

    async def interrupt(self, ses_id):
        self.owner.calls.append(("interrupt", ses_id))
        self._maybe_fail("interrupt")
        return {"interrupted": True}


class FakeOc:
    """Offline stand-in for `telegram_gateway.opencode_api` in bot_data."""

    def __init__(self, *, models=(), session=None, fail=None, fail_call=None):
        self.models = [dict(m) for m in models]
        self.session = json.loads(json.dumps(session or SESSION))
        self.fail = fail  # raised by client_for: discovery/auth is broken
        self.fail_call = dict(fail_call or {})  # method -> exception
        self.calls = []
        self.client = FakeOcClient(self)

    async def client_for(self, lane=None, pin=None):
        self.calls.append(("client_for", lane, pin))
        if self.fail is not None:
            raise self.fail
        return self.client


def oc_context(cfg, state, amux, oc, bot=None, args=()):
    return context_for(cfg, state, amux.client, bot, args=args, oc=oc)


def texts(update):
    return [e["text"] for e in update.callback_query.edits]


@pytest.fixture(autouse=True)
def _clean_fallback_dedupe():
    botmod._oc_fallback_warned.clear()
    opencode_api.reset_warn_dedupe()
    yield
    botmod._oc_fallback_warned.clear()
    opencode_api.reset_warn_dedupe()


# ---- discovery: fixture /proc tree + fixture `ss` output -------------------

SS = "\n".join(
    [
        'LISTEN 0      512        127.0.0.1:46373      0.0.0.0:*    '
        'users:(("opencode",pid=100,fd=8))',
        'LISTEN 0      512        127.0.0.1:33469      0.0.0.0:*    '
        'users:(("opencode",pid=110,fd=8))',
        'LISTEN 0      512        127.0.0.1:49374      0.0.0.0:*    '
        'users:(("opencode",pid=140,fd=10))',  # the TUI, not a serve
        'LISTEN 0      512        127.0.0.1:41571      0.0.0.0:*    '
        'users:(("opencode",pid=150,fd=8))',  # headless: no password
        'LISTEN 0      128        0.0.0.0:22           0.0.0.0:*    '
        'users:(("sshd",pid=120,fd=3))',
        'LISTEN 0      512        [::1]:8080           [::]:*       '
        'users:(("node",pid=130,fd=9))',
    ]
)


def write_proc(root, pid, *, cmdline=b"", environ=b"", ppid=1):
    directory = Path(root) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_bytes(cmdline)
    (directory / "environ").write_bytes(environ)
    (directory / "status").write_bytes(
        f"Name:\topencode\nPPid:\t{ppid}\n".encode()
    )


def build_proc_tree(tmp_path) -> str:
    root = tmp_path / "proc"
    # lane mgr-a: serve child -> TUI carrying the pin
    write_proc(
        root,
        100,
        cmdline=b"/usr/bin/opencode\x00serve\x00--stdio\x00--port\x000",
        environ=b"OPENCODE_PASSWORD=s3cret-a\x00AMUX_SESSION=mgr-a\x00",
        ppid=200,
    )
    write_proc(
        root,
        200,
        cmdline=b"/usr/bin/opencode\x00--session\x00ses_pin_a\x00--standalone",
        ppid=1,
    )
    # lane mgr-b: serve with a lane name but no pin anywhere
    write_proc(
        root,
        110,
        cmdline=b"/usr/bin/opencode\x00serve\x00--stdio\x00--port\x000",
        environ=b"OPENCODE_PASSWORD=s3cret-b\x00AMUX_SESSION=mgr-b\x00",
        ppid=1,
    )
    # the TUI itself listening (not a serve) and a headless serve with no
    # auth material: both must be skipped
    write_proc(
        root,
        140,
        cmdline=b"/usr/bin/opencode\x00--standalone",
        environ=b"OPENCODE_PASSWORD=unused\x00AMUX_SESSION=mgr-tui\x00",
        ppid=1,
    )
    write_proc(
        root,
        150,
        cmdline=b"/usr/bin/opencode\x00serve\x00--service",
        environ=b"AMUX_SESSION=mgr-headless\x00",
        ppid=1,
    )
    return str(root)


def fixture_discover(tmp_path):
    proc_root = build_proc_tree(tmp_path)
    return lambda: discover_servers(proc_root=proc_root, ss_output=SS)


def test_discovery_finds_the_lane_its_pin_and_its_password(tmp_path):
    servers = fixture_discover(tmp_path)()
    assert [(s.pid, s.port) for s in servers] == [(100, 46373), (110, 33469)]
    first = servers[0]
    assert first.password == "s3cret-a"
    assert first.lane == "mgr-a"
    assert first.pin == "ses_pin_a"  # read off the TUI, one level up
    assert first.base_url == "http://127.0.0.1:46373"
    assert servers[1].pin is None  # no --session anywhere up the tree


def test_discovery_skips_non_serves_and_passwordless_serves(tmp_path):
    servers = fixture_discover(tmp_path)()
    pids = {s.pid for s in servers}
    assert 140 not in pids  # the TUI: not a `serve`
    assert 150 not in pids  # headless `serve --service`: no OPENCODE_PASSWORD
    assert 120 not in pids and 130 not in pids  # not opencode, not loopback


def test_find_server_prefers_the_pin_over_a_stale_lane_name(tmp_path):
    """A rename rewrites the lane name but not `--session`: the pin is the
    identity that still resolves after one."""
    root = build_proc_tree(tmp_path)
    # mgr-a was renamed: a NEW serve env says mgr-renamed, the pin still wins
    write_proc(
        root,
        100,
        cmdline=b"/usr/bin/opencode\x00serve\x00--stdio\x00--port\x000",
        environ=b"OPENCODE_PASSWORD=s3cret-a\x00AMUX_SESSION=mgr-renamed\x00",
        ppid=200,
    )
    server = find_server(
        lane="mgr-b", pin="ses_pin_a",
        discover=lambda: discover_servers(proc_root=str(root), ss_output=SS),
    )
    assert server.pin == "ses_pin_a" and server.lane == "mgr-renamed"


def test_find_server_falls_back_to_the_lane_name(tmp_path):
    server = find_server(lane="mgr-b", discover=fixture_discover(tmp_path))
    assert server.port == 33469 and server.pin is None


def test_find_server_without_a_match_raises_and_warns_once(tmp_path, caplog):
    discover = fixture_discover(tmp_path)
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        for _ in range(2):
            with pytest.raises(OcError) as excinfo:
                find_server(lane="nobody", pin="ses_nope", discover=discover)
    assert "no opencode serve" in excinfo.value.detail
    assert "mgr-a" in excinfo.value.detail  # names what WAS listening
    warns = [m for m in caplog.messages if "oc_api_discovery_failed" in m]
    assert len(warns) == 1  # one WARN per stretch, not one per tap


def test_find_server_needs_a_lane_or_a_pin():
    with pytest.raises(OcError):
        find_server(discover=lambda: [])


def test_discovery_survives_an_unreadable_process(tmp_path):
    """A pid that vanishes mid-scan is skipped, not raised."""
    root = build_proc_tree(tmp_path)
    servers = discover_servers(
        proc_root=str(root),
        ss_output=SS + "\nLISTEN 0 512 127.0.0.1:5555 0.0.0.0:* "
        'users:(("opencode",pid=999,fd=8))',  # no /proc/999 at all
    )
    assert {s.pid for s in servers} == {100, 110}


# ---- the client: MockTransport, no sockets ---------------------------------


def make_client(handler, password="s3cret"):
    return OpencodeClient(
        "http://127.0.0.1:46373", password, transport=httpx.MockTransport(handler)
    )


def test_client_for_builds_the_client_from_discovery():
    seen = []

    def discover():
        seen.append(True)
        return [OcServer(pid=1, port=46373, password="pw", lane="mgr-x",
                         pin=PIN)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.port == 46373
        return httpx.Response(200, json={"interrupted": False})

    client = run(client_for(lane="mgr-x", pin=PIN, discover=discover,
                            transport=httpx.MockTransport(handler)))
    try:
        assert run(client.interrupt(PIN)) == {"interrupted": False}
    finally:
        run(client.aclose())
    assert seen == [True]


def test_basic_auth_is_opencode_with_the_env_password():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(204)

    client = make_client(handler, password="s3cret-a")
    try:
        run(client.set_model(PIN, "opencode-go", "kimi-k3.1-pro", "high"))
    finally:
        run(client.aclose())
    scheme, _, token = captured["auth"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "opencode:s3cret-a"


def test_list_models_filters_the_provider_family_and_normalizes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/model"
        return httpx.Response(200, json={"data": [
            {"id": "kimi-k3.1-pro", "providerID": "opencode-go",
             "variants": [{"id": "low"}, {"id": "high"}]},
            {"id": "flint-40b-free", "providerID": "opencode",
             "variants": []},
            {"id": "gemini-3.8-flash", "providerID": "google",
             "variants": [{"id": "low"}]},
            "not a row",
        ]})

    client = make_client(handler)
    try:
        rows = run(client.list_models("opencode"))
    finally:
        run(client.aclose())
    assert rows == [
        {"id": "kimi-k3.1-pro", "providerID": "opencode-go",
         "variants": ["low", "high"]},
        {"id": "flint-40b-free", "providerID": "opencode", "variants": []},
    ]  # `google` filtered out, variants reduced to their ids


def test_get_session_unwraps_the_data_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/session/{PIN}"
        return httpx.Response(200, json={"data": {"model": {
            "providerID": "opencode-go", "id": "kimi-k3.1-pro",
            "variant": "high"}}})

    client = make_client(handler)
    try:
        sess = run(client.get_session(PIN))
    finally:
        run(client.aclose())
    assert sess["model"]["variant"] == "high"


def test_set_model_posts_exactly_one_body_and_omits_a_default_variant():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/session/{PIN}/model"
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(204)

    client = make_client(handler)
    try:
        run(client.set_model(PIN, "opencode-go", "kimi-k3.1-pro", "max"))
        run(client.set_model(PIN, "opencode-go", "kimi-k3.1-pro", None))
    finally:
        run(client.aclose())
    assert bodies == [
        {"model": {"providerID": "opencode-go", "id": "kimi-k3.1-pro",
                   "variant": "max"}},
        {"model": {"providerID": "opencode-go", "id": "kimi-k3.1-pro"}},
    ]


def test_interrupt_returns_the_interrupted_flag():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/api/session/{PIN}/interrupt"
        return httpx.Response(200, json={"interrupted": False})

    client = make_client(handler)
    try:
        assert run(client.interrupt(PIN)) == {"interrupted": False}
    finally:
        run(client.aclose())


def test_a_401_is_an_oc_error_with_the_status(tmp_path, caplog):
    client = make_client(
        lambda request: httpx.Response(401, text="UnauthorizedError")
    )
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        with pytest.raises(OcError) as excinfo:
            run(client.list_models("opencode"))
        run(client.aclose())
    assert excinfo.value.status == 401
    # the model-list failure is logged, once per stretch
    assert any("oc_api_model_list_failed" in m for m in caplog.messages)


def test_an_unreachable_server_is_an_oc_error_not_a_crash():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(refuse)
    try:
        with pytest.raises(OcError) as excinfo:
            run(client.get_session(PIN))
    finally:
        run(client.aclose())
    assert excinfo.value.status == 0
    assert "unreachable" in excinfo.value.detail


# ---- the panel: FakeOc in bot_data -----------------------------------------


def panel_row(**kw):
    return lane("mgr-x", flags=f"--session {PIN}", **kw)


def test_panel_reads_model_and_effort_from_the_opencode_server(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc()
    update = fake_callback("gw:ctl:mgr-x")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    text = texts(update)[0]
    assert "kimi-k3.1-pro" in text
    assert "effort: `high`" in text  # the VARIANT the row can never know
    assert oc.calls[0] == ("client_for", "mgr-x", PIN)


def test_the_model_page_lists_the_servers_catalog_with_ctlo_rows(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()], models=[])  # amux's list is EMPTY
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlm:mgr-x")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    markup = update.callback_query.edits[0]["markup"]
    data = buttons(markup)
    model_rows = [d for d in data if d.startswith("gw:ctlo:")]
    assert model_rows == [f"gw:ctlo:mgr-x:{i}" for i in range(6)]
    assert all(len(d.encode()) <= 64 for d in data), data
    labels = [b.text for row in markup.inline_keyboard for b in row]
    # the session's own model is marked, and google's row never appears
    assert any(l.startswith("✅ kimi-k3.1-pro") for l in labels)
    assert not any("gemini-3.8-flash" in l for l in labels)
    assert any("Custom model id" in l for l in labels)
    # amux's own catalog was never consulted (only `find`, to read the row)
    assert not any(c[1] == "/api/models" for c in amux.calls)


def test_a_model_id_under_two_providers_marks_only_the_sessions_own(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(
        models=CATALOG,
        session={"model": {"providerID": "opencode-go", "id": "gemini-4-argon",
                           "variant": "high"}},
    )
    update = fake_callback("gw:ctlm:mgr-x")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    labels = [b.text for row in update.callback_query.edits[0]["markup"].inline_keyboard
              for b in row]
    marks = [l for l in labels if "gemini-4-argon" in l]
    assert [l.startswith("✅") for l in marks] == [False, True]


def test_a_long_catalog_pages_and_every_callback_fits_64_bytes(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    name = "mgr-" + "x" * 36  # the panel's own 40-byte cap
    amux = FakeAmux(rows=[lane(name, flags=f"--session {PIN}")])
    oc = FakeOc(
        models=[{"id": f"model-{i:02d}-" + "y" * 30, "providerID": "opencode-go",
                 "variants": ["high"]} for i in range(12)]
    )
    first = fake_callback(f"gw:ctlm:{name}")
    try:
        run(botmod.on_callback(first, oc_context(cfg, state, amux, oc)))
        page = fake_callback(f"gw:ctlm:{name}:1")
        run(botmod.on_callback(page, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    data = buttons(first.callback_query.edits[0]["markup"])
    assert len([d for d in data if d.startswith("gw:ctlo:")]) == 10
    assert any(d.startswith("gw:ctlm:") and d.endswith(":1") for d in data)
    data2 = buttons(page.callback_query.edits[0]["markup"])
    labels = [b.text for row in page.callback_query.edits[0]["markup"].inline_keyboard
              for b in row]
    assert any("model-11" in l for l in labels)  # the second page's rows
    assert any(d.endswith(":0") and d.startswith("gw:ctlm:") for d in data2)
    assert all(len(d.encode()) <= 64 for d in data + data2), data + data2


def test_picking_a_model_posts_model_and_variant_in_one_call(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlo:mgr-x:0")
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("set_model", PIN, "opencode-go", "kimi-k3.1-pro", "high") in oc.calls
    assert amux.patches() == []  # no restart, no config PATCH
    assert any("session_model_set" in m and "path=oc_api" in m
               for m in caplog.messages)
    assert "✅" in texts(update)[-1]
    # and the panel re-read the server: the applied model is what it shows
    assert "kimi-k3.1-pro" in texts(update)[-1]


def test_a_variant_the_new_model_lacks_is_dropped_not_invented(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)  # current variant is `high`
    update = fake_callback("gw:ctlo:mgr-x:2")  # mimo-v2.6-flash: no variants
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("set_model", PIN, "opencode-go", "mimo-v2.6-flash", None) in oc.calls
    assert amux.patches() == []


def test_the_effort_page_lists_that_models_own_variants(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctle:mgr-x")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    markup = update.callback_query.edits[0]["markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    levels = [l.split()[-1] for l in labels if l[:1] in "○✅" and l.split()[-1] != "default"]
    assert levels == ["low", "high", "max"]  # kimi's variants, not amux's five
    assert any(l.startswith("✅ high") for l in labels)
    assert any(l.startswith("○ max") for l in labels)
    assert all(len(d.encode()) <= 64 for d in buttons(markup))


def test_tapping_an_effort_level_posts_it_as_the_models_variant(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlf:mgr-x:max")
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("set_model", PIN, "opencode-go", "kimi-k3.1-pro", "max") in oc.calls
    assert amux.patches() == []  # an effort PATCH here would kill the lane
    assert any("session_effort_set" in m and "path=oc_api" in m
               for m in caplog.messages)


def test_provider_default_omits_the_variant_entirely(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlf:mgr-x:")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("set_model", PIN, "opencode-go", "kimi-k3.1-pro", None) in oc.calls


def test_an_unknown_effort_is_refused_before_any_post(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlf:mgr-x:ludicrous")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert not any(c[0] == "set_model" for c in oc.calls)
    assert amux.patches() == []
    assert "unknown effort" in texts(update)[-1]


def test_a_model_with_no_variants_says_so_instead_of_a_dead_dial(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(
        models=CATALOG,
        session={"model": {"providerID": "opencode-go", "id": "mimo-v2.6-flash",
                           "variant": "default"}},
    )
    update = fake_callback("gw:ctle:mgr-x")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert "no effort levels" in texts(update)[-1]
    assert buttons(update.callback_query.edits[0]["markup"]) == [] or not any(
        d.startswith("gw:ctlf:") for d in buttons(update.callback_query.edits[0]["markup"])
    )


def test_interrupt_prefers_the_opencode_endpoint_and_logs_the_path(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc()
    update = fake_callback("gw:ctli:mgr-x")
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("interrupt", PIN) in oc.calls
    assert not any(c[1].endswith("/keys") for c in amux.posts())
    assert any("session_interrupted" in m and "path=oc_api" in m
               for m in caplog.messages)
    assert "OpenCode API" in texts(update)[-1]


def test_interrupt_falls_back_to_escape_with_one_warn_per_stretch(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(fail=OcError(0, "serve gone"))
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(fake_callback("gw:ctli:mgr-x"),
                                   oc_context(cfg, state, amux, oc)))
            run(botmod.on_callback(fake_callback("gw:ctli:mgr-x"),
                                   oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert ("POST", "/api/workers/mgr-x/keys", {"keys": "Escape"}) in amux.posts()
    warns = [m for m in caplog.messages if "oc_api_fallback" in m]
    assert len(warns) == 1  # one WARN per stretch, not one per tap
    assert any("session_interrupted" in m and "path=keys" in m
               for m in caplog.messages)


def test_a_failed_switch_falls_back_to_the_amux_patch(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG, fail_call={"set_model": OcError(500, "boom")})
    update = fake_callback("gw:ctlo:mgr-x:0")
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    # today's behavior: amux PATCHes the config (a restart), with the id the
    # tap DID name — never an index against another catalog
    assert ("PATCH", "/api/sessions/mgr-x/config",
            {"model": "opencode-go/kimi-k3.1-pro"}) in amux.patches()
    assert any("oc_api_fallback" in m for m in caplog.messages)


def test_a_dead_server_on_the_model_page_falls_back_to_amuxs_catalog(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()], models=[])
    oc = FakeOc(fail=OcError(0, "serve gone"))
    update = fake_callback("gw:ctlm:mgr-x")
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    text = texts(update)[-1]
    assert "unreachable right now" in text  # the reason, not a silent empty list
    assert any(d.startswith("gw:ctlc:") for d in
               buttons(update.callback_query.edits[0]["markup"]))
    assert any("oc_api_fallback" in m for m in caplog.messages)


def test_a_ctlo_tap_with_a_dead_server_never_resolves_against_amux(tmp_path):
    """The list on screen was OpenCode's: an index into it must not be read
    against amux's catalog (which has different rows)."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[panel_row()], models=[
        {"provider": "opencode", "id": "some-amux-row"},
    ])
    oc = FakeOc(fail=OcError(0, "serve gone"))
    update = fake_callback("gw:ctlo:mgr-x:0")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert amux.patches() == []
    assert not any(c[0] == "set_model" for c in oc.calls)
    assert "unreachable" in texts(update)[-1]


def test_a_claude_lane_never_touches_the_opencode_api(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(
        rows=[lane("claude-lane", provider="claude", flags="")],
        models=[{"provider": "claude", "id": "claude-opus-5"}],
    )
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctlp:claude-lane:0")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert oc.calls == []  # client_for never even asked
    assert ("PATCH", "/api/sessions/claude-lane/config",
            {"model": "claude-opus-5"}) in amux.patches()


def test_the_model_command_applies_a_bare_id_through_the_same_api(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "mgr-x")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_command("/model kimi-k3.1-pro", forum=False)
    context = oc_context(cfg, state, amux, oc, args=("kimi-k3.1-pro",))
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.model_command(update, context))
    finally:
        run(amux.aclose())
    assert ("set_model", PIN, "opencode-go", "kimi-k3.1-pro", "high") in oc.calls
    assert amux.patches() == []
    assert any("session_model_set" in m and "path=oc_api" in m
               for m in caplog.messages)


def test_the_effort_command_lists_the_models_own_levels(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "mgr-x")
    amux = FakeAmux(rows=[panel_row()])
    oc = FakeOc(models=CATALOG)
    update = fake_command("/effort", forum=False)
    try:
        run(botmod.effort_command(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    reply = update.message.replies[-1]["text"]
    assert "kimi-k3.1-pro" in reply
    markup = update.message.replies[-1]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("max" in l for l in labels)


def test_an_unpinned_opencode_lane_keeps_todays_refusal(tmp_path):
    """No pin = no conversation id to address: the panel says the truth it
    knows (Milestone 8's message) instead of pretending a dial exists."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = FakeAmux(rows=[lane("mgr-loose", provider="opencode", flags="")])
    oc = FakeOc(models=CATALOG)
    update = fake_callback("gw:ctle:mgr-loose")
    try:
        run(botmod.on_callback(update, oc_context(cfg, state, amux, oc)))
    finally:
        run(amux.aclose())
    assert oc.calls == []
    assert "no effort switch" in texts(update)[-1]


# ---- Task 1 (owner, 2026-10-28): the SHARED service the relay reads from ---


def write_service_json(tmp_path, text, name="service.json") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_service_password_prefers_the_explicit_value_and_reads_the_file(tmp_path):
    file_pw = write_service_json(tmp_path, '{"password": "  file-pw  "}')
    assert service_password(path=file_pw) == "file-pw"
    assert service_password(password=" env-pw ", path=file_pw) == "env-pw"  # env wins
    assert service_password(password="", path=file_pw) == "file-pw"  # empty falls through
    assert service_password(path=tmp_path / "absent.json") is None
    assert service_password(path=write_service_json(tmp_path, "{ not json", "bad.json")) is None
    assert service_password(path=write_service_json(tmp_path, "{}", "empty.json")) is None
    assert service_password(
        path=write_service_json(tmp_path, '{"password": ""}', "blank.json")
    ) is None


def test_service_endpoint_prefers_the_env_url_and_never_scans(tmp_path):
    """`AMUX_OPENCODE_SERVICE_URL` set → that URL, no `ss`, no /proc."""
    file_pw = write_service_json(tmp_path, '{"password": "file-pw"}')
    base, pw = service_endpoint(
        url="http://127.0.0.1:49374/",
        service_json=file_pw,
        proc_root=str(tmp_path / "no-such-proc"),
        ss_output="",  # would be parsed only if the URL branch did not win
    )
    assert base == "http://127.0.0.1:49374"
    assert pw == "file-pw"


def test_service_endpoint_finds_the_shared_service_and_skips_lane_children(tmp_path):
    """Discovery picks `opencode serve --service`, never a lane's own child.

    Measured shape (2026-10-28): pid 2605614 `opencode serve --service` on
    127.0.0.1:49374, with five `serve --stdio --port 0` children listening
    beside it — those belong to individual lanes and are NOT the relay's
    source, so the `--service` argv is the discriminator.
    """
    root = tmp_path / "proc"
    write_proc(
        root, 100,
        cmdline=b"/usr/bin/opencode\x00serve\x00--stdio\x00--port\x000",
        environ=b"OPENCODE_PASSWORD=lane-pw\x00AMUX_SESSION=mgr-a\x00",
    )
    write_proc(root, 260, cmdline=b"/usr/bin/opencode\x00serve\x00--service")
    ss = "\n".join(
        [
            'LISTEN 0 512 127.0.0.1:46373 0.0.0.0:* users:(("opencode",pid=100,fd=8))',
            'LISTEN 0 512 127.0.0.1:49374 0.0.0.0:* users:(("opencode",pid=260,fd=10))',
        ]
    )
    base, pw = service_endpoint(
        service_json=write_service_json(tmp_path, '{"password": "shared-pw"}'),
        proc_root=str(root),
        ss_output=ss,
    )
    assert base == "http://127.0.0.1:49374"  # the --service, not the lane child
    assert pw == "shared-pw"


def test_service_endpoint_reports_what_is_actually_missing(tmp_path):
    """No password and no service are DIFFERENT failures — never conflated."""
    with pytest.raises(OcError, match="no password"):
        service_endpoint(
            url="http://127.0.0.1:1", service_json=tmp_path / "absent.json"
        )
    with pytest.raises(OcError, match="serve --service"):
        service_endpoint(
            service_json=write_service_json(tmp_path, '{"password": "pw"}'),
            proc_root=str(tmp_path / "empty-proc"),
            ss_output="",
        )


def test_client_for_service_builds_a_client_from_url_and_password():
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"data": [], "cursor": {}})

    client = run(
        client_for_service(
            url="http://127.0.0.1:46373",
            password="pw",
            transport=httpx.MockTransport(handler),
        )
    )
    try:
        assert run(client.messages(PIN)) == []
    finally:
        run(client.aclose())
    assert paths == [f"/api/session/{PIN}/message"]


def test_messages_are_newest_first_and_page_back_until_the_relayed_id():
    def row(msg_id):
        return {"id": msg_id, "type": "assistant", "time": {"completed": 3}}

    pages = {
        "0": [row("msg_3"), row("msg_2")],
        "2": [row("msg_1"), row("msg_0")],
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor") or "0"
        seen.append(
            {"limit": request.url.params.get("limit"), "cursor": cursor}
        )
        nxt = {"next": "2"} if cursor == "0" else {}
        return httpx.Response(200, json={"data": pages[cursor], "cursor": nxt})

    client = OpencodeClient(
        "http://127.0.0.1:46373", "pw", transport=httpx.MockTransport(handler)
    )
    try:
        out = run(client.messages(PIN, since_id="msg_1", page_limit=2))
        assert [m["id"] for m in out] == ["msg_3", "msg_2", "msg_1", "msg_0"]
        assert seen == [
            {"limit": "2", "cursor": "0"},
            {"limit": "2", "cursor": "2"},  # cursor.next walked OLDER
        ]
        # …and a relayed id already on page one costs exactly ONE request.
        seen.clear()
        run(client.messages(PIN, since_id="msg_3", page_limit=2))
        assert len(seen) == 1
    finally:
        run(client.aclose())


def test_messages_raises_when_the_session_is_gone():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "session not found"})

    client = OpencodeClient(
        "http://127.0.0.1:46373", "pw", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(OcError) as excinfo:
            run(client.messages(PIN))
    finally:
        run(client.aclose())
    assert excinfo.value.status == 404  # the relay reads this as "fall back to pane"
