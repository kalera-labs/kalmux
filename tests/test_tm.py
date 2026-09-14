"""Unit + CLI tests for the kalmux command layer (core parsing/merging/rendering and the commands)."""
import io
import json
import os
import subprocess
import sys

import pytest

from conftest import BIN
from fakes import (
    FakeIt2,
    FakeTmux,
    it2_json,
    it2_json_row,
    it2_row,
    pane,
    tmux_line,
    write_status,
    write_trail,
)
from kalmux import tmconfig, tmcore, tmsetup


# ---------- colors ----------
def test_parse_color_names_and_hex(tm):
    assert tmcore.parse_color("orange") == "#ff9500"
    assert tmcore.parse_color("#abc") == "#aabbcc"
    assert tmcore.parse_color("#00AAFF") == "#00aaff"
    with pytest.raises(ValueError):
        tmcore.parse_color("notacolor")


def test_osc6_sequences(tm):
    seqs = tmcore.osc6_tab_color("#ff8800")
    assert seqs == "\x1b]6;1;bg;red;brightness;255\x07\x1b]6;1;bg;green;brightness;136\x07\x1b]6;1;bg;blue;brightness;0\x07"
    assert tmcore.osc6_tab_color(None) == "\x1b]6;1;bg;*;default\x07"


# ---------- tmux parsing ----------
def test_parse_panes_from_tmux_format(tm):
    p = pane("api", pane_id="%0", window_id="@0", state="working", detail="Edit", since="1789290000", title="✳ Phase 2\twith tab",
             path="/Volumes/Dev/api-svc", color="#ff9500", tty="/dev/ttys004")
    rows = tmcore.parse_panes(tmux_line(p) + "\n")
    assert rows[0]["session"] == "api" and rows[0]["pane_id"] == "%0" and rows[0]["cc_state"] == "working"
    assert rows[0]["tm_color"] == "#ff9500" and rows[0]["path"].endswith("api-svc") and "\t" in rows[0]["title"]
    assert rows[0]["tty"] == "/dev/ttys004" and rows[0]["pane_active"] == "1"
    assert tmcore.parse_panes("") == []
    assert tmcore.parse_panes("only" + tmcore.SEP + "three" + tmcore.SEP + "fields\n") == []  # malformed records are dropped


def test_parse_panes_drops_forged_records(tm):
    good = pane("api", pane_id="%3", window_id="@1", tty="/dev/ttys004")
    pts = pane("lin", pane_id="%4", window_id="@2", tty="/dev/pts/3")
    forged = [pane("evil", pane_id="%99", window_id="@9", tty="/tmp/target"),          # tty outside /dev
              pane("evil", pane_id="9", window_id="@9", tty="/dev/ttys001"),            # pane id without %
              pane("evil", pane_id="%9", window_id="w9", tty="/dev/ttys001"),           # window id without @
              pane("evil", pane_id="%9", window_id="@9", tty="/dev/../etc/passwd"),     # path tricks
              {**pane("evil", pane_id="%9", window_id="@9"), "attached": "yes"}]
    text = "\n".join(tmux_line(p) for p in [good, *forged, pts]) + "\n"
    assert [r["session"] for r in tmcore.parse_panes(text)] == ["api", "lin"]


def test_write_tty_only_writes_character_devices(tm, tmp_path, ptty):
    plain = tmp_path / "known_hosts"
    plain.write_text("keep me\n")
    assert tmcore.write_tty(str(plain), "x") is False and plain.read_text() == "keep me\n"
    assert tmcore.write_tty("", "x") is False and tmcore.write_tty("/nonexistent/dir/tty", "x") is False
    tty, read = ptty
    assert tmcore.write_tty(tty, "hello") is True and "hello" in read()


