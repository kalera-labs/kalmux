"""Tests for src/kalmux/tmrestore.py: the session snapshot and the restore after a reboot.

Everything runs against the fakes and tmp_path, except the one end-to-end test at the bottom, which drives
a REAL tmux on a private socket (`-L kalmux-test-<pid>`) and kills only the sessions it created.
"""
import contextlib
import dataclasses
import json
import os
import subprocess
import threading
import time

import pytest

from fakes import FakeIt2, FakeTmux, pane
from kalmux import tmcore, tmrestore
from kalmux.tmcore import Tmux, resolve_tmux

BOOT_OUT = "{ sec = 1790848664, usec = 496772 } Tue Sep 30 21:37:44 2026\n"


def fake_tmux(*names, colors=None, path="/Volumes/Dev/proj"):
    """A FakeTmux holding one pane per session name, in that order."""
    panes = [pane(name, pane_id=f"%{i}", window_id=f"@{i}", path=f"{path}/{name}") for i, name in enumerate(names)]
    return FakeTmux(panes=panes, colors=dict(colors or {}))


def saved(name, cwd="/tmp", color="", created=1000):
    return tmrestore.SavedSession(name=name, cwd=cwd, color=color, created=created)


# ---------- boot time ----------
def test_boot_time_reads_the_kernel_boot_second():
    assert tmrestore.boot_time(lambda _cmd: (0, BOOT_OUT)) == 1790848664


@pytest.mark.parametrize("answer", [(1, ""), (0, ""), (0, "nonsense"), (127, "sysctl: unknown oid"), (0, "{ sec = x }")])
def test_boot_time_is_zero_when_the_kernel_does_not_answer(answer):
    """0 means "unknown": the restore must then do nothing rather than guess that this is a new boot."""
    assert tmrestore.boot_time(lambda _cmd: answer) == 0


def test_boot_time_asks_sysctl_for_kern_boottime():
    seen = []
    tmrestore.boot_time(lambda cmd: (seen.append(cmd), (0, BOOT_OUT))[1])
    assert seen == [list(tmrestore.BOOT_COMMAND)] and "kern.boottime" in tmrestore.BOOT_COMMAND


def test_boot_time_on_this_machine_is_either_a_real_second_or_zero():
    value = tmrestore.boot_time()
    assert value == 0 or 1_000_000_000 < value < time.time() + 60


def test_boot_time_survives_a_sysctl_that_cannot_even_run(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no such binary")
    monkeypatch.setattr(tmrestore.subprocess, "run", boom)
    assert tmrestore.boot_time() == 0


# ---------- live sessions ----------
def test_live_sessions_reads_name_cwd_color_and_creation_order():
    tmux = fake_tmux("api", "web", colors={"api": "#0a84ff"})
    rows = tmrestore.live_sessions(tmux)
    assert [s.name for s in rows] == ["api", "web"]
    assert rows[0].cwd == "/Volumes/Dev/proj/api" and rows[0].color == "#0a84ff" and rows[0].created == 1000
    assert rows[1].color == "" and rows[1].created == 1001


def test_live_sessions_returns_none_when_tmux_does_not_answer():
    """A stopped server exits 1: that is NOT "the user has no sessions", so nothing may be written."""
    tmux = fake_tmux("api")
    tmux.down = True
    assert tmrestore.live_sessions(tmux) is None


def test_live_sessions_drops_forged_names_and_junk_colors():
    tmux = fake_tmux("api")
    tmux.panes.append(pane("bad name", pane_id="%9", path="/tmp"))
    tmux.colors = {"api": "not-a-color"}
    rows = tmrestore.live_sessions(tmux)
    assert [s.name for s in rows] == ["api"] and rows[0].color == ""


def test_live_sessions_keeps_creation_order_even_when_tmux_lists_them_alphabetically():
    tmux = fake_tmux("zeta", "alpha")
    tmux.colors = {}
    rows = tmrestore.live_sessions(tmux)
    assert [s.name for s in rows] == ["zeta", "alpha"]       # created 1000 before 1001


def test_live_sessions_skips_records_with_the_wrong_number_of_fields(monkeypatch):
    tmux = fake_tmux("api")
    monkeypatch.setattr(tmux, "run_rc", lambda *_a: (0, "short\nalpha\x1f/tmp\x1f\x1f7\n"))
    assert [s.name for s in tmrestore.live_sessions(tmux)] == ["alpha"]


def test_live_sessions_keeps_a_session_whose_directory_name_holds_a_newline(monkeypatch):
    """macOS allows a newline in a directory name and tmux passes #{pane_current_path} through verbatim:
    splitting the output on "\\n" would break that record in two and drop the session for good."""
    tmux = fake_tmux("api")
    out = "api\x1f/tmp/two\nlines\x1f#0a84ff\x1f1000\nweb\x1f/tmp\x1f\x1f1001\n"
    monkeypatch.setattr(tmux, "run_rc", lambda *_a: (0, out))
    rows = tmrestore.live_sessions(tmux)
    assert [s.name for s in rows] == ["api", "web"]
    assert rows[0].cwd == "/tmp/two\nlines"      # the exact path, or the restore cannot find the directory


# ---------- the snapshot file ----------
def test_saved_session_and_snapshot_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        saved("api").name = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        tmrestore.Snapshot(boot=1, saved_at=2).boot = 3


def test_write_then_read_a_snapshot_round_trips(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    snap = tmrestore.Snapshot(boot=11, saved_at=22, sessions=(saved("api", color="#0a84ff"), saved("web", created=1001)))
    assert tmrestore.write_snapshot(path, snap) is True
    back = tmrestore.read_snapshot(path)
    assert back == snap and back.names == ("api", "web")
    assert json.loads(path.read_text())["version"] == tmrestore.SNAPSHOT_VERSION


def test_the_snapshot_file_is_private(tmp_path):
    """It lists every project the user works on: 0600 in a 0700 directory, like the rest of the state dir."""
    path = tmp_path / "state" / "sessions.json"
    tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),)))
    assert oct(path.stat().st_mode)[-3:] == "600" and oct(path.parent.stat().st_mode)[-3:] == "700"
    assert not list(path.parent.glob("*.tmp"))               # the temp file is renamed, never left behind


