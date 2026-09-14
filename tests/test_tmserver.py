"""HTTP tests for lib/tmserver.py against a real ThreadingHTTPServer with fake tmux / it2."""
import http.client
import json
import os
import threading
import time

import pytest
import tmconfig
import tmserver
from fakes import FakeIt2, FakeTmux, it2_row, pane, write_trail

TEMPLATE = ('<!doctype html><html><head><meta name="tm-token" content="__TM_TOKEN__">'
            '<meta name="tm-palette" content="__TM_PALETTE__"><style nonce="__TM_NONCE__">body{}</style></head>'
            '<body><script nonce="__TM_NONCE__">console.log(1)</script></body></html>')
SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def backend(tmp_path):
    ui = tmp_path / "index.html"
    ui.write_text(TEMPLATE)
    tmux = FakeTmux(panes=[
        pane("api", pane_id="%1", state="working", since="0", title="✳ Phase 2", path="/x/api-svc", color="#0a84ff"),
        pane("idle-one", pane_id="%2", window_id="@2", state="idle", since="0"),
    ], tty=str(tmp_path / "tty"))
    (tmp_path / "tty").write_text("")
    it2 = FakeIt2(rows=[it2_row("G1")], panes={"G1": "1"}, window="pty-CUR")
    # every directory is under tmp_path: the server must never read (or prune) the real ~/.local/state/kalmux
    return tmserver.Backend(tmux, it2, registry_dir=tmp_path / "no-registry", ui_path=ui, reapply_delay=0,
                            status_dir=tmp_path / "status", trace_dir=tmp_path / "trace",
                            config=tmconfig.load_config(path=tmp_path / "none.toml"))