def test_tmux_binary_resolution(tm, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    fallback = tmp_path / "homebrew-bin"
    fallback.mkdir()
    monkeypatch.setattr(tmcore, "TMUX_FALLBACK_DIRS", (str(tmp_path / "nowhere"), str(fallback)))
    assert tmcore.resolve_tmux() == "" and tmcore.Tmux().run_rc("ls")[0] == 127
    (fallback / "tmux").write_text("#!/bin/bash\necho found\n")
    (fallback / "tmux").chmod(0o755)
    t = tmcore.Tmux()
    assert t.path == str(fallback / "tmux") and t.run("anything").strip() == "found"    # AutoLaunch PATH has no Homebrew
    assert tmcore.Tmux(path="/explicit/tmux").path == "/explicit/tmux"


def test_pane_shells_and_foreground_busy(tm):
    """tmux reports the pane's foreground process, so a shell at its prompt reports itself."""
    zsh = {"shell": "/bin/zsh", "start_cmd": ""}
    assert tmcore.pane_shells({**zsh, "cmd": "zsh"}) == {"zsh"}
    assert tmcore.pane_shells({"shell": "/bin/zsh", "start_cmd": "/bin/bash"}) == {"bash"}   # pane opened with a shell
    assert tmcore.pane_shells({"shell": "/bin/zsh", "start_cmd": '"sleep 40"'}) == set()     # pane opened with a command
    # tmux copies default-command into pane_start_command: wrappers around a shell still mean "a prompt"
    for start in ('"exec /bin/zsh"', "reattach-to-user-namespace -l zsh", "cd /tmp && exec zsh", "FOO=1 zsh", "/usr/bin/env zsh"):
        assert tmcore.pane_shells({"shell": "/bin/zsh", "start_cmd": start}) == {"zsh"}, start
        assert tmcore.foreground_busy({"shell": "/bin/zsh", "start_cmd": start, "cmd": "zsh"}) is False, start
    assert tmcore.foreground_busy({**zsh, "cmd": "zsh"}) is False                         # at the prompt
    assert tmcore.foreground_busy({**zsh, "cmd": "bash"}) is True                         # a bash script under zsh
    assert tmcore.foreground_busy({**zsh, "cmd": "Python"}) is True
    assert tmcore.foreground_busy({**zsh, "cmd": ""}) is False
    assert tmcore.foreground_busy({"shell": "/bin/bash", "start_cmd": "/bin/bash", "cmd": "bash"}) is False
    assert tmcore.foreground_busy({"shell": "/bin/zsh", "start_cmd": '"sleep 40"', "cmd": "sleep"}) is True
    assert tmcore.foreground_busy({"shell": "/bin/zsh", "start_cmd": '"sleep 1"', "cmd": "sleep", "dead": "1"}) is False  # remain-on-exit corpse


def test_fields_and_format_stay_in_lockstep(tm):
    """parse_panes drops every record whose field count differs, so a desync empties the CLI and the UI silently."""
    assert len(tmcore.FIELDS) == len(tmcore.TMUX_FMT.split(tmcore.SEP)) == 17
    assert tmcore.parse_panes(tmux_line(pane("x")))[0]["dead"] == "0"


def test_merge_marks_a_pane_running_a_plain_command_as_busy(tm):
    now = 1789290100
    panes = [pane("shellpane", pane_id="%1", cmd="zsh"),
             pane("script", pane_id="%2", cmd="bash"),
             pane("build", pane_id="%3", cmd="make"),
             pane("bashbox", pane_id="%4", cmd="bash", start_cmd="/bin/bash")]
    rows = {r["session"]: r for r in tm.merge(panes, {}, now)}
    assert rows["shellpane"]["state"] == "" and rows["bashbox"]["state"] == ""
    assert rows["script"]["state"] == "busy" and rows["script"]["detail"] == "bash"
    assert rows["build"]["state"] == "busy" and rows["build"]["detail"] == "make"
    assert rows["build"]["claude"] is False and rows["build"]["age"] is None
    # a Claude pane is never "busy", and a leftover hook state still wins over the command
    claude = tm.merge([pane("cc", cmd="2.1.270")], {}, now)[0]
    assert claude["state"] == "unknown" and claude["claude"] is True
    stale = tm.merge([pane("left", cmd="make", state="working", since=str(now - 5))], {}, now)[0]
    assert stale["state"] == "stale"


def test_busy_sorts_below_claude_states_and_above_nothing(tm):
    rows = [{"state": s, "session": s} for s in ("", "busy", "idle", "gone", "stale", "waiting")]
    assert [r["state"] for r in tm.sort_rows(rows)] == ["waiting", "idle", "stale", "gone", "busy", ""]


def test_valid_session_name(tm):
    assert tmcore.valid_session_name("api") and tmcore.valid_session_name("web-api_2")
    for bad in ("", "a b", "a.b", "a:b", "-x", "x" * 65, "ü"):
        assert not tmcore.valid_session_name(bad)
    with pytest.raises(ValueError):
        tmcore.cc_tab_command("bad name")
    # the target stays quoted: a bare =ok is zsh EQUALS expansion ("ok not found"), and the tab never attaches
    assert tmcore.cc_tab_command("ok") == """/bin/zsh -lc 'exec tmux -CC attach -t "=ok"'"""


# ---------- registry ----------
def test_load_registry_maps_tmux_pane_and_skips_bad(tm, tmp_path):
    good = {"pid": 1, "sessionId": "sid-1", "cwd": "/Volumes/Dev/api-svc", "tmux": "api:@0.%0", "status": "busy",
            "name": "api-4b", "statusUpdatedAt": 1789290000000}
    (tmp_path / "1.json").write_text(json.dumps(good))
    (tmp_path / "2.json").write_text("{broken")
    (tmp_path / "3.json").write_text(json.dumps({"pid": 3, "sessionId": "sid-3"}))  # no tmux field
    reg = tm.load_registry(tmp_path, alive=lambda pid: pid == 1)
    assert "api:@0.%0" in reg and reg["api:@0.%0"]["status"] == "busy" and reg["api:@0.%0"]["alive"] is True
    assert len(reg) == 1


def test_load_registry_marks_dead_pids(tm, tmp_path):
    (tmp_path / "9.json").write_text(json.dumps({"pid": 9, "sessionId": "s", "tmux": "x:@1.%1", "status": "idle"}))
    reg = tm.load_registry(tmp_path, alive=lambda pid: False)
    assert reg["x:@1.%1"]["alive"] is False


def test_load_registry_missing_dir(tm, tmp_path):
    assert tm.load_registry(tmp_path / "nope", alive=lambda pid: True) == {}


def test_load_registry_ignores_non_object_json(tm, tmp_path):
    (tmp_path / "1.json").write_text("null")
    (tmp_path / "2.json").write_text("[1,2]")
    (tmp_path / "3.json").write_text(json.dumps({"pid": "notint", "sessionId": "s", "tmux": "a:@1.%1", "status": "busy"}))
    reg = tm.load_registry(tmp_path, alive=lambda pid: True)
    assert list(reg) == ["a:@1.%1"] and reg["a:@1.%1"]["alive"] is False


def test_load_registry_prefers_alive_then_newer(tm, tmp_path):
    (tmp_path / "10.json").write_text(json.dumps({"pid": 10, "sessionId": "old-dead", "tmux": "k:@1.%1", "status": "busy", "statusUpdatedAt": 900}))
    (tmp_path / "5.json").write_text(json.dumps({"pid": 5, "sessionId": "live", "tmux": "k:@1.%1", "status": "idle", "statusUpdatedAt": 100}))
    reg = tm.load_registry(tmp_path, alive=lambda pid: pid == 5)
    assert reg["k:@1.%1"]["sessionId"] == "live"
    (tmp_path / "7.json").write_text(json.dumps({"pid": 7, "sessionId": "live-newer", "tmux": "k:@1.%1", "status": "busy", "statusUpdatedAt": 500}))
    reg = tm.load_registry(tmp_path, alive=lambda pid: pid in (5, 7))
    assert reg["k:@1.%1"]["sessionId"] == "live-newer"


def test_pid_alive(tm):
    assert tmcore.pid_alive(os.getpid()) is True
    assert tmcore.pid_alive(2 ** 22 - 1) in (True, False)
    for bad in (0, -1, True, 2 ** 40, "x", None):
        assert tmcore.pid_alive(bad) is False


# ---------- merge / sort / render ----------
def test_merge_prefers_hook_state_then_registry(tm):
    now = 1789290100
    panes = [pane("a", state="waiting", detail="Allow Bash?", since="1789290090"),
             pane("b", pane_id="%2", window_id="@2"),
             pane("c", pane_id="%3", window_id="@3", cmd="zsh")]
    reg = {"b:@2.%2": {"status": "busy", "name": "b-1", "sessionId": "sid-b", "alive": True, "cwd": "/x"}}
    rows = tm.merge(panes, reg, now=now)
    by = {r["session"]: r for r in rows}
    assert by["a"]["state"] == "waiting" and by["a"]["detail"] == "Allow Bash?" and by["a"]["age"] == 10
    assert by["b"]["state"] == "working" and by["b"]["source"] == "registry" and by["b"]["claude_session_id"] == "sid-b"
    assert by["c"]["state"] == "" and by["c"]["claude"] is False and by["c"]["active"] is True


def test_merge_treats_claude_process_without_state_as_unknown(tm):
    rows = tm.merge([pane("a", cmd="2.1.270")], {}, now=0)
    assert rows[0]["claude"] is True and rows[0]["state"] == "unknown"


def test_merge_flags_stale_gone_and_orphaned_state(tm):
    now = 100_000
    rows = tm.merge([
        pane("old", state="working", since=str(now - 3 * 3600)),               # hook state, very old
        pane("dead", pane_id="%2", window_id="@2", state="working", since=str(now - 5)),
        pane("orphan", pane_id="%3", window_id="@3", state="working", since=str(now - 5), cmd="zsh"),
    ], {"dead:@2.%2": {"status": "busy", "alive": False, "sessionId": "d", "name": "", "cwd": ""}}, now=now)
    by = {r["session"]: r for r in rows}
    assert by["old"]["state"] == "working" and by["old"]["stale"] is True
    assert by["dead"]["state"] == "gone"
    assert by["orphan"]["state"] == "stale"
    out = tm.render_table(rows)
    assert "working?" in out and "gone" in out


def test_sort_rows_priority(tm):
    rows = [{"session": "i", "state": "idle"}, {"session": "n", "state": ""}, {"session": "w", "state": "working"},
            {"session": "q", "state": "waiting"}, {"session": "u", "state": "unknown"}]
    assert [r["session"] for r in tm.sort_rows(rows)] == ["q", "w", "u", "i", "n"]


def test_fmt_age(tm):
    assert tmcore.fmt_age(5) == "5s" and tmcore.fmt_age(65) == "1m" and tmcore.fmt_age(3700) == "1h01m" and tmcore.fmt_age(None) == ""


def test_render_table_contains_key_columns(tm):
    rows = tm.merge([pane("api", state="working", detail="Edit", since="0", title="✳ Phase 2 — wiring the índex", color="#ff9500", path="/Volumes/Dev/api-svc")], {}, now=60)
    out = tm.render_table(rows)
    assert "api" in out and "working" in out and "api-svc" in out and "Phase 2" in out and "#ff9500" in out


def test_render_marks_registry_sourced_state(tm):
    reg = {"b:@2.%2": {"status": "busy", "name": "b-1", "sessionId": "sid-b", "alive": True, "cwd": "/x", "statusUpdatedAt": 0}}
    rows = tm.merge([pane("b", pane_id="%2", window_id="@2")], reg, now=100)
    assert "working~" in tm.render_table(rows)


def test_junk_tm_color_never_reaches_the_swatch(tm):
    panes = [pane("a", color="blue"), pane("b", pane_id="%2", color="#ab"), pane("c", pane_id="%3", color="#0a84ff")]
    rows = tm.merge(panes, {}, 100)
    assert [r["color"] for r in rows] == ["", "", "#0a84ff"]       # only #rrggbb leaves the core (CSS var, swatch)
    out = tm.render_table(rows, color=True, columns=120)
    assert "48;2;10;132;255" in out and "\033[48" in out
    rows[0]["color"] = "blue"                                       # belt and braces: the renderer guards too
    assert "blue" in tm.render_table(rows, color=True, columns=120)


def test_render_table_strips_control_chars(tm):
    rows = tm.merge([pane("evil", title="✳ ok\x1b]52;c;aGk=\x07 bad", detail="x\x1by")], {}, now=0)
    out = tm.render_table(rows)
    assert "\x1b" not in out and "\x07" not in out and "ok" in out


def test_attach_command_picks_the_right_verb(tm):
    assert tm.attach_command("x", {"TMUX": "/tmp/tmux-1/default,1,0"}) == ["tmux", "switch-client", "-t", "x"]
    assert tm.attach_command("x", {"TERM_PROGRAM": "iTerm.app"}) == ["tmux", "-CC", "attach", "-t", "x"]
    assert tm.attach_command("x", {"LC_TERMINAL": "iTerm2"}) == ["tmux", "-CC", "attach", "-t", "x"]
    assert tm.attach_command("x", {}) == ["tmux", "attach", "-t", "x"]


def test_write_tty_failure_returns_false(tm):
    assert tmcore.write_tty("/nonexistent/dir/tty", "x") is False


# ---------- commands ----------
def test_cmd_ls_json(tm, capsys):
    t = FakeTmux(panes=[pane("api", state="idle", since="0")])
    rc = tm.cmd_ls(t, registry={}, json_out=True, now=10)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["session"] == "api" and data[0]["state"] == "idle" and data[0]["age"] == 10


def test_cmd_new_creates_detached_session_and_sets_color(tm, capsys, monkeypatch):
    monkeypatch.setattr(tm.os, "environ", {"TERM_PROGRAM": "iTerm.app"})
    t = FakeTmux(panes=[])
    rc = tm.cmd_new(t, "proj-x", cwd="/tmp", color="teal", attach=False, start_claude=True)
    assert rc == 0
    assert ("new", "proj-x", "/tmp") in t.calls and ("send", "=proj-x:", "claude") in t.calls
    assert ("set", "proj-x", "@tm_color", tmcore.parse_color("teal")) in t.calls
    assert "tmux -CC attach -t proj-x" in capsys.readouterr().out


def test_cmd_new_refuses_duplicate_and_reports_failures(tm, capsys):
    t = FakeTmux(panes=[pane("proj-x")])
    assert tm.cmd_new(t, "proj-x", cwd=None, color=None, attach=False) == 1
    t.fail.add("new")
    assert tm.cmd_new(t, "p3", cwd=None, color=None, attach=False) == 1
    assert "could not create" in capsys.readouterr().err


def test_cmd_new_prints_context_appropriate_hint(tm, capsys, monkeypatch):
    t = FakeTmux(panes=[])
    monkeypatch.setattr(tm.os, "environ", {"TMUX": "/tmp/x"})
    assert tm.cmd_new(t, "p2", cwd=None, color=None, attach=True) == 0
    assert "tmux switch-client -t p2" in capsys.readouterr().out


def test_cmd_attach_without_tty_prints_command(tm, capsys, monkeypatch):
    t = FakeTmux(panes=[pane("api")])
    monkeypatch.setattr(tm.os, "environ", {})
    assert tm.cmd_attach(t, "api", do_exec=False) == 0
    assert "tmux attach -t api" in capsys.readouterr().out
    assert tm.cmd_attach(t, "nope", do_exec=False) == 1


def test_cmd_open_reapplies_after_opening(tm, ptty, capsys):
    tty, read = ptty
    t = FakeTmux(panes=[pane("api", state="working", tty=tty, color="#ff9500")])
    it2 = FakeIt2(window="pty-CUR")
    assert tm.cmd_open(t, it2, "api", wait=0) == 0
    assert it2.tabs[0][1] == "pty-CUR" and "status=working" in read()
    assert tm.cmd_open(t, it2, "nope", wait=0) == 1


def test_main_dispatch_and_help(tm, capsys):
    assert tm.main(["--help"]) == 0
    assert "kalmux ls" in capsys.readouterr().out
    assert tm.main(["bogus"]) == 2
    assert tm.main([]) == 0
    assert tm.main(["ui", "url"]) == 0
    assert capsys.readouterr().out.strip().endswith(f"http://127.0.0.1:{tm.UI_PORT}/")


# ---------- real subprocess wrappers (fake binaries on PATH) ----------
@pytest.fixture
def fake_bins(tmp_path, monkeypatch, ptty):
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "log"
    tty, read = ptty
    record = tmux_line(pane("api", pane_id="%0", window_id="@0", state="working", detail="Edit", since="5", title="✳ T",
                            path="/Volumes/Dev/api-svc", color="#ff9500", tty=tty))
    (tmp_path / "record").write_text(record + "\n")
    (b / "tmux").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {log}\n'
        'case "$1" in\n'
        f"  list-panes) cat {tmp_path / 'record'};;\n"
        "  list-sessions) printf '$0\\037api\\n$1\\037api-old\\n';;\n"
        '  new-session) echo "%42";;\n'

        f'  display) echo {tty};;\n'
        '  show) echo "#ff9500";;\n'
        '  has-session) [ "$3" = "=api" ] && exit 0 || exit 1;;\n'
        "esac\nexit 0\n")
    (tmp_path / "sessions.json").write_text(it2_json([it2_json_row("G-1", "tmux (tmux)", is_tmux=False, tty="/dev/ttys001"),
                                                        it2_json_row("G-2", "✳ T (claude)", tab="9")]) + "\n")
    (b / "it2").write_text(
        "#!/bin/bash\n"
        f'echo "it2 $*" >> {log}\n'
        'case "$1 $2" in\n'

        f"  'session list') cat {tmp_path / 'sessions.json'};;\n"
        '  "session get-var") [ "$5" = "G-2" ] && echo 0 || echo "Variable not set";;\n'
        '  "app get-focus") echo "Current window: pty-FAKE";;\n'
        '  "window list") printf "pty-FAKE\t4 tabs\t(71, 53)\t1210x922\npty-OTHER\t1 tabs\t(0, 0)\t100x100\n";;\n'
        '  "window new") echo "Created new window: pty-NEW";;\n'
        '  "tab new") echo "Created new tab: 3";;\n'
        "esac\nexit 0\n")
    for f in (b / "tmux", b / "it2"):
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(tmcore, "IT2_BUNDLED", "/nonexistent/it2")
    return {"log": log, "tty": tty, "read": read, "bin": b}