def test_write_snapshot_replaces_the_previous_file_atomically(tmp_path):
    path = tmp_path / "sessions.json"
    tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),)))
    tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=1, saved_at=3, sessions=()))
    assert tmrestore.read_snapshot(path).sessions == ()


def test_write_snapshot_never_reuses_one_temp_path(tmp_path, monkeypatch):
    """A fixed `sessions.json.tmp` is one shared, truncated buffer: two writers (two threads, or an old
    server shutting down while a new one starts) overwrite each other there and rename a mixed file in."""
    path = tmp_path / "sessions.json"
    snap = tmrestore.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),))
    seen, real = [], os.replace
    monkeypatch.setattr(tmrestore.os, "replace", lambda src, dst: (seen.append(str(src)), real(src, dst))[1])
    assert tmrestore.write_snapshot(path, snap) and tmrestore.write_snapshot(path, snap)
    assert len(set(seen)) == 2 and str(path) + ".tmp" not in seen
    assert all(name.startswith(str(path) + ".") and name.endswith(".tmp") for name in seen)


def test_write_snapshot_cleans_up_its_temp_file_when_the_rename_fails(tmp_path, monkeypatch):
    def refuse(_src, _dst):
        raise OSError("read-only file system")

    monkeypatch.setattr(tmrestore.os, "replace", refuse)
    path = tmp_path / "sessions.json"
    assert tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=1, saved_at=2)) is False
    assert not path.exists() and list(tmp_path.glob("*.tmp")) == []


def test_write_snapshot_reports_a_directory_it_cannot_create(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    assert tmrestore.write_snapshot(blocker / "sessions.json", tmrestore.Snapshot(boot=1, saved_at=2)) is False


@pytest.mark.parametrize("body", [
    "", "not json", "[]", '{"version": 2, "boot": 1, "saved_at": 2, "sessions": []}',
    '{"version": 1, "boot": "x", "saved_at": 2, "sessions": []}',
    '{"version": 1, "boot": 1, "saved_at": 2, "sessions": {}}',
])
def test_read_snapshot_refuses_anything_that_is_not_our_document(tmp_path, body):
    path = tmp_path / "sessions.json"
    path.write_text(body)
    assert tmrestore.read_snapshot(path) is None


def test_read_snapshot_of_a_missing_file_is_none(tmp_path):
    assert tmrestore.read_snapshot(tmp_path / "nope.json") is None


def test_read_snapshot_drops_entries_that_are_not_restorable(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": 1, "boot": 1, "saved_at": 2, "sessions": [
        {"name": "api", "cwd": "/tmp", "color": "#0a84ff", "created": 7},
        {"name": "bad name", "cwd": "/tmp", "color": "", "created": 8},
        {"name": "nocwd", "created": 9},
        "junk",
        {"name": "badcolor", "cwd": "/tmp", "color": "red", "created": 10},
    ]}))
    snap = tmrestore.read_snapshot(path)
    assert snap.names == ("api", "nocwd", "badcolor")
    assert snap.sessions[1].cwd == "" and snap.sessions[2].color == ""


