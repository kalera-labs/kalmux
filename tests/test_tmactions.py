"""Unit tests for lib/tmactions.py (TabMap, snapshot, status replay, actions)."""
import base64
import json
import os

import pytest
import tmactions as ta
import tmconfig
from fakes import FakeIt2, FakeTmux, it2_row, pane, write_status, write_trail


# ---------- TabMap ----------
def test_tabmap_maps_pane_ids_and_caches_lookups():
    it2 = FakeIt2(rows=[it2_row("G0", "tmux (tmux)", is_tmux=False), it2_row("G1", tab="2"), it2_row("G2", tab="4")],
                  panes={"G1": "3", "G2": "5"})
    tabmap = ta.TabMap(it2)
    assert tabmap.refresh() == {"%3": {"guid": "G1", "window": "pty-W1", "tab": "2"}, "%5": {"guid": "G2", "window": "pty-W1", "tab": "4"}}
    assert it2.get_var_calls == 2                      # the gateway row (is_tmux false) is never queried
    tabmap.refresh(force=True)
    assert it2.get_var_calls == 2                      # known GUIDs are cached
    it2.rows = [r for r in it2.rows if r["guid"] != "G2"]
    assert "%5" not in tabmap.refresh(force=True)      # gone GUID drops out of the map


def test_tabmap_retries_failed_lookups_and_honours_ttl():
    it2 = FakeIt2(rows=[it2_row("G1")], panes={})
    tabmap = ta.TabMap(it2, ttl=1000)
    assert tabmap.refresh() == {}
    assert tabmap.refresh() == {} and it2.get_var_calls == 1   # within ttl: no new call
    it2.pane_of["G1"] = "7"
    assert tabmap.refresh(force=True) == {"%7": {"guid": "G1", "window": "pty-W1", "tab": "2"}}
    assert it2.get_var_calls == 2


def test_tabmap_backs_off_while_it2_is_failing():
    it2 = FakeIt2(rows=[it2_row("G1")], panes={"G1": "1"})
    it2.last_rc = 127
    it2.list_sessions = list                          # e.g. it2 timed out
    tabmap = ta.TabMap(it2, ttl=0, backoff=1000)
    assert tabmap.refresh() == {} and tabmap.refresh() == {}
    assert tabmap._down_until > 0
    it2.last_rc = 0
    it2.list_sessions = lambda: [it2_row("G1")]
    assert tabmap.refresh() == {}                             # still backing off
    assert tabmap.refresh(force=True) == {"%1": {"guid": "G1", "window": "pty-W1", "tab": "2"}}
    assert tabmap._down_until == 0.0


def test_tabmap_builds_on_first_refresh_right_after_boot(monkeypatch):
    """Found on 2026-09-13 after a machine crash: time.monotonic() counts from boot on macOS, so a cache
    stamp of 0.0 looked 'fresh' for the first ttl seconds of uptime and the first refresh skipped _build."""
    monkeypatch.setattr(ta.time, "monotonic", lambda: 5.0)          # 5 s after boot
    it2 = FakeIt2(rows=[it2_row("G1")], panes={"G1": "1"})
    tabmap = ta.TabMap(it2, ttl=1000)
    assert tabmap.refresh() == {"%1": {"guid": "G1", "window": "pty-W1", "tab": "2"}} and it2.get_var_calls == 1
    assert tabmap.refresh() == {"%1": {"guid": "G1", "window": "pty-W1", "tab": "2"}} and it2.get_var_calls == 1


def test_tabmap_without_iterm2_is_empty():
    assert ta.TabMap(FakeIt2(rows=[it2_row("G1")], panes={"G1": "1"}, available=False)).refresh() == {}