def test_real_tmux_wrapper_roundtrip(tm, fake_bins):
    t = tm.Tmux()
    panes = t.list_panes()
    assert panes[0]["session"] == "api" and panes[0]["cc_state"] == "working" and panes[0]["tty"] == fake_bins["tty"]
    assert t.show_session_option("api", "@tm_color") == "#ff9500"
    t.set_session_option("api", "@tm_color", "#00ff00")
    t.set_session_option("api", "@tm_color", None)
    assert t.pane_tty("%0") == fake_bins["tty"] and t.path == str(fake_bins["bin"] / "tmux")
    t.new_session("newsess", "/tmp")
    assert t.has_session("api") is True and t.has_session("zzz") is False
    assert t.kill_session("api") and t.rename_session("api", "g") and t.detach_session("api") and t.send_keys("=api:", "claude")
    assert t.send_keys("%42", "--dangerously-skip-permissions")
    assert t.session_id("api") == "$0" and t.session_id("nope") == ""
    assert t.new_session_pane("np", "/tmp") == "%42" and t.send_text("%42", "claude --resume x") and t.press_enter("%42")
    log = fake_bins["log"].read_text()
    assert "new-session -d -s np -c /tmp -P -F #{pane_id}" in log      # a pane id, never a name, for send-keys
    assert "send-keys -t %42 -l -- claude --resume x" in log and "send-keys -t %42 Enter" in log
    # by session id: `set -t =api` is rejected by tmux 3.6a ("no such session"), and a bare name prefix-matches
    assert "set -t $0 @tm_color #00ff00" in log and "set -t $0 -u @tm_color" in log and "set -t =api" not in log
    assert "new-session -d -s newsess -c /tmp" in log and "kill-session -t =api" in log
    assert "rename-session -t =api g" in log and "detach-client -s =api" in log
    # send_keys types literally and presses Enter separately: `send-keys -t X --dangerously-... Enter`
    # would be read as tmux flags, so a configured claude.new starting with "-" never reached the shell
    assert "send-keys -t =api: -l -- claude" in log and "send-keys -t =api: Enter" in log
    assert "send-keys -t %42 -l -- --dangerously-skip-permissions" in log