@pytest.fixture
def server(backend):
    httpd = tmserver.make_server(0, backend)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield httpd.server_address[1], backend
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, token=None, host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["X-TM-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return resp.status, dict(resp.getheaders()), parsed


# ---------- page ----------
def test_index_injects_token_nonce_palette_and_csp(server):
    port, backend = server
    status, headers, page = call(port, "GET", "/")
    assert status == 200 and "__TM_" not in page and backend.token in page
    nonce = headers["Content-Security-Policy"].split("nonce-")[1].split("'")[0]
    assert page.count(f'nonce="{nonce}"') == 2 and "&quot;orange&quot;:&quot;#ff9500&quot;" in page
    assert headers["Cache-Control"] == "no-store" and "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_render_page_escapes_token():
    out = tmserver.render_page(TEMPLATE, 'a"b', "N", {"red": "#f00"})
    assert 'content="a&quot;b"' in out and "nonce=\"N\"" in out


# ---------- auth / origin ----------
def test_state_requires_token_and_loopback_origin(server):
    port, backend = server
    assert call(port, "GET", "/api/state")[0] == 403
    assert call(port, "GET", "/api/state", token="wrong")[0] == 403
    assert call(port, "GET", "/api/state", token=backend.token, host="evil.example:80")[0] == 403
    assert call(port, "GET", "/api/state", token=backend.token, origin="http://evil.example")[0] == 403
    assert call(port, "GET", "/", host="evil.example")[0] == 403
    status, _, body = call(port, "GET", "/api/state", token=backend.token, origin=f"http://localhost:{port}", host=f"localhost:{port}")
    assert status == 200 and body["ok"] is True


def test_healthz_is_open_but_host_pinned(server, monkeypatch):
    port, _ = server
    status, _, body = call(port, "GET", "/healthz")
    assert status == 200 and body["app"] == "kalmux" and body["version"] and body["tmux"] == "/fake/bin/tmux"
    assert tmserver.health(port)["app"] == "kalmux"
    assert tmserver.health(1) is None
    # health() must talk to loopback directly: urllib would route it through a system/env proxy that may be dead
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    assert tmserver.health(port)["pid"]


def test_health_recognises_a_pre_rename_server(server):
    """Every name the app ever answered with stays claimable, so `kalmux ui stop` can retire an old server
    that is still holding the port (invisible + unkillable is how the tm -> kmux rename went wrong)."""
    port, backend = server
    for legacy in ("kmux", "tmux-manager"):
        backend.health = lambda legacy=legacy: {"app": legacy, "version": "0.3.1", "pid": 4711, "started_at": 1}
        assert tmserver.health(port)["pid"] == 4711
    assert set(tmserver.LEGACY_APP_NAMES) == {"kmux", "tmux-manager"} and tmserver.APP_NAME == "kalmux"
    backend.health = lambda: {"app": "someone-else", "pid": 1}
    assert tmserver.health(port) is None


def test_rejected_post_closes_the_connection(server):
    """Early rejections happen before the body is read; keeping the socket alive would make the server parse
    the leftover JSON as the next request (observed: `501 Unsupported method ('{"session":"api"}GET')`)."""
    port, backend = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    for path, headers in (("/api/kill", {"X-TM-Token": "stale"}), ("/api/nope", {"X-TM-Token": backend.token}),
                          ("/other", {"X-TM-Token": backend.token}), ("/api/kill", {"X-TM-Token": backend.token, "Origin": "http://evil"})):
        conn.request("POST", path, body=json.dumps({"session": "api"}), headers={"Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        resp.read()
        assert resp.status in (403, 404) and resp.getheader("Connection") == "close"
        conn.request("GET", "/healthz")                        # http.client reconnects: the next request is clean
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read())["app"] == "kalmux"
    conn.close()


# ---------- state ----------
def test_state_shape(server):
    port, backend = server
    status, _, body = call(port, "GET", "/api/state", token=backend.token)
    data = body["data"]
    assert status == 200 and data["counts"]["working"] == 1 and data["counts"]["total"] == 2
    api = data["sessions"][0]
    assert api["name"] == "api" and api["in_iterm2"] is True and api["panes"][0]["guid"] == "G1"
    assert data["iterm2"]["available"] is True and data["server"]["version"] == tmserver.VERSION and "now" in data


def test_state_reports_iterm2_unavailable_while_it2_fails(server):
    port, backend = server
    backend.it2.last_rc = 127
    backend.it2.list_sessions = list                   # it2 timed out / API off: TabMap backs off
    data = call(port, "GET", "/api/state", token=backend.token)[2]["data"]
    assert data["iterm2"] == {"available": False, "installed": True, "current_window": ""}
    assert data["sessions"][0]["in_iterm2"] is False


def test_state_failure_is_a_json_500(server):
    port, backend = server

    def boom():
        raise RuntimeError("tmux exploded")
    backend.tmux.list_panes = boom
    status, _, body = call(port, "GET", "/api/state", token=backend.token)
    assert status == 500 and body["ok"] is False and "tmux exploded" in body["message"]


# ---------- actions ----------
def test_actions_roundtrip(server, tmp_path):
    port, backend = server
    tok = backend.token
    assert call(port, "POST", "/api/go", {"session": "api"}, token=tok)[2]["data"] == {"opened": False, "guid": "G1"}
    assert backend.it2.focused == ["G1"]
    status, _, body = call(port, "POST", "/api/go", {"session": "idle-one"}, token=tok)
    assert status == 200 and body["data"]["opened"] is True and backend.it2.tabs[-1][1] == "pty-CUR"
    assert call(port, "POST", "/api/open", {"session": "api", "window": "pty-OTHER"}, token=tok)[2]["data"]["window"] == "pty-OTHER"
    status, _, body = call(port, "POST", "/api/color", {"session": "api", "color": "teal"}, token=tok)
    assert status == 200 and body["data"]["color"] == "#40c8e0"
    status, _, body = call(port, "POST", "/api/new", {"name": "fresh", "cwd": str(tmp_path), "color": "pink", "start_claude": True, "open": True}, token=tok)
    assert status == 200 and body["data"]["open"]["ok"] is True and backend.tmux.has_session("fresh")
    assert ("send", "=fresh:", "claude") in backend.tmux.calls
    assert call(port, "POST", "/api/rename", {"session": "fresh", "name": "fresher"}, token=tok)[0] == 200
    assert call(port, "POST", "/api/detach", {"session": "fresher"}, token=tok)[0] == 200
    assert call(port, "POST", "/api/kill", {"session": "fresher"}, token=tok)[0] == 200 and not backend.tmux.has_session("fresher")
    assert call(port, "POST", "/api/reapply", {}, token=tok)[2]["ok"] is True
    status, _, body = call(port, "POST", "/api/next-waiting", {}, token=tok)
    assert status == 400 and "no session is waiting" in body["message"]
    backend.tmux.panes[1]["cc_state"] = "waiting"
    status, _, body = call(port, "POST", "/api/next-waiting", {}, token=tok)
    assert status == 200 and body["data"]["session"] == "idle-one"


def test_resume_and_forget_roundtrip(server, tmp_path):
    port, backend = server
    tok = backend.token
    write_trail(backend.trace_dir, SID_A, [{"ts": int(time.time()) - 60, "event": "SessionStart",
                                            "tmux_session": "api", "cwd": str(tmp_path), "color": "#bf5af2"}])
    status, _, body = call(port, "POST", "/api/resume", {"session_id": SID_A}, token=tok)
    assert status == 200 and body["data"]["name"] == "api-2" and SID_A in body["data"]["typed"]
    assert backend.tmux.has_session("api-2") and body["data"]["pane_id"].startswith("%")
    assert call(port, "POST", "/api/resume", {"session_id": SID_B}, token=tok)[0] == 400
    assert call(port, "POST", "/api/forget", {"session_id": SID_A}, token=tok)[0] == 200
    assert not (backend.trace_dir / f"{SID_A}.jsonl").exists()
    assert call(port, "POST", "/api/forget", {"session_id": SID_A}, token=tok)[0] == 400


def test_resume_never_targets_a_conversation_that_is_still_running(server, tmp_path, monkeypatch):
    """`kalmux resume alpha` used to fork whichever trail sorted first — half the time the LIVE one."""
    port, backend = server
    tok = backend.token
    now = int(time.time())
    write_trail(backend.trace_dir, SID_A, [{"ts": now - 600, "event": "SessionStart",
                                            "tmux_session": "api", "cwd": str(tmp_path)}])
    write_trail(backend.trace_dir, SID_B, [{"ts": now - 10, "event": "Stop",
                                            "tmux_session": "api", "cwd": str(tmp_path)}])
    monkeypatch.setattr(tmserver, "load_registry", lambda _d: {"api:@1.%1": {"sessionId": SID_B, "alive": True}})
    status, _, body = call(port, "POST", "/api/resume", {"session_id": SID_B}, token=tok)
    assert status == 400 and "no dead session" in body["message"]
    status, _, body = call(port, "POST", "/api/resume", {"session_id": "api"}, token=tok)
    assert status == 200 and SID_A in body["data"]["typed"]        # the dead one, not the newest
    assert body["data"]["matches"] == 1                            # the live trail is not even counted


def test_resume_by_name_says_how_many_trails_shared_it(server, tmp_path):
    port, backend = server
    now = int(time.time())
    write_trail(backend.trace_dir, SID_A, [{"ts": now - 600, "event": "Stop",
                                            "tmux_session": "api", "cwd": str(tmp_path)}])
    write_trail(backend.trace_dir, SID_B, [{"ts": now - 10, "event": "Stop",
                                            "tmux_session": "api", "cwd": str(tmp_path)}])
    status, _, body = call(port, "POST", "/api/resume", {"session_id": "api"}, token=backend.token)
    assert status == 200 and SID_B in body["data"]["typed"] and body["data"]["matches"] == 2
    assert "newest of 2; pass the session id" in body["message"]


def test_forget_drops_the_status_file_too(server, tmp_path):
    port, backend = server
    write_trail(backend.trace_dir, SID_A, [{"ts": int(time.time()), "event": "SessionStart"}])
    leftover = backend.status_dir / f"{SID_A}.json"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text(json.dumps({"ts": 1, "session_id": SID_A}))
    assert call(port, "POST", "/api/forget", {"session_id": SID_A}, token=backend.token)[0] == 200
    assert not leftover.exists() and not (backend.trace_dir / f"{SID_A}.jsonl").exists()


def test_backend_rereads_the_config_when_the_file_changes(backend, tmp_path, monkeypatch):
    """An AutoLaunch daemon outlives many config edits, and a stale keep_days DELETES trail files."""
    path = tmp_path / "config.toml"
    path.write_text("[tombstones]\nkeep_days = 7\n")
    monkeypatch.setattr(tmserver, "config_path", lambda: path)
    backend._injected_config = None                      # the production path: no config was injected
    assert backend.keep_days == 7
    path.write_text("[tombstones]\nkeep_days = 11\n")
    os.utime(path, (2_000_000, 2_000_000))
    assert backend.keep_days == 11                       # no restart needed
    path.unlink()
    assert backend.keep_days == 30                       # file gone: back to the defaults, still no restart


def test_a_response_holding_nan_becomes_a_clean_500(server, monkeypatch):
    """NaN is not JSON: json.dumps would happily write it and the page's JSON.parse() would throw."""
    port, backend = server
    monkeypatch.setattr(backend, "state", lambda: {"now": float("nan"), "sessions": []})
    status, _, body = call(port, "GET", "/api/state", token=backend.token)
    assert status == 500 and body["ok"] is False and "unrepresentable" in body["message"]


def test_state_carries_context_quota_and_tombstones(server, tmp_path):
    port, backend = server
    write_trail(backend.trace_dir, SID_B, [{"ts": int(time.time()) - 300, "event": "SessionStart",
                                            "tmux_session": "old", "cwd": str(tmp_path)}])
    data = call(port, "GET", "/api/state", token=backend.token)[2]["data"]
    assert data["quota"] is None and [t["session_id"] for t in data["tombstones"]] == [SID_B]
    assert data["tombstones"][0]["ended"] == "killed" and data["sessions"][0]["ctx_pct"] is None


def test_ui_palette_offers_twenty_colors():
    assert len(tmserver.UI_PALETTE) == 20 and "grey" not in tmserver.UI_PALETTE
    assert tmserver.UI_PALETTE["indigo"].startswith("#") and tmserver.UI_PALETTE["orange"] == "#ff9500"


def test_action_validation_and_errors(server):
    port, backend = server
    tok = backend.token
    assert call(port, "POST", "/api/color", {"session": "api", "color": "zzz"}, token=tok)[0] == 400
    assert call(port, "POST", "/api/kill", {"session": "api"})[0] == 403
    assert call(port, "POST", "/api/nope", {}, token=tok)[0] == 404
    assert call(port, "POST", "/other", {}, token=tok)[0] == 404
    assert call(port, "POST", "/api/color", "{not json", token=tok)[0] == 400
    assert call(port, "POST", "/api/color", "[1,2]", token=tok)[0] == 400
    assert call(port, "POST", "/api/color", "x" * (tmserver.MAX_BODY + 1), token=tok)[0] == 400
    assert call(port, "POST", "/api/color", {"session": 5, "color": None}, token=tok)[0] == 400
    assert call(port, "GET", "/api/other", token=tok)[0] == 404
    assert call(port, "HEAD", "/")[0] == 200
    assert call(port, "GET", "/favicon.ico")[0] == 204


def test_action_exception_is_a_json_500(server):
    port, backend = server

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")
    backend.tmux.has_session = boom
    status, _, body = call(port, "POST", "/api/kill", {"session": "api"}, token=backend.token)
    assert status == 500 and "kaboom" in body["message"]


def test_page_missing_ui_file(server, tmp_path):
    port, backend = server
    backend.ui_path = tmp_path / "gone.html"
    status, _, body = call(port, "GET", "/")
    assert status == 500 and "ui file missing" in body["message"]


# ---------- lifecycle ----------
def test_serve_runs_writes_pidfile_and_detects_existing_instance(backend, tmp_path, capsys):
    ready = {}
    pidfile = tmp_path / "ui.pid"
    thread = threading.Thread(target=tmserver.serve, args=(0, backend, pidfile), kwargs={"on_ready": lambda h: ready.setdefault("httpd", h)}, daemon=True)
    thread.start()
    for _ in range(100):
        if "httpd" in ready:
            break
        time.sleep(0.02)
    httpd = ready["httpd"]
    port = httpd.server_address[1]
    assert pidfile.read_text().strip().isdigit() and tmserver.health(port)
    assert tmserver.serve(port, backend) == 0            # second instance: already running -> exit 0
    httpd.shutdown()
    thread.join(timeout=5)
    assert not pidfile.exists()
    out = capsys.readouterr().out
    assert "listening on" in out and "already running" in out


def test_serve_reports_foreign_port_owner(backend, monkeypatch, capsys):
    monkeypatch.setattr(tmserver, "make_server", lambda *_a, **_k: (_ for _ in ()).throw(OSError("in use")))
    monkeypatch.setattr(tmserver, "health", lambda *_a, **_k: None)
    assert tmserver.serve(1, backend) == 1
    assert "cannot bind" in capsys.readouterr().out