# ---------- snapshot ----------
def test_snapshot_groups_panes_and_aggregates_state(tmp_path):
    now = 1000
    tmux = FakeTmux(panes=[
        pane("a", pane_id="%1", window_id="@1", state="idle", since="900", title="A idle", path="/x/proj-a", color="#ff9500"),
        pane("a", pane_id="%2", window_id="@2", state="waiting", detail="Allow Bash?", since="990", title="A waiting", path="/x/proj-a", color="#ff9500", active=False),
        pane("b", pane_id="%3", window_id="@3", cmd="zsh", title="shell", path="/x/b", attached="0"),
    ])
    it2 = FakeIt2(rows=[it2_row("G1", tab="1")], panes={"G1": "1"})
    snap = ta.snapshot(tmux, tmp_path / "no-registry", ta.TabMap(it2), now=now)
    a, b = snap["sessions"]
    assert a["name"] == "a" and a["state"] == "waiting" and a["age"] == 10 and a["detail"] == "Allow Bash?"
    assert a["title"] == "A waiting" and a["project"] == "proj-a" and a["color"] == "#ff9500" and a["attached"] == 1
    assert a["in_iterm2"] is True and a["panes"][0]["guid"] == "G1" and a["panes"][1]["guid"] == ""
    assert b["state"] == "" and b["in_iterm2"] is False and b["attached"] == 0
    assert snap["counts"] == {"waiting": 1, "working": 0, "idle": 0, "busy": 0, "unknown": 0, "stale": 0, "gone": 0, "total": 2}
    assert ta.next_waiting(snap["sessions"]) == "a"


def test_snapshot_counts_busy_panes(tmp_path):
    tmux = FakeTmux(panes=[pane("cc", state="waiting", since="1"), pane("build", pane_id="%2", cmd="make"),
                           pane("free", pane_id="%3", cmd="zsh")])
    snap = ta.snapshot(tmux, tmp_path / "none", None, now=100)
    assert snap["counts"]["busy"] == 1 and snap["counts"]["waiting"] == 1 and snap["counts"]["total"] == 3
    by_name = {s["name"]: s for s in snap["sessions"]}
    assert by_name["build"]["state"] == "busy" and by_name["build"]["detail"] == "make"
    assert by_name["free"]["state"] == ""
    assert [s["name"] for s in snap["sessions"]] == ["cc", "build", "free"]      # urgency order


def test_session_with_a_dead_claude_is_gone_not_busy(tmp_path):
    """A pane whose Claude died while its shell runs something must still read as gone (review 2026-09-14)."""
    (tmp_path / "reg").mkdir()
    (tmp_path / "reg" / "1.json").write_text('{"pid": 1, "sessionId": "s", "tmux": "dead:@1.%1", "status": "busy"}')
    tmux = FakeTmux(panes=[pane("dead", pane_id="%1", cmd="make"), pane("dead", pane_id="%2", window_id="@2", cmd="python3")])
    import tmcore
    snap = ta.snapshot(tmux, tmp_path / "reg", None, now=100) if tmcore.pid_alive(1) is False else None
    if snap is None:      # pid 1 answers kill -0 on this platform: force the dead branch through load_registry's hook
        import tmactions
        real = tmactions.load_registry
        try:
            tmactions.load_registry = lambda d: real(d, alive=lambda pid: False)
            snap = ta.snapshot(tmux, tmp_path / "reg", None, now=100)
        finally:
            tmactions.load_registry = real
    s = snap["sessions"][0]
    assert s["state"] == "gone" and snap["counts"]["gone"] == 1 and snap["counts"]["busy"] == 0


def test_snapshot_tie_prefers_active_pane_and_sorts_by_urgency(tmp_path):
    tmux = FakeTmux(panes=[
        pane("z", pane_id="%1", state="working", since="0", title="inactive", active=False),
        pane("z", pane_id="%2", window_id="@2", state="working", since="0", title="active"),
        pane("m", pane_id="%3", window_id="@3", state="idle", since="0"),
    ])
    snap = ta.snapshot(tmux, tmp_path, None, now=5)
    assert [s["name"] for s in snap["sessions"]] == ["z", "m"]
    assert snap["sessions"][0]["title"] == "active"
    assert ta.next_waiting(snap["sessions"]) is None