def test_real_it2_wrapper(tm, fake_bins):
    it2 = tm.It2()
    assert it2.path == str(fake_bins["bin"] / "it2")
    assert it2.available() == (os.uname().sysname == "Darwin")
    rows = it2.list_sessions()
    assert rows[1]["guid"] == "G-2" and rows[1]["name"].startswith("✳ T") and rows[1]["is_tmux"] is True and rows[1]["tab"] == "9"
    assert rows[0]["is_tmux"] is False and rows[0]["tty"] == "/dev/ttys001"
    assert it2.get_var("G-2", "tmuxWindowPane") == "0" and it2.get_var("G-1", "tmuxWindowPane") == ""
    assert it2.current_window() == "pty-FAKE"
    assert it2.list_windows() == ["pty-FAKE", "pty-OTHER"] and it2.new_window("cmd") == (True, "Created new window: pty-NEW")
    assert it2.new_tab("cmd", "pty-FAKE") == (True, "Created new tab: 3")
    it2.focus("G-2")
    it2.activate()
    log = fake_bins["log"].read_text()
    assert "it2 session focus G-2" in log and "it2 tab new --window pty-FAKE --command cmd" in log and "it2 app activate" in log
    assert it2.last_rc == 0


def test_it2_tsv_fallback_and_missing_binary(tm, tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "it2").write_text('#!/bin/bash\n[ "$3" = "--json" ] && exit 0\nprintf "G-1\\ttmux (tmux)\\ttmux (tmux)\\t1x1\\t/dev/ttys001\\n"\nexit 0\n')
    (b / "it2").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(tmcore, "IT2_BUNDLED", "/nonexistent/it2")
    rows = tm.It2().list_sessions()
    assert rows == [{"guid": "G-1", "name": "tmux (tmux)", "title": "tmux (tmux)", "window": "", "tab": "", "tty": "/dev/ttys001", "is_tmux": None}]
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    it2 = tm.It2()
    assert it2.path is None and it2.available() is False and it2.list_sessions() == [] and it2.focus("x") is False


def test_main_with_fake_bins(tm, fake_bins, monkeypatch, capsys):
    monkeypatch.setattr(tm, "DEFAULT_REGISTRY", fake_bins["bin"] / "no-registry")
    monkeypatch.setattr(tm.time, "sleep", lambda _s: None)
    assert tm.main(["ls"]) == 0
    assert "api" in capsys.readouterr().out
    assert tm.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session"] == "api"
    assert tm.main(["color", "api", "none"]) == 0
    assert "default" in fake_bins["read"]()
    assert tm.main(["color", "api", "zzz"]) == 1
    assert tm.main(["kill", "api"]) == 0 and tm.main(["detach", "api"]) == 0 and tm.main(["rename", "api", "g2"]) == 0
    assert tm.main(["reapply"]) == 0 and "status=working" in fake_bins["read"]()
    if os.uname().sysname == "Darwin":
        assert tm.main(["go", "api"]) == 0
        assert "it2 session focus G-2" in fake_bins["log"].read_text()
        assert tm.main(["open", "api"]) == 0
        assert 'it2 tab new --window pty-FAKE --command /bin/zsh -lc \'exec tmux -CC attach -t "=api"\'' in fake_bins["log"].read_text()
    assert tm.main(["new", "brand-new", "--cwd", "/tmp", "--color", "blue", "--no-attach"]) == 0
    assert tm.main(["new", "api", "--no-attach"]) == 1
    assert tm.main(["rename", "api", "bad.name"]) == 1