def test_copy_snapshot_keeps_the_previous_boots_list(tmp_path):
    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=7, saved_at=8, sessions=(saved("api"),)))
    assert tmrestore.copy_snapshot(path, previous) is True
    assert tmrestore.read_snapshot(previous).boot == 7 and oct(previous.stat().st_mode)[-3:] == "600"
    assert tmrestore.copy_snapshot(tmp_path / "gone.json", previous) is False


# ---------- the snapshotter ----------
class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_snapshotter_writes_only_when_the_session_list_changed(tmp_path):
    tmux = fake_tmux("api")
    clock = Clock()
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=42, clock=clock)
    assert shot.tick() is True
    assert tmrestore.read_snapshot(shot.path).names == ("api",) and tmrestore.read_snapshot(shot.path).boot == 42
    clock.now += 5
    assert shot.tick() is False                              # same list: no write, no churn on the disk
    tmux.panes.append(pane("web", pane_id="%9", path="/tmp/web"))
    assert shot.tick() is True and tmrestore.read_snapshot(shot.path).names == ("api", "web")
    assert tmrestore.read_snapshot(shot.path).saved_at == int(clock.now)


def test_snapshotter_can_be_forced_to_write_an_unchanged_list(tmp_path):
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.tick() is True and shot.tick() is False and shot.tick(force=True) is True


def test_snapshotter_waits_out_a_shutdown_before_writing_an_empty_list(tmp_path):
    """Every process gets SIGTERM at once at shutdown, so the server dies long before the grace period:
    the last good list survives the reboot. Only a server that stays unreachable really has no sessions."""
    tmux = fake_tmux("api")
    clock = Clock()
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=clock, grace=30.0)
    shot.tick()
    tmux.down = True
    assert shot.tick() is False
    clock.now += 29
    assert shot.tick() is False and tmrestore.read_snapshot(shot.path).names == ("api",)
    clock.now += 2
    assert shot.tick() is True and tmrestore.read_snapshot(shot.path).names == ()
    clock.now += 60
    assert shot.tick() is False                              # the empty list is written once, not every 5 s
    tmux.down = False
    assert shot.tick() is True and tmrestore.read_snapshot(shot.path).names == ("api",)


def test_snapshotter_final_snapshot_needs_a_live_tmux(tmp_path):
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.final() is True and tmrestore.read_snapshot(shot.path).names == ("api",)
    tmux.down = True
    assert shot.final() is False
    assert tmrestore.read_snapshot(shot.path).names == ("api",)      # the dying server never empties the list


def test_snapshotter_survives_a_state_dir_it_cannot_write(tmp_path):
    """An unwritable state dir is reported as "nothing written", never as an exception in the thread."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    shot = tmrestore.Snapshotter(blocker / "sessions.json", fake_tmux("api"), boot=1, clock=Clock())
    assert shot.tick() is False and shot.tick() is False and shot.final() is False


def test_snapshotter_says_so_when_it_could_not_write(tmp_path):
    """A write that fails silently is how a snapshot stops being taken without anyone noticing."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    lines = []
    shot = tmrestore.Snapshotter(blocker / "sessions.json", fake_tmux("api"), boot=1, clock=Clock(), log=lines.append)
    assert shot.tick() is False
    assert any("could not write" in line for line in lines)


class FlipTmux:
    """A tmux whose list flips between 40 sessions and 1, so two writers always disagree in length:
    that is what turns an interleaved write into a short record on top of a long one."""

    socket = ""

    def __init__(self):
        self._n = 0
        self._long = "".join(f"s{i}\x1f/Volumes/Dev/p{i}\x1f\x1f{1000 + i}\n" for i in range(40))
        self._short = "api\x1f/tmp\x1f\x1f1\n"

    def run_rc(self, *_args):
        self._n += 1
        return 0, self._long if self._n % 2 else self._short