# ---------- status replay ----------
def test_status_sequence_replays_status_uservar_and_color():
    seq = ta.status_sequence("working", "Bash; rm", "#0a84ff")
    assert "\x1b]21337;status=working;indicator=#ff9500;status-color=#ff9500;detail=Bash, rm\x1b\\" in seq
    assert "SetUserVar=cc_state=" + base64.b64encode(b"working").decode() in seq
    assert "]6;1;bg;red;brightness;10" in seq and seq.startswith("\x1b\\")
    # no state to replay: emit the hook's own clear, or a killed session keeps its dot on the tab
    cleared = ta.status_sequence("", "", "nothex")
    assert cleared == "\x1b\\" + ta.CLEAR_STATUS
    assert "status=;indicator=;status-color=;detail=" in cleared and cleared.endswith("SetUserVar=cc_state=\x07")
    assert "status=working;" in ta.status_sequence("work;ing\x1b", "", "")      # state is one OSC parameter word
    assert "status=idle;indicator=#00d75f;status-color=#888888" in ta.status_sequence("idle", "", "")


def test_reapply_writes_attached_panes_only(ptty, tmp_path):
    tty, read = ptty
    plain = tmp_path / "not-a-tty"
    plain.write_text("")
    tmux = FakeTmux(panes=[
        pane("a", state="working", tty=tty, color="#ff9500"),
        pane("b", pane_id="%2", state="idle", tty=tty, attached="0"),
        pane("c", pane_id="%3", state="idle", tty=""),
        pane("d", pane_id="%4", state="idle", tty=str(plain)),       # forged tty: a regular file is never written
    ])
    r = ta.reapply(tmux)
    assert r.ok and r.data["panes"] == 1
    assert "status=working" in read() and plain.read_text() == ""
    assert ta.reapply(tmux, "b").data["panes"] == 0


# ---------- go / open ----------
def test_action_go_focuses_the_active_mapped_pane():
    tmux = FakeTmux(panes=[pane("api", pane_id="%1", active=False), pane("api", pane_id="%2", window_id="@2")])
    it2 = FakeIt2(rows=[it2_row("G1"), it2_row("G2")], panes={"G1": "1", "G2": "2"})
    r = ta.action_go(tmux, it2, ta.TabMap(it2), "api")
    assert r.ok and it2.focused == ["G2"] and r.data == {"opened": False, "guid": "G2"}


def test_action_go_opens_a_tab_when_session_has_none():
    tmux = FakeTmux(panes=[pane("api")])
    it2 = FakeIt2(rows=[], window="pty-CUR")
    r = ta.action_go(tmux, it2, ta.TabMap(it2), "api")
    assert r.ok and r.data["opened"] is True
    assert it2.tabs == [("""/bin/zsh -lc 'exec tmux -CC attach -t "=api"'""", "pty-CUR")]


def test_action_go_refuses_to_open_while_it2_is_down():
    """An empty TabMap because `it2 session list` failed is not "this session has no tab": opening would stack a
    second control-mode client on a session that most likely already has one."""
    tmux = FakeTmux(panes=[pane("api")])
    it2 = FakeIt2(rows=[], window="pty-CUR")
    it2.last_rc = 127
    r = ta.action_go(tmux, it2, ta.TabMap(it2), "api")
    assert r.ok is False and "not answering" in r.message and it2.tabs == []


def test_action_go_errors():
    tmux = FakeTmux(panes=[pane("api")])
    assert ta.action_go(tmux, FakeIt2(), ta.TabMap(FakeIt2()), "nope").ok is False
    r = ta.action_go(tmux, FakeIt2(available=False), ta.TabMap(FakeIt2(available=False)), "api", env={})
    assert r.ok is False and "tmux attach -t api" in r.message
    it2 = FakeIt2(rows=[it2_row("G1")], panes={"G1": "1"})
    it2.fail_focus = True
    assert "could not focus" in ta.action_go(tmux, it2, ta.TabMap(it2), "api").message


def test_action_open_validates_name_and_reports_it2_failure():
    tmux = FakeTmux(panes=[pane("bad name;rm"), pane("ok", pane_id="%2")])
    assert ta.action_open(tmux, FakeIt2(), "missing").ok is False
    assert ta.action_open(tmux, FakeIt2(available=False), "ok").ok is False
    assert "not safe" in ta.action_open(tmux, FakeIt2(), "bad name;rm").message
    it2 = FakeIt2()
    it2.fail_tab = True
    assert "boom" in ta.action_open(tmux, it2, "ok").message
    assert ta.action_open(tmux, FakeIt2(), "ok", window="pty-X").data["window"] == "pty-X"