def test_it2_records_failures_and_timeouts(tm, tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "it2").write_text("#!/bin/bash\necho boom >&2\nexit 2\n")
    (b / "it2").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(tmcore, "IT2_BUNDLED", "/nonexistent/it2")
    it2 = tm.It2()
    rc, out = it2._run_rc("app", "activate")
    assert rc == 2 and "boom" in out and it2.last_rc == 2 and it2.list_sessions() == [] and it2.current_window() == ""
    monkeypatch.setattr(tmcore, "IT2_TIMEOUT", 0.2)
    (b / "it2").write_text("#!/bin/bash\nsleep 2\n")
    assert it2._run_rc("app", "activate") == (127, "") and it2.last_rc == 127


def test_tmux_run_tolerates_invalid_utf8(tm, tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "tmux").write_bytes(b"#!/bin/bash\nprintf 'ok\\xff\\n'\nexit 0\n")
    (b / "tmux").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    assert tm.Tmux().run("anything").startswith("ok")


# ---------- doctor / setup / ui plumbing ----------
def test_cmd_doctor_runs(tm, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tm, "DEFAULT_SETTINGS", tmp_path / "missing.json")
    monkeypatch.setattr(tm, "DEFAULT_HOOK_LINK", tmp_path / "missing-link")
    monkeypatch.setattr(tm, "DEFAULT_TMUX_CONF", tmp_path / "missing.conf")
    monkeypatch.setattr(tm, "health", lambda port: None)
    monkeypatch.setattr(tmsetup, "autolaunch_state", lambda: "missing")
    rc = tm.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1 and "✗" in out and "allow-passthrough" in out


def test_cmd_setup_dry_run_and_failure(tm, monkeypatch, capsys):
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **_kw: [("step", lambda: None)])
    monkeypatch.setattr(tm, "cmd_doctor", lambda ui=True, statusline=True: 0)
    monkeypatch.setattr(tm, "health", lambda port: {"pid": 1})
    assert tm.cmd_setup(dry_run=True) == 0 and "would: step" in capsys.readouterr().out
    assert tm.cmd_setup() == 0
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **_kw: [("step", lambda: (_ for _ in ()).throw(OSError("x")))])
    assert tm.cmd_setup() == 1


def test_setup_passes_no_statusline_through(tm, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **kw: seen.append(kw) or [])
    assert tm.main(["setup", "--dry-run", "--no-statusline", "--no-ui"]) == 0
    assert seen == [{"ui": False, "statusline": False}]


def test_ui_status_start_stop_paths(tm, monkeypatch, tmp_path, capsys):
    killed = []
    monkeypatch.setattr(tm.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(tm, "health", lambda port: None if killed else {"pid": 7})
    monkeypatch.setattr(tmsetup, "autolaunch_state", lambda: "ours")
    monkeypatch.setattr(tmsetup, "toolbelt_registered", lambda: True)
    monkeypatch.setattr(tm, "PIDFILE", tmp_path / "ui.pid")
    assert tm.main(["ui", "status"]) == 0
    out = capsys.readouterr().out
    assert "running (pid 7)" in out and "ours" in out and "registered" in out
    assert tm.main(["ui", "start"]) == 0 and "already running" in capsys.readouterr().out
    # stop trusts /healthz for the pid, never a bare pidfile
    (tmp_path / "ui.pid").write_text("424242")
    assert tm.main(["ui", "stop"]) == 0 and "SIGTERM to pid 7" in capsys.readouterr().out
    assert killed == [(7, tm.signal.SIGTERM)] and not (tmp_path / "ui.pid").exists()
    assert tm.main(["ui", "status"]) == 1
    # no server: a stale pidfile naming a dead or foreign pid is dropped, nothing is signalled
    for stale in ("999999", str(os.getpid())):          # gone / alive-but-not-`tm ui serve` (this pytest process)
        (tmp_path / "ui.pid").write_text(stale)
        assert tm.main(["ui", "stop"]) == 0 and "nothing to stop" in capsys.readouterr().out
        assert len(killed) == 1 and not (tmp_path / "ui.pid").exists()
    # no server answering but the pidfile's process really is `tm ui serve` (hung): signal it
    monkeypatch.setattr(tm, "_is_tm_serve", lambda pid: pid == 4242)
    (tmp_path / "ui.pid").write_text("4242")
    assert tm.main(["ui", "stop"]) == 0 and killed[-1] == (4242, tm.signal.SIGTERM)
    # a server started before a rename left its pidfile in an old state dir: still found, still stopped
    legacy = (tmp_path / "kmux" / "ui.pid", tmp_path / "tm" / "ui.pid")
    monkeypatch.setattr(tm, "LEGACY_PIDFILES", legacy)
    for i, pidfile in enumerate(legacy, start=3):
        pidfile.parent.mkdir()
        pidfile.write_text("4242")
        assert tm.main(["ui", "stop"]) == 0 and killed[-1] == (4242, tm.signal.SIGTERM) and len(killed) == i
        assert not pidfile.exists()
    monkeypatch.setattr(tmsetup, "start_server", lambda path: True)
    assert tm.main(["ui", "start"]) == 0 and "running on" in capsys.readouterr().out
    assert tm.main(["ui", "restart"]) == 0 and "running on" in capsys.readouterr().out
    monkeypatch.setattr(tmsetup, "start_server", lambda path: False)
    assert tm.main(["ui", "start"]) == 1 and "did not come up" in capsys.readouterr().err


def test_is_tm_serve_reads_the_process_command_line(tm):
    assert tm._is_tm_serve(os.getpid()) is False           # this pytest process is not `tm ui serve`
    assert tm._is_tm_serve(999999) is False


# ---------- config + status line ----------
def test_cmd_config_prints_the_effective_configuration(tm, monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[claude]\nresume_mode = "run"\nnew = 7\n')
    monkeypatch.setenv("KALMUX_CONFIG", str(cfg))
    assert tm.main(["config"]) == 0
    out = capsys.readouterr().out
    assert str(cfg) in out and "claude.resume_mode = 'run'" in out and "claude.new = 'claude'" in out
    assert "! claude.new: expected a string" in out


def test_statusline_install_status_and_uninstall_from_the_cli(tm, monkeypatch, tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "bun hud.ts"}}))
    monkeypatch.setattr(tm, "DEFAULT_SETTINGS", settings)
    monkeypatch.setattr(tm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tm, "DEFAULT_TM_LINK", tmp_path / "kalmux")
    assert tm.main(["statusline", "install"]) == 0 and "✓" in capsys.readouterr().out
    assert json.loads(settings.read_text())["statusLine"]["command"] == f"{tmp_path / 'kalmux'} statusline"
    assert tm.main(["statusline", "status"]) == 0
    out = capsys.readouterr().out
    assert "routed: yes" in out and "bun hud.ts" in out and "fresh status files: 0" in out
    assert tm.main(["statusline", "install"]) == 0 and "already" in capsys.readouterr().out
    assert tm.main(["statusline", "uninstall"]) == 0 and "restored" in capsys.readouterr().out
    assert json.loads(settings.read_text())["statusLine"]["command"] == "bun hud.ts"
    assert tm.main(["statusline", "status"]) == 1 and "routed: no" in capsys.readouterr().out
    assert tm.main(["statusline", "uninstall"]) == 1 and "not routed" in capsys.readouterr().err
    settings.write_text("{broken")
    assert tm.main(["statusline", "install"]) == 1 and "✗" in capsys.readouterr().err


