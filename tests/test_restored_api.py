"""The restored sessions as the toolbelt and the CLI see them: /api/state, Open all, Dismiss, a card click,
`kalmux open --restored`, and `kalmux restore` following [restore] open_tabs."""
import threading
import time

import pytest

from fakes import FakeIt2, FakeTmux, it2_row, pane
from kalmux import tmactions, tmconfig, tmrestore, tmserver, tmsnapshot
from kalmux.tmcore import RESTORED_OPTION
from test_tm import write_sessions
from test_tmserver import TEMPLATE, call


def restored_tmux(tmp_path, *names, attached=()):
    panes = [pane(n, pane_id=f"%{i}", window_id=f"@{i}", cmd="zsh", title="", attached="1" if n in attached else "0",
                  path=f"/x/{n}") for i, n in enumerate(names)]
    tmux = FakeTmux(panes=panes, tty=str(tmp_path / "tty"))
    (tmp_path / "tty").write_text("")
    return tmux


@pytest.fixture
def rbackend(tmp_path):
    ui = tmp_path / "index.html"
    ui.write_text(TEMPLATE)
    tmux = restored_tmux(tmp_path, "api", "web", "db", "live")
    for name in ("web", "db"):
        tmsnapshot.mark_restored(tmux, name, 1_790_000_000)
    tmsnapshot.mark_restored(tmux, "live", 1_789_000_000)            # marked, but not in the list any more
    tmux.panes[3]["attached"] = "1"                                    # a client attached it meanwhile
    it2 = FakeIt2(rows=[it2_row("G1")], panes={"G1": "0"}, window="pty-CUR")
    return tmserver.Backend(tmux, it2, registry_dir=tmp_path / "no-registry", ui_path=ui, reapply_delay=0,
                            status_dir=tmp_path / "status", trace_dir=tmp_path / "trace",
                            config=tmconfig.load_config(path=tmp_path / "none.toml"))


@pytest.fixture
def rserver(rbackend):
    httpd = tmserver.make_server(0, rbackend)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield httpd.server_address[1], rbackend
    httpd.shutdown()
    httpd.server_close()


def marked(backend, name):
    return backend.tmux.options.get((name, RESTORED_OPTION), "")


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------- /api/state ----------
def test_state_carries_the_restored_flag_per_session_and_a_summary(rbackend):
    state = rbackend.state()
    rows = {s["name"]: s for s in state["sessions"]}
    assert rows["web"]["restored"] is True and rows["web"]["restored_at"] == 1_790_000_000
    assert rows["api"]["restored"] is False and rows["api"]["restored_at"] is None
    assert rows["live"]["restored"] is False                          # attached: no longer waiting for a tab
    summary = state["restored"]
    assert summary["count"] == 2 and summary["at"] == 1_790_000_000 and summary["names"] == ["web", "db"]
    assert summary["opening"] == {"running": False, "done": 0, "failed": 0, "total": 0}


def test_state_without_restored_sessions_has_an_empty_summary(tmp_path):
    snap = tmactions.snapshot(restored_tmux(tmp_path, "api"), tmp_path / "none", None, now=1)
    assert snap["restored"] == {"count": 0, "at": None, "names": [], "fallbacks": 0}


# ---------- a card click clears the mark ----------
def test_go_on_a_restored_session_opens_its_tab_and_clears_its_mark(rbackend):
    r = rbackend.do("go", {"session": "web"})
    assert r.ok and r.data["opened"] is True and marked(rbackend, "web") == ""
    assert marked(rbackend, "db") == "1790000000"


def test_open_clears_the_mark_and_a_failed_open_keeps_it(rbackend):
    rbackend.it2.fail_tab = True
    assert rbackend.do("open", {"session": "db"}).ok is False and marked(rbackend, "db")
    rbackend.it2.fail_tab = False
    assert rbackend.do("open", {"session": "db"}).ok is True and marked(rbackend, "db") == ""


def test_open_of_an_unmarked_session_never_writes_an_option(rbackend):
    rbackend.tmux.calls.clear()                                       # the fixture's own marks
    assert rbackend.do("open", {"session": "api"}).ok
    assert not any(c[0] == "set" for c in rbackend.tmux.calls)