def test_action_open_falls_back_when_iterm2_has_no_current_window():
    tmux = FakeTmux(panes=[pane("ok")])
    it2 = FakeIt2(window="")
    it2.windows = ["pty-FRONT", "pty-BACK"]
    r = ta.action_open(tmux, it2, "ok")
    assert r.ok and r.data["window"] == "pty-FRONT" and it2.tabs[-1][1] == "pty-FRONT"
    it2.windows = []
    r = ta.action_open(tmux, it2, "ok")
    assert r.ok and "new window" in r.message and it2.new_windows == ["""/bin/zsh -lc 'exec tmux -CC attach -t "=ok"'"""]
    it2.fail_tab = True
    assert "window new failed" in ta.action_open(tmux, it2, "ok").message


# ---------- color / new / kill / rename / detach ----------
def test_action_color_sets_option_and_writes_every_pane(ptty):
    tty, read = ptty
    tmux = FakeTmux(panes=[pane("api", pane_id="%0", tty=tty), pane("api", pane_id="%5", window_id="@9", tty=tty),
                           pane("other", pane_id="%2", tty=tty)])
    r = ta.action_color(tmux, "api", "orange")
    assert r.ok and ("set", "api", "@tm_color", "#ff9500") in tmux.calls and r.data["written"] == 2
    assert read().count("]6;1;bg;red;brightness;255") == 2
    assert ta.action_color(tmux, "api", "none").data["color"] == "" and "default" in read()
    assert ta.action_color(tmux, "api", "zzz").ok is False
    assert ta.action_color(tmux, "nope", "red").ok is False
    tmux.fail.add("set")
    assert "refused" in ta.action_color(tmux, "api", "red").message


def test_action_new_validates_then_creates(tmp_path):
    tmux = FakeTmux(panes=[pane("exists")])
    assert "must start with" in ta.action_new(tmux, "bad name").message and ta.action_new(tmux, "-dash").ok is False
    assert "already exists" in ta.action_new(tmux, "exists").message
    assert "not a directory" in ta.action_new(tmux, "p1", cwd=str(tmp_path / "missing")).message
    assert ta.action_new(tmux, "p1", color="zzz").ok is False
    r = ta.action_new(tmux, "p1", cwd=str(tmp_path), color="teal", start_claude=True)
    assert r.ok and ("new", "p1", str(tmp_path)) in tmux.calls
    assert ("set", "p1", "@tm_color", "#40c8e0") in tmux.calls and ("send", "=p1:", "claude") in tmux.calls
    tmux.fail.add("new")
    assert "could not create" in ta.action_new(tmux, "p2").message


def test_action_kill_rename_detach():
    tmux = FakeTmux(panes=[pane("a"), pane("b", pane_id="%2")])
    assert ta.action_rename(tmux, "a", "bad.name").ok is False
    assert ta.action_rename(tmux, "a", "b").ok is False
    assert ta.action_rename(tmux, "zz", "c").ok is False
    assert ta.action_rename(tmux, "a", "c").ok and tmux.has_session("c")
    assert ta.action_detach(tmux, "c").ok and ("detach", "c") in tmux.calls
    assert ta.action_detach(tmux, "zz").ok is False
    assert ta.action_kill(tmux, "zz").ok is False
    assert ta.action_kill(tmux, "c").ok and not tmux.has_session("c")
    tmux.fail.update({"kill", "rename", "detach"})
    assert ta.action_kill(tmux, "b").ok is False and ta.action_rename(tmux, "b", "d").ok is False and ta.action_detach(tmux, "b").ok is False


@pytest.mark.parametrize("value,expected", [(None, ""), (12, ""), ("a\x1bb\x00c", "abc"), ("  x  ", "x"), ("y" * 300, "y" * 256)])
def test_text_coercion(value, expected):
    assert ta._text(value) == expected


# ---------- phase 4: context, quota, tombstones in the snapshot ----------
SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SID_C = "99999999-8888-7777-6666-555555555555"
SID_D = "0f0f0f0f-1111-2222-3333-444444444444"