def test_statusline_without_a_subcommand_runs_the_tap(tm, monkeypatch, tmp_path, capsys):
    session_id = "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d"
    payload = {"session_id": session_id, "model": {"display_name": "Opus 5"}, "context_window": {"used_percentage": 42}}
    monkeypatch.setattr(tm, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tm.sys, "stdin", type("S", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})())
    assert tm.main(["statusline"]) == 0
    assert capsys.readouterr().out.strip() == "Opus 5 · ctx 42%"
    written = json.loads((tmp_path / "state" / "status" / f"{session_id}.json").read_text())
    assert written["context_pct"] == 42 and written["session_id"] == session_id


def test_ui_show_install_uninstall(tm, monkeypatch, capsys):
    monkeypatch.setattr(tm, "health", lambda port: {"pid": 1})
    monkeypatch.setattr(tmsetup, "register_toolbelt", lambda url, show=True: (True, "registered " + url))
    monkeypatch.setattr(tmsetup, "install_autolaunch", lambda: None)
    monkeypatch.setattr(tmsetup, "uninstall_autolaunch", lambda: True)
    assert tm.main(["ui", "show"]) == 0 and "registered" in capsys.readouterr().out
    assert tm.main(["ui", "install"]) == 0 and "AutoLaunch" in capsys.readouterr().out
    monkeypatch.setattr(tm, "PIDFILE", tm.Path("/nonexistent/ui.pid"))
    assert tm.main(["ui", "uninstall"]) == 0 and "AutoLaunch script removed" in capsys.readouterr().out
    monkeypatch.setattr(tmsetup, "install_autolaunch", lambda: (_ for _ in ()).throw(OSError("foreign script")))
    assert tm.main(["ui", "install"]) == 1 and "foreign script" in capsys.readouterr().err
    monkeypatch.setattr(tmsetup, "register_toolbelt", lambda url, show=True: (False, "no cookie"))
    assert tm.main(["ui", "show"]) == 1


# ---------- phase 4: palette ----------
PALETTE_NAMES = ("red", "orange", "yellow", "lime", "green", "mint", "teal", "cyan", "blue", "indigo",
                 "purple", "magenta", "pink", "coral", "brown", "gold", "olive", "slate", "gray", "white")
SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SID_C = "99999999-8888-7777-6666-555555555555"
SID_LIVE = "0f0f0f0f-1111-2222-3333-444444444444"


def test_palette_has_twenty_names_and_keeps_the_old_hex(tm):
    assert [k for k in tmcore.PALETTE if k != "grey"] == list(PALETTE_NAMES)
    assert tmcore.PALETTE["grey"] == tmcore.PALETTE["gray"] == "#8e8e93"      # alias, hidden from the UI
    assert (tmcore.PALETTE["orange"], tmcore.PALETTE["blue"], tmcore.PALETTE["teal"]) == ("#ff9500", "#0a84ff", "#40c8e0")
    assert all(tmcore.HEX_RE.match(v) for v in tmcore.PALETTE.values())
    assert len(set(tmcore.PALETTE.values())) == len(PALETTE_NAMES)            # only gray/grey share a value
    assert tmcore.parse_color("Indigo") == tmcore.PALETTE["indigo"]
    with pytest.raises(ValueError, match="indigo"):
        tmcore.parse_color("notacolor")


# ---------- phase 4: status files (the statusline tap) ----------
def test_load_status_reads_records_and_ignores_junk(tm, tmp_path):
    d = tmp_path / "status"
    write_status(d, SID_A, context_pct=42, model="Opus 5", cost_usd=1.5)
    write_status(d, SID_B, context_pct=7)
    (d / "broken.json").write_text("{not json")
    (d / "list.json").write_text("[1, 2]")
    (d / "anonymous.json").write_text('{"ts": 1}')
    (d / "huge.json").write_text('{"session_id": "huge", "pad": "' + "y" * 70000 + '"}')
    status = tmcore.load_status(d)
    assert set(status) == {SID_A, SID_B}
    assert status[SID_A]["context_pct"] == 42 and status[SID_A]["model"] == "Opus 5"
    assert tmcore.load_status(tmp_path / "missing") == {}


def test_merge_adds_context_model_cost_and_quota(tm):
    now = 1_000_000
    reg = {"a:@1.%1": {"status": "busy", "name": "", "cwd": "", "sessionId": SID_A, "alive": True, "statusUpdatedAt": None}}
    status = {SID_A: {"ts": now - 30, "session_id": SID_A, "model": "Opus 5", "context_pct": 42.7, "cost_usd": 1.23,
                      "rate_limits": {"five_hour": {"used_pct": 35, "resets_at": now + 600}}},
              SID_B: {"ts": now, "context_pct": 99}}
    r = tm.merge([pane("a", state="working", since=str(now - 5))], reg, now=now, status=status)[0]
    assert r["ctx_pct"] == 42 and r["model"] == "Opus 5" and r["cost_usd"] == 1.23 and r["status_age"] == 30
    assert r["quota"]["five_hour"]["used_pct"] == 35


def test_merge_ignores_status_records_older_than_an_hour(tm):
    now = 1_000_000
    reg = {"a:@1.%1": {"status": "busy", "name": "", "cwd": "", "sessionId": SID_A, "alive": True, "statusUpdatedAt": None}}
    status = {SID_A: {"ts": now - 3601, "model": "Opus 5", "context_pct": 42, "cost_usd": 1.0, "rate_limits": {}}}
    stale = tm.merge([pane("a")], reg, now=now, status=status)[0]
    assert (stale["ctx_pct"], stale["model"], stale["cost_usd"], stale["quota"], stale["status_age"]) == (None, "", None, None, None)
    none = tm.merge([pane("a")], reg, now=now)[0]                 # no status directory at all
    assert none["ctx_pct"] is None and none["model"] == "" and none["quota"] is None


def test_render_table_has_a_ctx_column(tm):
    now = 1000
    reg = {"api:@1.%1": {"status": "busy", "name": "", "cwd": "", "sessionId": SID_A, "alive": True, "statusUpdatedAt": None}}
    rows = tm.merge([pane("api", state="working", since="990"), pane("plain", pane_id="%2", window_id="@2", cmd="zsh")],
                    reg, now=now, status={SID_A: {"ts": now, "context_pct": 42, "model": "Opus 5"}})
    lines = tm.render_table(rows).splitlines()
    assert lines[0].split()[:5] == ["SESSION", "PROJECT", "STATE", "CTX", "AGE"]
    assert lines[1].split()[3] == "42%" and lines[2].split()[3] == "-"