def test_snapshotter_rounds_never_overlap(tmp_path):
    """A round reads tmux and then writes what it read. Two of them inside each other set _last from one
    list while another list is what actually reached the file, and the difference is never written again."""
    busy, peak, guard = [0], [0], threading.Lock()

    class CountingTmux:
        socket = ""

        def run_rc(self, *_args):
            with guard:
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            time.sleep(0.002)
            with guard:
                busy[0] -= 1
            return 0, "api\x1f/tmp\x1f\x1f1\n"

    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", CountingTmux(), boot=1, clock=Clock())
    rounds = [lambda: shot.tick(force=True), shot.tick, shot.final]
    threads = [threading.Thread(target=lambda r=r: [r() for _ in range(20)]) for r in rounds for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert peak[0] == 1 and not any(t.is_alive() for t in threads)


def test_concurrent_ticks_never_leave_a_torn_snapshot_on_disk(tmp_path):
    """Three threads drive one Snapshotter: the keeper loop every 5 s, an HTTP handler through
    Backend._kept() right after an action, and the main thread's final() in the SIGTERM handler. Nothing
    serialises them, and a torn file stays torn (the writer that won set _last, so the next tick writes
    nothing) — a reboot in that window brings back no sessions at all."""
    path = tmp_path / "sessions.json"
    shot = tmrestore.Snapshotter(path, FlipTmux(), boot=1, clock=Clock())
    dropped, unreadable, stop = [], [], threading.Event()

    def write():
        for _ in range(150):
            if not shot.tick(force=True):
                dropped.append(1)

    def read():
        while not stop.is_set():
            if path.exists() and tmrestore.read_snapshot(path) is None:
                unreadable.append(1)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    writers = [threading.Thread(target=write) for _ in range(4)]
    for t in writers:
        t.start()
    for t in writers:
        t.join(timeout=60)
    stop.set()
    reader.join(timeout=5)
    assert dropped == [] and unreadable == []
    assert tmrestore.read_snapshot(path) is not None


# ---------- restoring ----------
def test_restore_recreates_missing_sessions_in_creation_order(tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    tmux = fake_tmux("api")
    lines = []
    created = tmrestore.restore(tmux, [saved("api", cwd=str(tmp_path / "api")),
                                       saved("web", cwd=str(tmp_path / "web"), color="#0a84ff", created=1001)],
                                log=lines.append)
    assert created == ["web"] and tmux.has_session("web")
    assert ("new", "web", str(tmp_path / "web")) in tmux.calls
    assert ("set", "web", "@tm_color", "#0a84ff") in tmux.calls
    assert any("api" in line and "already" in line for line in lines)


def test_restore_falls_back_to_home_when_the_directory_is_gone(tmp_path):
    tmux = fake_tmux("api")
    lines = []
    created = tmrestore.restore(tmux, [saved("web", cwd=str(tmp_path / "gone"))], log=lines.append, home=str(tmp_path))
    assert created == ["web"] and ("new", "web", str(tmp_path)) in tmux.calls
    assert any("gone" in line for line in lines)             # tmux would move it to $HOME silently; we say so


def test_restore_skips_invalid_names_and_reports_what_tmux_refused():
    tmux = fake_tmux("api")
    tmux.fail.add("new")
    lines = []
    created = tmrestore.restore(tmux, [saved("bad name"), saved("web")], log=lines.append)
    assert created == [] and any("bad name" in line for line in lines) and any("refused" in line for line in lines)


def test_restore_holds_the_lock_while_it_talks_to_tmux():
    lock = threading.Lock()
    tmux = fake_tmux("api")
    held = []
    original = tmux.new_session
    tmux.new_session = lambda name, cwd: (held.append(lock.locked()), original(name, cwd))[1]
    tmrestore.restore(tmux, [saved("web")], lock=lock)
    assert held == [True] and not lock.locked()


def test_restore_and_plan_never_echo_control_characters_from_a_directory(tmp_path):
    """The directory comes from tmux verbatim; the log goes to ui.log and to the user's terminal."""
    tmux = fake_tmux("api")
    lines = []
    tmrestore.restore(tmux, [saved("web", cwd="/tmp/two\nlines")], log=lines.append, home=str(tmp_path))
    assert lines and all("\n" not in line for line in lines) and "two?lines" in lines[0]
    assert "\n" not in tmrestore.plan(tmux, [saved("db", cwd="/tmp/two\nlines")], home=str(tmp_path))[0]


def test_plan_says_what_a_restore_would_do_without_touching_tmux(tmp_path):
    tmux = fake_tmux("api")
    lines = tmrestore.plan(tmux, [saved("api"), saved("web", cwd=str(tmp_path)), saved("old", cwd="/gone"),
                                  saved("bad name")], home=str(tmp_path))
    assert lines[0].startswith("exists") and "api" in lines[0]
    assert lines[1].startswith("create") and str(tmp_path) in lines[1]
    assert "gone" in lines[2] and str(tmp_path) in lines[2]
    assert lines[3].startswith("skip")
    assert ("new", "web", str(tmp_path)) not in tmux.calls


# ---------- iTerm2 tabs ----------
def test_open_tabs_uses_the_current_window():
    it2 = FakeIt2(window="pty-CUR")
    assert tmrestore.open_tabs(it2, ["api", "web"]) == 2
    assert [w for _c, w in it2.tabs] == ["pty-CUR", "pty-CUR"]
    assert it2.tabs[0][0] == """/bin/zsh -lc 'exec tmux -CC attach -t "=api"'; exit"""


def test_open_tabs_opens_one_window_first_when_iterm2_has_none():
    """After a reboot this user has OpenNoWindowsAtStartup=1: `it2 tab new` fails with "No current window"
    until a window exists, so the first session gets `it2 window new` and the rest land in that window."""
    it2 = FakeIt2(window="")
    lines = []
    assert tmrestore.open_tabs(it2, ["api", "web", "db"], log=lines.append) == 3
    assert len(it2.new_windows) == 1 and [w for _c, w in it2.tabs] == ["pty-NEW", "pty-NEW"]


def test_open_tabs_asks_it2_for_the_window_when_its_output_names_none():
    """"Created new window: pty-XYZ" is a message, not a contract: without a parsed id every later
    session would open a window of its own, so the front-most window is asked for instead."""
    it2 = FakeIt2(window="")
    it2.new_window_reply, it2.new_window_id = "ok", "pty-FRONT"
    lines = []
    assert tmrestore.open_tabs(it2, ["api", "web", "db"], log=lines.append) == 3
    assert len(it2.new_windows) == 1 and [w for _c, w in it2.tabs] == ["pty-FRONT", "pty-FRONT"]
    assert lines == []


def test_open_tabs_says_so_once_when_no_window_id_can_be_found():
    it2 = FakeIt2(window="")
    it2.new_window_reply, it2.new_window_id = "ok", ""
    lines = []
    assert tmrestore.open_tabs(it2, ["api", "web", "db"], log=lines.append) == 3
    assert len(it2.new_windows) == 3 and it2.tabs == []       # degraded, but no longer silent
    assert len(lines) == 1 and "window" in lines[0]


def test_open_tabs_without_iterm2_opens_nothing():
    lines = []
    assert tmrestore.open_tabs(FakeIt2(available=False), ["api"], log=lines.append) == 0
    assert any("it2" in line for line in lines)
    assert tmrestore.open_tabs(FakeIt2(), [], log=lines.append) == 0


def test_open_tabs_reports_a_failing_it2_and_keeps_going():
    it2 = FakeIt2(window="pty-CUR")
    it2.fail_tab = True
    lines = []
    assert tmrestore.open_tabs(it2, ["api", "web"], log=lines.append) == 0
    assert len(lines) == 2 and all("boom" in line for line in lines)


def test_open_tabs_attaches_to_the_private_socket_when_there_is_one():
    it2 = FakeIt2(window="pty-CUR")
    tmrestore.open_tabs(it2, ["api"], socket="kalmux-test-1")
    assert "tmux -L kalmux-test-1 -CC attach" in it2.tabs[0][0]


def test_open_tabs_skips_a_name_no_tab_command_can_carry():
    it2 = FakeIt2(window="pty-CUR")
    assert tmrestore.open_tabs(it2, ["bad name"]) == 0 and it2.tabs == []


# ---------- restore on start ----------
def snapshot_file(tmp_path, boot, *names, cwd=None):
    path = tmp_path / "sessions.json"
    sessions = tuple(saved(n, cwd=cwd or str(tmp_path), created=1000 + i) for i, n in enumerate(names))
    tmrestore.write_snapshot(path, tmrestore.Snapshot(boot=boot, saved_at=1, sessions=sessions))
    return path


def test_restore_on_start_recreates_the_previous_boots_sessions(tmp_path):
    path = snapshot_file(tmp_path, 100, "api", "web")
    previous = tmp_path / "sessions.previous.json"
    tmux, it2 = fake_tmux("api"), FakeIt2(window="pty-CUR")
    lines = []
    report = tmrestore.restore_on_start(tmux, it2, path, previous, boot=200, log=lines.append)
    assert report["ran"] is True and report["created"] == ["web"] and report["tabs"] == 1
    assert tmux.has_session("web")
    assert tmrestore.read_snapshot(previous).names == ("api", "web")      # kept before anything is touched


def test_restore_on_start_does_nothing_within_the_same_boot(tmp_path):
    path = snapshot_file(tmp_path, 200, "api", "web")
    previous = tmp_path / "sessions.previous.json"
    tmux = fake_tmux("api")
    report = tmrestore.restore_on_start(tmux, FakeIt2(), path, previous, boot=200)
    assert report["ran"] is False and report["created"] == [] and not tmux.has_session("web")
    assert not previous.exists()                             # same boot: the list is still the live one


@pytest.mark.parametrize("saved_boot,boot", [(0, 200), (100, 0), (0, 0)])
def test_restore_on_start_refuses_to_guess_when_a_boot_time_is_unknown(tmp_path, saved_boot, boot):
    path = snapshot_file(tmp_path, saved_boot, "web")
    tmux = fake_tmux("api")
    report = tmrestore.restore_on_start(tmux, FakeIt2(), path, tmp_path / "prev.json", boot=boot)
    assert report["ran"] is False and not tmux.has_session("web") and "boot" in report["reason"]


def test_restore_on_start_without_a_snapshot_is_a_no_op(tmp_path):
    report = tmrestore.restore_on_start(fake_tmux("api"), FakeIt2(), tmp_path / "none.json",
                                        tmp_path / "prev.json", boot=200)
    assert report["ran"] is False and "no saved" in report["reason"]


def test_restore_on_start_disabled_still_keeps_the_previous_list(tmp_path):
    """`enabled = false` only turns off the automatic restore: `kalmux restore` must still find the list."""
    path = snapshot_file(tmp_path, 100, "web")
    previous = tmp_path / "sessions.previous.json"
    tmux = fake_tmux("api")
    report = tmrestore.restore_on_start(tmux, FakeIt2(), path, previous, boot=200, enabled=False)
    assert report["ran"] is False and not tmux.has_session("web")
    assert tmrestore.read_snapshot(previous).names == ("web",) and "enabled" in report["reason"]


# ---------- the keeper ----------
def keeper(tmp_path, tmux=None, it2=None, boot=200, **kw):
    out = kw.pop("out", None)
    return tmrestore.SessionKeeper(tmux or fake_tmux("api"), it2 or FakeIt2(window="pty-CUR"),
                                   tmp_path / "sessions.json", tmp_path / "sessions.previous.json",
                                   boot=boot, out=out, clock=Clock(), **kw)


def test_keeper_startup_restores_then_writes_this_boots_list(tmp_path):
    snapshot_file(tmp_path, 100, "api", "web")
    tmux = fake_tmux("api")
    k = keeper(tmp_path, tmux=tmux)
    report = k.startup()
    assert report["created"] == ["web"] and tmux.has_session("web")
    snap = tmrestore.read_snapshot(k.snapshotter.path)
    assert snap.boot == 200 and snap.names == ("api", "web")     # a later `ui restart` must not restore again


def test_keeper_startup_writes_an_empty_list_when_there_is_nothing_at_all(tmp_path):
    k = keeper(tmp_path, tmux=FakeTmux(panes=[]))
    k.startup()
    assert tmrestore.read_snapshot(k.snapshotter.path).names == ()


def test_keeper_run_once_and_stop_take_snapshots(tmp_path):
    tmux = fake_tmux("api")
    k = keeper(tmp_path, tmux=tmux)
    assert k.run_once() is True and k.run_once() is False
    tmux.panes.append(pane("web", pane_id="%9", path="/tmp/web"))
    assert k.run_once() is True
    tmux.kill_session("web")
    k.stop()
    assert tmrestore.read_snapshot(k.snapshotter.path).names == ("api",)


def test_keeper_stop_before_start_is_harmless(tmp_path):
    k = keeper(tmp_path, tmux=FakeTmux(panes=[]))
    k.stop()
    k.stop()
    assert k.thread is None


def test_keeper_thread_runs_the_startup_step_and_is_a_daemon(tmp_path):
    snapshot_file(tmp_path, 100, "web")
    tmux = fake_tmux("api")
    k = keeper(tmp_path, tmux=tmux, every=0.01)
    thread = k.start()
    for _ in range(200):
        if tmux.has_session("web"):
            break
        time.sleep(0.01)
    k.stop()
    thread.join(timeout=5)
    assert thread.daemon is True and tmux.has_session("web") and not thread.is_alive()


def test_keeper_logs_to_the_servers_own_stream(tmp_path, capsys):
    snapshot_file(tmp_path, 100, "web")
    keeper(tmp_path, out=None).startup()                     # out=None: sys.stdout, like serve() does
    out = capsys.readouterr().out
    assert "restore:" in out and "web" in out


def test_keeper_reports_a_snapshot_it_could_not_write(tmp_path, capsys):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    k = tmrestore.SessionKeeper(fake_tmux("api"), FakeIt2(), blocker / "sessions.json",
                                tmp_path / "prev.json", boot=200, clock=Clock())
    assert k.run_once() is False
    assert "could not write" in capsys.readouterr().out


def test_keeper_never_lets_one_bad_round_kill_the_thread(tmp_path, capsys):
    k = keeper(tmp_path, every=0.01)

    def boom(*_a, **_k):
        raise RuntimeError("tmux exploded")
    k.snapshotter.tick = boom
    thread = k.start()
    time.sleep(0.1)
    assert thread.is_alive()
    k.snapshotter.tick = lambda force=False: False
    k.stop()
    thread.join(timeout=5)
    assert "tmux exploded" in capsys.readouterr().out


# ---------- picking a list for `kalmux restore` ----------
def test_restore_source_prefers_the_file_the_server_has_not_rewritten_yet(tmp_path):
    path = snapshot_file(tmp_path, 100, "api")
    previous = tmp_path / "sessions.previous.json"
    tmrestore.write_snapshot(previous, tmrestore.Snapshot(boot=50, saved_at=1, sessions=(saved("ancient"),)))
    snap, source = tmrestore.restore_source(path, previous, boot=200)
    assert snap.names == ("api",) and source == path         # the server has not started since the reboot


def test_restore_source_falls_back_to_the_previous_boots_copy(tmp_path):
    path = snapshot_file(tmp_path, 200, "api")               # already rewritten for this boot
    previous = tmp_path / "sessions.previous.json"
    tmrestore.write_snapshot(previous, tmrestore.Snapshot(boot=100, saved_at=1, sessions=(saved("web"),)))
    snap, source = tmrestore.restore_source(path, previous, boot=200)
    assert snap.names == ("web",) and source == previous


def test_restore_source_finds_nothing_when_there_is_nothing(tmp_path):
    assert tmrestore.restore_source(tmp_path / "a.json", tmp_path / "b.json", boot=200) == (None, None)


# ---------- doctor ----------
def test_snapshot_health_is_green_when_the_file_matches_the_live_sessions(tmp_path):
    path = snapshot_file(tmp_path, 200, "api", "web")
    ok, info = tmrestore.snapshot_health(path, boot=200, live=("web", "api"), now=10)
    assert ok is True and "2 session(s)" in info


def test_snapshot_health_flags_a_missing_stale_or_disagreeing_file(tmp_path):
    missing = tmrestore.snapshot_health(tmp_path / "none.json", boot=200, live=(), now=10)
    assert missing[0] is False and "missing" in missing[1]
    path = snapshot_file(tmp_path, 100, "api")
    stale = tmrestore.snapshot_health(path, boot=200, live=("api",), now=10)
    assert stale[0] is False and "boot" in stale[1]
    drifted = tmrestore.snapshot_health(snapshot_file(tmp_path, 200, "api"), boot=200, live=("api", "web"), now=10)
    assert drifted[0] is False and "web" in drifted[1]


def test_snapshot_health_stays_green_while_tmux_is_not_running(tmp_path):
    path = snapshot_file(tmp_path, 200, "api")
    ok, info = tmrestore.snapshot_health(path, boot=200, live=None, now=61)
    assert ok is True and "1m" in info                       # age is shown, tmux is simply not asked


def test_snapshot_health_without_a_readable_boot_time_only_checks_the_names(tmp_path):
    path = snapshot_file(tmp_path, 100, "api")
    assert tmrestore.snapshot_health(path, boot=0, live=("api",), now=10)[0] is True


# ---------- end to end, against a real tmux on a private socket ----------
TMUX_BIN = resolve_tmux()
SOCKET = f"kalmux-test-{os.getpid()}"


@pytest.fixture
def private_tmux():
    """A Tmux bound to our own socket. Teardown kills every session we made there, never the server:
    `kill-server` on a shell that inherited $TMUX would reach the user's real server (incident 2026-09-14).
    The empty server then stops by itself (`exit-empty on`) and only its socket file is left to tidy up."""
    tmux = Tmux(socket=SOCKET)
    yield tmux
    for line in tmux.run("list-sessions", "-F", "#{session_name}").split("\n"):
        if line.strip():
            tmux.kill_session(line.strip())
    _drop_stale_socket()


def _drop_stale_socket():
    """Remove OUR socket file once no server answers on it, so a hundred runs do not leave a hundred files.

    Only ever the one path this process named (kalmux-test-<pid>), and only once `list-sessions` fails."""
    path = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", SOCKET)
    if SOCKET.startswith("kalmux-test-") and os.path.exists(path) and Tmux(socket=SOCKET).run_rc("list-sessions")[0] != 0:
        with contextlib.suppress(OSError):
            os.unlink(path)


@pytest.mark.skipif(not TMUX_BIN, reason="tmux is not installed")
def test_end_to_end_snapshot_and_restore_on_a_private_socket(tmp_path, private_tmux):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    names = (f"e2e-a-{os.getpid()}", f"e2e-b-{os.getpid()}")
    for name, directory, color in zip(names, (first, second), ("#8a9a5b", "#0a84ff"), strict=True):
        assert private_tmux.new_session(name, str(directory))
        assert private_tmux.set_session_option(name, "@tm_color", color)
    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    shot = tmrestore.Snapshotter(path, private_tmux, boot=111, clock=lambda: 1_000.0)
    assert shot.tick() is True
    assert set(tmrestore.read_snapshot(path).names) >= set(names)

    for name in names:
        assert private_tmux.kill_session(name)
    assert not private_tmux.has_session(names[0])

    it2 = FakeIt2(window="pty-CUR")
    report = tmrestore.restore_on_start(private_tmux, it2, path, previous, boot=222, log=lambda _m: None)
    assert report["ran"] is True and set(report["created"]) >= set(names)
    live = {s.name: s for s in tmrestore.live_sessions(private_tmux)}
    for name, directory, color in zip(names, (first, second), ("#8a9a5b", "#0a84ff"), strict=True):
        assert os.path.realpath(live[name].cwd) == os.path.realpath(directory)
        assert live[name].color == color
    assert tmrestore.read_snapshot(previous).boot == 111
    assert [c for c, _w in it2.tabs] == [f"""/bin/zsh -lc 'exec tmux -L {SOCKET} -CC attach -t "={n}"'; exit"""
                                         for n in names]


def test_a_private_socket_hands_the_child_an_environment_without_tmux(monkeypatch):
    """-L already wins over $TMUX, so only the child's own environment proves the variable was dropped:
    anything the session spawns downstream reads $TMUX directly, not tmux's socket precedence."""
    seen = {}

    class Finished:
        returncode, stdout = 0, b""

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return Finished()

    monkeypatch.setattr(tmcore.subprocess, "run", fake_run)
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1,0")
    Tmux(path="/x/tmux", socket="kalmux-test-env").run_rc("list-sessions")
    assert seen["argv"][:3] == ["/x/tmux", "-L", "kalmux-test-env"]
    assert "TMUX" not in seen["env"] and seen["env"]["PATH"] == os.environ["PATH"]
    Tmux(path="/x/tmux").run_rc("list-sessions")
    assert seen["argv"][:2] == ["/x/tmux", "list-sessions"]
    assert seen["env"] is None                               # the user's own server: inherit, strip nothing


@pytest.mark.skipif(not TMUX_BIN, reason="tmux is not installed")
def test_a_private_socket_never_reaches_the_server_in_this_terminal(private_tmux, monkeypatch):
    """-L wins over $TMUX, and the child never even sees the variable: the user's own server is unreachable."""
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,1,0")
    name = f"e2e-env-{os.getpid()}"
    assert private_tmux.new_session(name, None)
    # tmux copies the STARTING client's environment into its global one, so a leaked TMUX is visible there
    assert private_tmux.run_rc("show-environment", "-g", "TMUX")[0] != 0
    out = subprocess.run([TMUX_BIN, "-L", SOCKET, "list-sessions", "-F", "#{session_name}"],
                         capture_output=True, check=False, timeout=15).stdout.decode()
    assert name in out
    assert private_tmux.socket == SOCKET and Tmux().socket == ""
    with pytest.raises(ValueError):
        Tmux(socket="../../etc/passwd")