def _registry_dir(tmp_path, pairs):
    """A ~/.claude/sessions directory with one live record per (tmux key, session id)."""
    reg = tmp_path / "reg"
    reg.mkdir(exist_ok=True)
    for i, (key, sid) in enumerate(pairs, start=1):
        (reg / f"{i}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "tmux": key, "status": "busy"}))
    return reg


def test_snapshot_adds_context_quota_and_tombstones(tmp_path):
    now = 10_000
    reg = _registry_dir(tmp_path, [("a:@1.%1", SID_A), ("b:@2.%2", SID_B)])
    status = tmp_path / "status"
    write_status(status, SID_A, ts=now - 10, context_pct=42, model="Opus 5", cost_usd=1.23,
                 rate_limits={"five_hour": {"used_pct": 35, "resets_at": now + 600}})
    write_status(status, SID_B, ts=now - 10, context_pct=88, model="Sonnet 5", cost_usd=0.2,
                 rate_limits={"five_hour": {"used_pct": 70, "resets_at": now + 900}})
    trace = tmp_path / "trace"
    write_trail(trace, SID_C, [{"ts": now - 500, "event": "SessionStart", "tmux_session": "old", "cwd": str(tmp_path)}])
    write_trail(trace, SID_D, [{"ts": now - 400, "event": "SessionStart", "tmux_session": "cleared", "cwd": str(tmp_path)},
                               {"ts": now - 300, "event": "SessionEnd", "reason": "clear"}])
    tmux = FakeTmux(panes=[pane("a", state="working", since=str(now - 5)),
                           pane("b", pane_id="%2", window_id="@2", state="idle", since=str(now - 5)),
                           pane("shell", pane_id="%3", window_id="@3", cmd="zsh")])
    snap = ta.snapshot(tmux, reg, None, now=now, status_dir=status, trace_dir=trace)
    by = {s["name"]: s for s in snap["sessions"]}
    assert by["a"]["ctx_pct"] == 42 and by["a"]["model"] == "Opus 5" and by["a"]["cost_usd"] == 1.23
    assert by["a"]["claude_session_id"] == SID_A and by["a"]["quota"]["five_hour"]["used_pct"] == 35
    assert by["shell"]["ctx_pct"] is None and by["shell"]["model"] == "" and by["shell"]["claude_session_id"] == ""
    assert snap["quota"]["five_hour"]["used_pct"] == 70                  # the tightest window of all sessions
    assert [t["session_id"] for t in snap["tombstones"]] == [SID_C]      # superseded is hidden by default
    assert [t["ended"] for t in ta.snapshot(tmux, reg, None, now=now, status_dir=status, trace_dir=trace,
                                            superseded=True)["tombstones"]] == ["killed", "superseded"]
    plain = ta.snapshot(tmux, reg, None, now=now)
    assert plain["tombstones"] == [] and plain["quota"] is None and plain["sessions"][0]["ctx_pct"] is None


def test_snapshot_context_comes_from_the_most_urgent_claude_pane(tmp_path):
    now = 10_000
    reg = _registry_dir(tmp_path, [("a:@1.%1", SID_A), ("a:@2.%2", SID_B)])
    status = tmp_path / "status"
    write_status(status, SID_A, ts=now, context_pct=10, model="idle one")
    write_status(status, SID_B, ts=now, context_pct=90, model="waiting one")
    tmux = FakeTmux(panes=[pane("a", state="idle", since=str(now)),
                           pane("a", pane_id="%2", window_id="@2", state="waiting", since=str(now), active=False)])
    s = ta.snapshot(tmux, reg, None, now=now, status_dir=status)["sessions"][0]
    assert s["state"] == "waiting" and s["ctx_pct"] == 90 and s["claude_session_id"] == SID_B


# ---------- phase 4: resolve / resume / forget ----------
def test_resolve_session_exact_then_unique_substring():
    rows = [{"session": "api", "project": "api-svc", "title": "✳ Phase 2"},
            {"session": "notes", "project": "notes", "title": "✳ groceries"},
            {"session": "api-old", "project": "api-v1", "title": "✳ Phase 1"}]
    assert ta.resolve_session(rows, "api").data["session"] == "api"          # exact beats the substring match
    assert ta.resolve_session(rows, "groceries").data["session"] == "notes"
    assert ta.resolve_session(rows, "PHASE 2").data["session"] == "api"
    ambiguous = ta.resolve_session(rows, "phase")
    assert ambiguous.ok is False and ambiguous.message == "ambiguous: api, api-old"
    assert ta.resolve_session(rows, "zzz").ok is False and ta.resolve_session(rows, "").ok is False
    assert ta.resolve_session([{"name": "only", "project": "", "title": ""}], "onl").data["session"] == "only"


def test_resolve_session_searches_every_pane_of_a_session():
    """Claude often sits in the second pane: the title of a non-leading pane must still be searchable."""
    rows = [{"name": "api", "project": "api-svc", "title": "shell"},
            {"name": "api", "project": "api-svc", "title": "✳ Phase 2 wiring"},
            {"name": "notes", "project": "notes", "title": "✳ groceries"}]
    assert ta.resolve_session(rows, "wiring").data["session"] == "api"
    assert ta.resolve_session(rows, "api").data["session"] == "api"       # one hit, not "ambiguous: api, api"
    assert ta.resolve_session(rows, "notes").data["session"] == "notes"


def _trail(tmp_path, **over):
    trail = {"session_id": SID_A, "tmux_session": "alpha", "pane": "%0", "cwd": str(tmp_path), "color": "#bf5af2",
             "started": 1, "last_ts": 2, "last_event": "Stop", "end_reason": "", "last_message": ""}
    return {**trail, **over}


def test_action_resume_creates_a_session_and_types_the_command(tmp_path):
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    tmux = FakeTmux(panes=[pane("alpha"), pane("alpha-2", pane_id="%2", window_id="@2")])
    r = ta.action_resume(tmux, cfg, _trail(tmp_path), {})
    assert r.ok and r.data["name"] == "alpha-3" and r.data["typed"] == f"claude --resume {SID_A}"
    assert ("new", "alpha-3", str(tmp_path)) in tmux.calls and tmux.has_session("alpha-3")
    assert ("set", "alpha-3", "@tm_color", "#bf5af2") in tmux.calls
    assert ("text", r.data["pane_id"], r.data["typed"]) in tmux.calls
    assert ("enter", r.data["pane_id"]) not in tmux.calls        # resume_mode "type": the user presses Enter


def test_action_resume_falls_back_to_home_and_can_press_enter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    cfg["claude"]["resume_mode"] = "run"
    tmux = FakeTmux(panes=[])
    r = ta.action_resume(tmux, cfg, _trail(tmp_path, cwd="/gone/for/good", color="", tmux_session="beta"), {})
    assert r.ok and r.data["name"] == "beta" and ("new", "beta", str(tmp_path)) in tmux.calls
    assert str(tmp_path) in r.message and ("enter", r.data["pane_id"]) in tmux.calls
    assert not any(c[0] == "set" for c in tmux.calls)            # no color in the trail: nothing to set


def test_action_resume_errors(tmp_path):
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    assert ta.action_resume(FakeTmux(), cfg, _trail(tmp_path, session_id="not-a-uuid"), {}).ok is False
    assert ta.action_resume(FakeTmux(), cfg, _trail(tmp_path, tmux_session=""), {}).data["name"] == "claude"
    failing = FakeTmux()
    failing.fail.add("new")
    assert "could not create" in ta.action_resume(failing, cfg, _trail(tmp_path), {}).message
    mute = FakeTmux()
    mute.fail.add("text")
    assert "could not type" in ta.action_resume(mute, cfg, _trail(tmp_path), {}).message


def test_action_forget_removes_only_valid_trail_files(tmp_path):
    trace = tmp_path / "trace"
    path = write_trail(trace, SID_A, [{"ts": 1, "event": "SessionStart"}])
    assert ta.action_forget(trace, "../../etc/passwd").ok is False
    assert "no trail" in ta.action_forget(trace, SID_B).message
    assert ta.action_forget(trace, SID_A).ok is True and not path.exists()


def test_find_trail_by_id_prefix_or_tmux_name():
    trails = [{"session_id": SID_A, "tmux_session": "alpha"}, {"session_id": SID_B, "tmux_session": "beta"}]
    assert ta.find_trail(trails, SID_B)["tmux_session"] == "beta"
    assert ta.find_trail(trails, "beta")["session_id"] == SID_B
    assert ta.find_trail(trails, SID_A[:8])["session_id"] == SID_A
    assert ta.find_trail(trails, "nope") is None and ta.find_trail([], SID_A) is None


def test_find_trail_picks_the_newest_of_several_trails_with_one_name():
    """A tmux name gets reused: resuming must follow recency, not whichever uuid sorts first."""
    old = {"session_id": SID_A, "tmux_session": "alpha", "last_ts": 100}
    new = {"session_id": SID_C, "tmux_session": "alpha", "last_ts": 900}
    assert ta.find_trail([old, new], "alpha")["session_id"] == SID_C
    assert ta.find_trail([new, old], "alpha")["session_id"] == SID_C
    # the same fixture with the ids swapped: the answer follows last_ts, never the id
    old_id, new_id = {**old, "session_id": SID_C}, {**new, "session_id": SID_A}
    assert ta.find_trail([old_id, new_id], "alpha")["session_id"] == SID_A
    assert [t["session_id"] for t in ta.trails_named([old, new], "alpha")] == [SID_C, SID_A]
    assert ta.trails_named([old, new], SID_A) == [] and ta.trails_named([old, new], "") == []
    assert ta.find_trail([old, new], SID_A)["session_id"] == SID_A        # a full id still wins


def test_action_resume_reports_how_many_trails_shared_the_name(tmp_path):
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    r = ta.action_resume(FakeTmux(), cfg, _trail(tmp_path), {}, matches=3)
    assert r.ok and r.data["matches"] == 3 and "newest of 3; pass the session id" in r.message
    one = ta.action_resume(FakeTmux(), cfg, _trail(tmp_path), {}, matches=1)
    assert "newest of" not in one.message


def test_action_resume_refuses_a_conversation_that_is_still_running(tmp_path):
    """Two claude processes on one transcript corrupt it: the live registry is the last line of defence."""
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    tmux = FakeTmux()
    registry = {"alpha:@1.%1": {"sessionId": SID_A, "alive": True}}
    r = ta.action_resume(tmux, cfg, _trail(tmp_path), registry)
    assert r.ok is False and "still running" in r.message and r.data["alive"] is True
    assert tmux.calls == []
    dead = {"alpha:@1.%1": {"sessionId": SID_A, "alive": False}}
    assert ta.action_resume(tmux, cfg, _trail(tmp_path), dead).ok is True


def test_resume_refuses_when_every_name_is_taken(tmp_path, monkeypatch):
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    tmux = FakeTmux()
    monkeypatch.setattr(tmux, "has_session", lambda name: True)
    r = ta.action_resume(tmux, cfg, _trail(tmp_path), {})
    assert r.ok is False and "no free tmux name" in r.message and "alpha" in r.message
    assert not any(c[0] == "new" for c in tmux.calls)


def test_action_forget_also_removes_the_status_leftover(tmp_path):
    trace, status = tmp_path / "trace", tmp_path / "status"
    trail = write_trail(trace, SID_A, [{"ts": 1, "event": "SessionStart"}])
    leftover = write_status(status, SID_A)
    other = write_status(status, SID_B)
    r = ta.action_forget(trace, SID_A, status)
    assert r.ok and r.data["status"] is True and not trail.exists() and not leftover.exists() and other.exists()
    # no trail left but a status file still there: forgetting must still clean up and say so
    orphan = write_status(status, SID_B)
    r2 = ta.action_forget(trace, SID_B, status)
    assert r2.ok and not orphan.exists() and "dropped its status file" in r2.message
    assert ta.action_forget(trace, SID_C, status).ok is False        # nothing at all to forget


def test_action_new_types_the_configured_claude_command():
    tmux = FakeTmux()
    assert ta.action_new(tmux, "p1", start_claude=True, claude_cmd="claude --model opus").ok
    assert ("send", "=p1:", "claude --model opus") in tmux.calls