# ---------- Dismiss ----------
def test_dismiss_clears_every_mark_or_just_one(rbackend):
    r = rbackend.do("dismiss-restored", {"session": "web"})
    assert r.ok and r.data["dismissed"] == ["web"] and marked(rbackend, "db")
    r = rbackend.do("dismiss-restored", {})
    assert r.ok and r.data["dismissed"] == ["db"] and rbackend.state()["restored"]["count"] == 0


def test_dismiss_refuses_a_session_that_is_not_waiting(rbackend):
    for body in ({"session": "api"}, {"session": "nope"}, {"session": ["web"]}):
        r = rbackend.do("dismiss-restored", body)
        assert r.ok is False and marked(rbackend, "web")
    rbackend.do("dismiss-restored", {})
    assert rbackend.do("dismiss-restored", {}).ok is False            # nothing left to dismiss


# ---------- Open all ----------
def test_open_all_opens_every_marked_session_on_a_thread_of_its_own(rbackend):
    r = rbackend.do("open-restored", {})
    assert r.ok and r.data == {"total": 2, "names": ["web", "db"]}
    rbackend.opener.join(5)
    assert [w for _c, w in rbackend.it2.tabs] == ["pty-CUR", "pty-CUR"]
    assert marked(rbackend, "web") == "" and marked(rbackend, "db") == ""
    assert rbackend.state()["restored"]["opening"] == {"running": False, "done": 2, "failed": 0, "total": 2}


def test_open_all_never_holds_the_server_lock_while_iterm2_opens_a_tab(rbackend):
    """Like the restore: the lock is held around the tmux calls only, so a click on another card is served
    while it2 is still busy with a tab (6 s each when iTerm2 is wedged)."""
    gate = threading.Event()
    tab = rbackend.it2.new_tab

    def slow_tab(command, window=""):
        if "=web" in command:
            gate.wait(5)
        return tab(command, window)

    rbackend.it2.new_tab = slow_tab
    assert rbackend.do("open-restored", {}).ok
    assert wait_for(lambda: rbackend.state()["restored"]["opening"]["running"])
    released, between = threading.Event(), {}

    def other_click():
        between["color"] = rbackend.do("color", {"session": "api", "color": "teal"}).ok
        released.set()

    threading.Thread(target=other_click, daemon=True).start()
    try:
        assert released.wait(5) and between["color"] is True          # served while the tab is still opening
        progress = rbackend.state()["restored"]["opening"]
        assert progress["running"] is True and progress["total"] == 2 and progress["done"] == 0
    finally:
        gate.set()
    rbackend.opener.join(5)
    assert rbackend.state()["restored"]["opening"]["done"] == 2


def test_a_card_click_during_open_all_never_gives_a_session_two_tabs(rbackend):
    """Open all works through a list fixed when it starts; a session opened by a click (or dismissed)
    meanwhile is skipped, and the batch's total shrinks to what it still opens."""
    gate, at_web = threading.Event(), threading.Event()
    tab = rbackend.it2.new_tab

    def slow_tab(command, window=""):
        if "=web" in command:
            at_web.set()
            gate.wait(5)
        return tab(command, window)

    rbackend.it2.new_tab = slow_tab
    assert rbackend.do("open-restored", {}).ok
    assert at_web.wait(5)
    clicked = {}
    click = threading.Thread(target=lambda: clicked.update(r=rbackend.do("go", {"session": "db"})), daemon=True)
    click.start()
    click.join(5)
    gate.set()
    rbackend.opener.join(5)
    assert clicked["r"].ok
    assert sum("=db" in c for c, _w in rbackend.it2.tabs) == 1
    assert rbackend.state()["restored"]["opening"] == {"running": False, "done": 1, "failed": 0, "total": 1}