# ---------- phase 4: trails ----------
def test_load_trails_reads_the_hook_jsonl(tm, tmp_path):
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, [
        {"ts": 1000, "event": "SessionStart", "session_id": SID_A, "tmux_session": "worker", "pane": "%0",
         "cwd": "/Volumes/Dev/worker", "color": "#bf5af2", "source": "startup"},
        {"ts": 1200, "event": "Stop", "session_id": SID_A, "detail": "wrote the merge helper"},
        {"ts": 1300, "event": "Stop", "session_id": SID_A, "detail": "ran the tests"},
        {"ts": 1400, "event": "SessionEnd", "session_id": SID_A, "reason": "prompt_input_exit"},
    ])
    assert tmcore.load_trails(trace, now=2000, keep_days=30) == [
        {"session_id": SID_A, "tmux_session": "worker", "pane": "%0", "cwd": "/Volumes/Dev/worker",
         "color": "#bf5af2", "started": 1000, "last_ts": 1400, "last_event": "SessionEnd",
         "end_reason": "prompt_input_exit", "last_message": "ran the tests"}]


def test_load_trails_skips_malformed_lines_and_empty_files(tm, tmp_path):
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, ["{not json", "", "   ", "[1, 2]", '"a string"',
                               {"ts": "soon", "event": "SessionStart", "tmux_session": "x"},
                               {"ts": 1500, "event": "Stop", "detail": "last words"}])
    write_trail(trace, SID_B, [])
    write_trail(trace, SID_C, ["garbage only"])
    trails = tmcore.load_trails(trace, now=2000, keep_days=30)
    assert [t["session_id"] for t in trails] == [SID_A]           # nothing parseable -> no tombstone
    assert trails[0]["tmux_session"] == "x" and trails[0]["started"] is None
    assert trails[0]["last_ts"] == 1500 and trails[0]["last_message"] == "last words"


def test_load_trails_prunes_files_older_than_keep_days(tm, tmp_path):
    trace = tmp_path / "trace"
    now = 40 * 86400
    old = write_trail(trace, SID_A, [{"ts": 10, "event": "SessionStart"}], mtime=now - 31 * 86400)
    keep = write_trail(trace, SID_B, [{"ts": now - 100, "event": "SessionStart"}], mtime=now - 100)
    (trace / "notes.txt").write_text("not a trail")
    assert [t["session_id"] for t in tmcore.load_trails(trace, now=now, keep_days=30)] == [SID_B]
    assert not old.exists() and keep.exists() and (trace / "notes.txt").exists()
    assert tmcore.load_trails(tmp_path / "nothing", now=now, keep_days=30) == []


def _trail(session_id, name, last_ts, last_event, end_reason="", message=""):
    return {"session_id": session_id, "tmux_session": name, "pane": "%0", "cwd": f"/Volumes/Dev/{name}/",
            "color": "#bf5af2", "started": 100, "last_ts": last_ts, "last_event": last_event,
            "end_reason": end_reason, "last_message": message}


def test_tombstones_classify_sort_and_skip_live_sessions(tm):
    trails = [_trail(SID_A, "alpha", 900, "Stop", message="killed mid-flight"),
              _trail(SID_B, "beta", 800, "SessionEnd", "logout"),
              _trail(SID_C, "gamma", 950, "SessionEnd", "clear"),
              _trail(SID_LIVE, "live", 999, "Stop")]
    registry = {"live:@1.%1": {"sessionId": SID_LIVE, "alive": True}, "old:@1.%2": {"sessionId": SID_A, "alive": False}}
    out = tmcore.tombstones(trails, registry)
    assert [t["session_id"] for t in out] == [SID_A, SID_C, SID_B]        # killed first, then newest first
    assert [t["ended"] for t in out] == ["killed", "superseded", "clean"]
    assert out[0]["project"] == "alpha"
    assert "session_exists" not in out[0]            # nothing read it; a resume picks its own free name
    assert out[0]["last_message"] == "killed mid-flight"


def test_load_trails_returns_the_newest_trail_first(tm, tmp_path):
    """The order is a contract: `find_trail` takes the first match, so it must be the most recent one."""
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, [{"ts": 500, "event": "SessionStart", "tmux_session": "alpha"}], mtime=1000)
    write_trail(trace, SID_B, [{"ts": 900, "event": "Stop", "tmux_session": "beta"}], mtime=1000)
    write_trail(trace, SID_C, [{"ts": 700, "event": "Stop", "tmux_session": "gamma"}], mtime=1000)
    write_trail(trace, SID_LIVE, ["garbage", {"event": "Stop", "tmux_session": "no-ts"}], mtime=2000)
    ids = [t["session_id"] for t in tmcore.load_trails(trace, now=5000, keep_days=30)]
    assert ids == [SID_B, SID_C, SID_A, SID_LIVE]     # by last_ts, then by mtime for the one with no timestamp


def test_resumable_trails_never_offers_a_live_conversation(tm, tmp_path):
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, [{"ts": 900, "event": "Stop", "tmux_session": "alpha"}])
    write_trail(trace, SID_LIVE, [{"ts": 990, "event": "Stop", "tmux_session": "alpha"}])
    registry = {"alpha:@1.%1": {"sessionId": SID_LIVE, "alive": True}}
    assert tmcore.alive_session_ids(registry) == {SID_LIVE}
    rows = tmcore.resumable_trails(trace, registry, now=1000, keep_days=30)
    assert [t["session_id"] for t in rows] == [SID_A]     # the newest trail is still running: not a candidate


def test_status_numbers_that_json_cannot_represent_are_dropped(tm, tmp_path):
    """NaN / Infinity parse out of a status file but cannot be re-encoded: they would break the UI's fetch."""
    d = tmp_path / "status"
    (d).mkdir(parents=True, exist_ok=True)
    (d / f"{SID_A}.json").write_text(
        '{"ts": 1000, "session_id": "' + SID_A + '", "model": "Opus 5", "context_pct": NaN,'
        ' "cost_usd": Infinity, "rate_limits": {"five_hour": {"used_pct": NaN, "resets_at": Infinity}}}')
    reg = {"a:@1.%1": {"status": "idle", "name": "", "cwd": "", "sessionId": SID_A, "alive": True, "statusUpdatedAt": None}}
    row = tm.merge([pane("a", state="idle", since="1000")], reg, now=1000, status=tmcore.load_status(d))[0]
    assert row["ctx_pct"] is None and row["cost_usd"] is None and row["quota"] is None
    json.dumps(row, allow_nan=False)                      # what the server does on every poll


def test_cmd_resume_refuses_a_conversation_that_is_still_running(tm, tmp_path, capsys):
    trace = tmp_path / "trace"
    write_trail(trace, SID_LIVE, [{"ts": 900, "event": "Stop", "tmux_session": "alpha", "cwd": str(tmp_path)}])
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    registry = {"alpha:@1.%1": {"sessionId": SID_LIVE, "alive": True}}
    t = FakeTmux(panes=[pane("alpha")])
    assert tm.cmd_resume(t, None, trace, registry, cfg, "alpha", now=1000) == 1
    assert "no trail" in capsys.readouterr().err
    assert not any(c[0] == "new" for c in t.calls)


def test_cmd_forget_also_drops_the_status_leftover(tm, tmp_path, capsys):
    trace, status = tmp_path / "trace", tmp_path / "status"
    trail = write_trail(trace, SID_A, [{"ts": 1, "event": "SessionStart"}])
    leftover = write_status(status, SID_A)
    assert tm.cmd_forget(trace, SID_A, status) == 0
    assert not trail.exists() and not leftover.exists()
    capsys.readouterr()