def test_a_click_on_the_session_open_all_is_opening_right_now_waits_for_it(rbackend):
    gate, at_web = threading.Event(), threading.Event()
    tab = rbackend.it2.new_tab

    def slow_tab(command, window=""):
        if "=web" in command:
            at_web.set()
            gate.wait(5)
        return tab(command, window)

    rbackend.it2.new_tab = slow_tab
    assert rbackend.do("open-restored", {}).ok
    assert at_web.wait(5)
    for action in ("go", "open"):
        r = rbackend.do(action, {"session": "web"})
        assert r.ok is False and "Open all" in r.message
    gate.set()
    rbackend.opener.join(5)
    assert sum("=web" in c for c, _w in rbackend.it2.tabs) == 1


def test_open_all_skips_a_session_dismissed_over_the_api_meanwhile(rbackend):
    gate, at_web = threading.Event(), threading.Event()
    tab = rbackend.it2.new_tab

    def slow_tab(command, window=""):
        if "=web" in command:
            at_web.set()
            gate.wait(5)
        return tab(command, window)

    rbackend.it2.new_tab = slow_tab
    assert rbackend.do("open-restored", {}).ok
    assert at_web.wait(5)
    assert rbackend.do("dismiss-restored", {"session": "db"}).ok
    gate.set()
    rbackend.opener.join(5)
    assert not any("=db" in c for c, _w in rbackend.it2.tabs)


def test_open_restored_on_the_command_line_skips_what_was_opened_meanwhile(tmp_path, monkeypatch):
    from kalmux import cli
    tmux = restored_tmux(tmp_path, "web", "db")
    for name in ("web", "db"):
        tmsnapshot.mark_restored(tmux, name, 1_790_000_000)
    it2 = FakeIt2(window="pty-CUR")
    tab = it2.new_tab

    def tab_then_attach_db(command, window=""):
        tmux.panes[1]["attached"] = "1"                 # someone attached db while web's tab opened
        return tab(command, window)

    it2.new_tab = tab_then_attach_db
    monkeypatch.setattr(cli, "reapply", lambda *a, **kw: None)
    assert cli.cmd_open_restored(tmux, it2, wait=0) == 0
    assert [("=db" in c) for c, _w in it2.tabs] == [False]


def test_open_all_reports_failures_and_keeps_their_marks(rbackend, capsys):
    rbackend.it2.fail_tab = True
    assert rbackend.do("open-restored", {}).ok
    rbackend.opener.join(5)
    assert rbackend.state()["restored"]["opening"] == {"running": False, "done": 0, "failed": 2, "total": 2}
    assert marked(rbackend, "web") and marked(rbackend, "db")
    assert "0 of 2 restored session(s) opened" in capsys.readouterr().out


def test_open_all_that_explodes_still_ends_and_says_why(rbackend, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("it2 exploded")
    monkeypatch.setattr(tmrestore, "open_tabs", boom)
    assert rbackend.do("open-restored", {}).ok
    rbackend.opener.join(5)
    assert rbackend.state()["restored"]["opening"]["running"] is False
    assert "it2 exploded" in capsys.readouterr().out


def test_open_all_refuses_a_second_batch_and_an_empty_one(rbackend):
    gate = threading.Event()
    tab = rbackend.it2.new_tab
    rbackend.it2.new_tab = lambda command, window="": gate.wait(5) and tab(command, window)
    assert rbackend.do("open-restored", {}).ok
    again = rbackend.do("open-restored", {})
    assert again.ok is False and "already" in again.message
    gate.set()
    rbackend.opener.join(5)
    empty = rbackend.do("open-restored", {})
    assert empty.ok is False and "no restored session" in empty.message


def test_open_all_without_iterm2_says_so(rbackend):
    rbackend.it2._available = False
    r = rbackend.do("open-restored", {})
    assert r.ok is False and "iTerm2" in r.message and rbackend.opener is None


# ---------- over HTTP: token-protected like every other action ----------
def test_the_new_actions_need_the_token_and_a_json_body(rserver):
    port, backend = rserver
    for action in ("open-restored", "dismiss-restored"):
        assert call(port, "POST", f"/api/{action}", {})[0] == 403
        assert call(port, "POST", f"/api/{action}", b"[1]", token=backend.token)[0] == 400
    status, _h, body = call(port, "POST", "/api/dismiss-restored", {"session": "web"}, token=backend.token)
    assert status == 200 and body["ok"] is True
    status, _h, body = call(port, "GET", "/api/state", token=backend.token)
    assert body["data"]["restored"]["names"] == ["db"]


# ---------- the CLI ----------
def test_cmd_open_restored_opens_every_marked_session_and_clears_them(tm, tmp_path, monkeypatch, capsys):
    tmux = restored_tmux(tmp_path, "web", "db")
    for name in ("web", "db"):
        tmsnapshot.mark_restored(tmux, name, 5)
    it2 = FakeIt2(window="pty-CUR")
    assert tm.cmd_open_restored(tmux, it2, wait=0) == 0
    assert len(it2.tabs) == 2 and not tmux.options
    assert "2 of 2 restored session(s) opened" in capsys.readouterr().out
    assert tm.cmd_open_restored(tmux, it2, wait=0) == 0
    assert "no restored session" in capsys.readouterr().out


def test_cmd_open_restored_fails_without_iterm2_or_with_a_failing_tab(tm, tmp_path, capsys):
    tmux = restored_tmux(tmp_path, "web")
    tmsnapshot.mark_restored(tmux, "web", 5)
    assert tm.cmd_open_restored(tmux, FakeIt2(available=False), wait=0) == 1
    assert "iTerm2" in capsys.readouterr().err
    it2 = FakeIt2(window="pty-CUR")
    it2.fail_tab = True
    assert tm.cmd_open_restored(tmux, it2, wait=0) == 1 and tmux.options


def test_main_open_takes_a_session_or_restored_but_not_both(tm, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(tm, "Tmux", lambda: FakeTmux(panes=[]))
    monkeypatch.setattr(tm, "It2", FakeIt2)
    monkeypatch.setattr(tm, "cmd_open_restored", lambda *a, **k: seen.setdefault("restored", (a, k)) and 0)
    monkeypatch.setattr(tm, "cmd_open", lambda *a, **k: seen.setdefault("open", (a, k)) and 0)
    assert tm.main(["open", "--restored"]) == 0 and "restored" in seen
    assert tm.main(["open", "api"]) == 0 and seen["open"][0][2] == "api"
    assert tm.main(["open", "api", "--restored"]) == 2
    assert tm.main(["open"]) == 2
    assert "a session name or --restored" in capsys.readouterr().err


def test_cmd_restore_follows_open_tabs_and_marks_what_it_does_not_open(tm, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tm.os, "environ", {"TERM_PROGRAM": "iTerm.app"})
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 222)
    path = write_sessions(tmp_path / "sessions.json", 111, "web", cwd=str(tmp_path))
    tmux, it2 = FakeTmux(panes=[]), FakeIt2(window="pty-CUR")
    assert tm.cmd_restore(tmux, it2, path, tmp_path / "prev.json") == 0
    assert it2.tabs == [] and tmux.options.get(("web", RESTORED_OPTION))
    assert "recreated, not opened" in capsys.readouterr().out
    tmux, it2 = FakeTmux(panes=[]), FakeIt2(window="pty-CUR")
    assert tm.cmd_restore(tmux, it2, path, tmp_path / "prev.json", tabs=True) == 0
    assert len(it2.tabs) == 1 and not tmux.options.get(("web", RESTORED_OPTION))


def test_main_restore_takes_tabs_from_the_config_unless_a_flag_says_otherwise(tm, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tm, "Tmux", lambda: FakeTmux(panes=[]))
    monkeypatch.setattr(tm, "It2", FakeIt2)
    monkeypatch.setattr(tm, "cmd_restore", lambda *a, **k: seen.append(k["tabs"]) or 0)
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("KALMUX_CONFIG", str(cfg))
    assert tm.main(["restore"]) == 0                                   # no file: the default, off
    cfg.write_text("[restore]\nopen_tabs = true\n")
    assert tm.main(["restore"]) == 0 and tm.main(["restore", "--no-tabs"]) == 0
    cfg.write_text("[restore]\nopen_tabs = false\n")
    assert tm.main(["restore", "--tabs"]) == 0
    assert seen == [False, True, False, True]
    assert tm.main(["restore", "--tabs", "--no-tabs"]) == 2