# ---------- phase 4: CLI ----------
def test_cmd_ls_json_includes_the_context_fields(tm, capsys):
    t = FakeTmux(panes=[pane("api", state="idle", since="0")])
    reg = {"api:@1.%1": {"status": "idle", "name": "", "cwd": "", "sessionId": SID_A, "alive": True, "statusUpdatedAt": None}}
    status = {SID_A: {"ts": 10, "model": "Opus 5", "context_pct": 42, "cost_usd": 0.5}}
    assert tm.cmd_ls(t, registry=reg, json_out=True, now=10, status=status) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["ctx_pct"] == 42 and data[0]["model"] == "Opus 5" and data[0]["cost_usd"] == 0.5


def test_cmd_new_types_the_configured_claude_command(tm, capsys):
    t = FakeTmux(panes=[])
    assert tm.cmd_new(t, "p9", cwd=None, color=None, attach=False, start_claude=True, claude_cmd="claude --model opus") == 0
    assert ("send", "=p9:", "claude --model opus") in t.calls
    capsys.readouterr()


def test_cmd_go_resolves_a_topic_to_a_session(tm, capsys):
    t = FakeTmux(panes=[pane("api", title="✳ Phase 2", path="/x/api-svc"),
                        pane("notes", pane_id="%2", window_id="@2", title="✳ groceries", path="/x/notes")])
    it2 = FakeIt2(rows=[it2_row("G1"), it2_row("G2")], panes={"G1": "1", "G2": "2"})
    assert tm.cmd_go(t, it2, "phase") == 0 and it2.focused == ["G1"]
    assert tm.cmd_go(t, it2, "notes") == 0 and it2.focused[-1] == "G2"
    assert tm.cmd_go(t, it2, "zzz") == 1 and "no session" in capsys.readouterr().err


def test_cmd_dead_lists_tombstones_and_hides_superseded(tm, tmp_path, capsys):
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, [{"ts": 900, "event": "SessionStart", "tmux_session": "alpha", "cwd": "/x/alpha"},
                               {"ts": 950, "event": "Stop", "detail": "mid flight"}])
    write_trail(trace, SID_C, [{"ts": 900, "event": "SessionStart", "tmux_session": "gamma", "cwd": "/x/gamma"},
                               {"ts": 960, "event": "SessionEnd", "reason": "clear"}])
    assert tm.cmd_dead(trace, {}, now=1000) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "killed" in out and "mid flight" in out and "gamma" not in out
    assert tm.cmd_dead(trace, {}, show_all=True, json_out=True, now=1000) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["ended"] for d in data] == ["killed", "superseded"]
    assert tm.cmd_dead(tmp_path / "empty", {}, now=1000) == 0 and "no dead" in capsys.readouterr().out


def test_cmd_resume_and_forget(tm, tmp_path, capsys):
    trace = tmp_path / "trace"
    path = write_trail(trace, SID_A, [{"ts": 900, "event": "SessionStart", "tmux_session": "alpha",
                                       "cwd": str(tmp_path), "color": "#bf5af2"}])
    cfg = tmconfig.load_config(path=tmp_path / "none.toml")
    t = FakeTmux(panes=[pane("alpha")])
    assert tm.cmd_resume(t, None, trace, {}, cfg, "alpha", now=1000) == 0
    assert ("new", "alpha-2", str(tmp_path)) in t.calls
    assert any(c[0] == "text" and c[2] == f"claude --resume {SID_A}" for c in t.calls)
    assert not any(c[0] == "enter" for c in t.calls)         # resume_mode "type": the user presses Enter
    assert "alpha-2" in capsys.readouterr().out
    assert tm.cmd_resume(t, None, trace, {}, cfg, "nothing-like-this", now=1000) == 1
    assert "no trail" in capsys.readouterr().err
    assert tm.cmd_forget(trace, SID_A) == 0 and not path.exists()
    assert tm.cmd_forget(trace, SID_A) == 1 and tm.cmd_forget(trace, "../../etc/passwd") == 1
    capsys.readouterr()


def test_main_dispatch_for_dead_resume_forget(tm, tmp_path, monkeypatch, capsys):
    trace = tmp_path / "trace"
    write_trail(trace, SID_A, [{"ts": 900, "event": "SessionStart", "tmux_session": "alpha", "cwd": str(tmp_path)}])
    fake = FakeTmux(panes=[])
    monkeypatch.setattr(tm, "TRACE_DIR", trace)
    monkeypatch.setattr(tm, "STATUS_DIR", tmp_path / "status")
    monkeypatch.setattr(tm, "Tmux", lambda: fake)
    monkeypatch.setattr(tm, "load_registry", lambda _d: {})
    monkeypatch.setattr(tm, "load_config", lambda: tmconfig.load_config(path=tmp_path / "none.toml"))
    assert tm.main(["dead", "--json", "--all"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == SID_A
    assert tm.main(["resume", SID_A]) == 0 and ("new", "alpha", str(tmp_path)) in fake.calls
    capsys.readouterr()
    assert tm.main(["forget", SID_A]) == 0 and not (trace / f"{SID_A}.jsonl").exists()
    assert tm.main(["ls"]) == 0
    capsys.readouterr()


# ---------- phase 4 fixes: doctor flags + the statusline fast path ----------
def test_doctor_and_setup_thread_the_no_statusline_flag(tm, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(tm, "doctor_checks", lambda **kw: seen.append(kw) or [{"name": "x", "ok": True, "info": ""}])
    assert tm.main(["doctor", "--no-ui", "--no-statusline"]) == 0
    assert seen[-1]["statusline"] is False and seen[-1]["ui"] is False
    assert tm.main(["doctor"]) == 0
    assert seen[-1]["statusline"] is True and seen[-1]["ui"] is True
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **_kw: [])
    assert tm.main(["setup", "--no-ui", "--no-statusline"]) == 0
    assert seen[-1]["statusline"] is False
    capsys.readouterr()


def test_the_tap_runs_end_to_end_through_the_cli(tm, tmp_path):
    """The real command, a real child process: bytes in, bytes out, exit status and stderr preserved."""
    state = tmp_path / "state"
    state.mkdir()
    sink = tmp_path / "sink"
    (state / "statusline.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": f"cat > {sink}; echo boom >&2; exit 7"}}))
    payload = json.dumps({"session_id": "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d",
                          "model": {"display_name": "Opus 5"},
                          "context_window": {"used_percentage": 42}}).encode()
    env = {**os.environ, "HOME": str(tmp_path), "KALMUX_STATE_DIR": str(state)}
    p = subprocess.run([sys.executable, str(BIN / "kalmux"), "statusline"], input=payload,
                       env=env,
                       capture_output=True, check=False)
    assert p.returncode == 7 and p.stdout == b"" and p.stderr == b"boom\n"
    assert sink.read_bytes() == payload
    written = json.loads((state / "status" / "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d.json").read_text())
    assert written["context_pct"] == 42
