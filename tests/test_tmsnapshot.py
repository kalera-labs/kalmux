"""Tests for src/kalmux/tmsnapshot.py: reading what tmux is running, and the file that remembers it.

Everything here runs against the fakes and tmp_path — no real tmux, no real state directory. The restore
that reads these records is tested in test_tmrestore.py.
"""
import dataclasses
import json
import os
import time

import pytest

from fakes import FakeTmux, pane
from helpers_restore import fake_tmux, saved
from kalmux import tmcore, tmsnapshot

BOOT_OUT = "{ sec = 1790848664, usec = 496772 } Tue Sep 30 21:37:44 2026\n"


# ---------- boot time ----------
def test_boot_time_reads_the_kernel_boot_second():
    assert tmsnapshot.boot_time(lambda _cmd: (0, BOOT_OUT)) == 1790848664


@pytest.mark.parametrize("answer", [(1, ""), (0, ""), (0, "nonsense"), (127, "sysctl: unknown oid"), (0, "{ sec = x }")])
def test_boot_time_is_zero_when_the_kernel_does_not_answer(answer):
    """0 means "unknown": the restore must then do nothing rather than guess that this is a new boot."""
    assert tmsnapshot.boot_time(lambda _cmd: answer) == 0


def test_boot_time_asks_sysctl_for_kern_boottime():
    seen = []
    tmsnapshot.boot_time(lambda cmd: (seen.append(cmd), (0, BOOT_OUT))[1])
    assert seen == [list(tmsnapshot.BOOT_COMMAND)] and "kern.boottime" in tmsnapshot.BOOT_COMMAND


def test_boot_time_on_this_machine_is_either_a_real_second_or_zero():
    value = tmsnapshot.boot_time()
    assert value == 0 or 1_000_000_000 < value < time.time() + 60


def test_boot_time_survives_a_sysctl_that_cannot_even_run(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no such binary")
    monkeypatch.setattr(tmsnapshot.subprocess, "run", boom)
    assert tmsnapshot.boot_time() == 0


# ---------- live sessions ----------
def test_live_sessions_reads_name_cwd_color_and_creation_order():
    tmux = fake_tmux("api", "web", colors={"api": "#0a84ff"})
    rows = tmsnapshot.live_sessions(tmux)
    assert [s.name for s in rows] == ["api", "web"]
    assert rows[0].cwd == "/Volumes/Dev/proj/api" and rows[0].color == "#0a84ff" and rows[0].created == 1000
    assert rows[1].color == "" and rows[1].created == 1001


def test_live_sessions_returns_none_when_tmux_does_not_answer():
    """A stopped server exits 1: that is NOT "the user has no sessions", so nothing may be written."""
    tmux = fake_tmux("api")
    tmux.down = True
    assert tmsnapshot.live_sessions(tmux) is None


def test_live_sessions_drops_forged_names_and_junk_colors():
    tmux = fake_tmux("api")
    tmux.panes.append(pane("bad name", pane_id="%9", path="/tmp"))
    tmux.colors = {"api": "not-a-color"}
    rows = tmsnapshot.live_sessions(tmux)
    assert [s.name for s in rows] == ["api"] and rows[0].color == ""


def test_live_sessions_keeps_creation_order_even_when_tmux_lists_them_alphabetically():
    tmux = fake_tmux("zeta", "alpha")
    tmux.colors = {}
    rows = tmsnapshot.live_sessions(tmux)
    assert [s.name for s in rows] == ["zeta", "alpha"]       # created 1000 before 1001


def test_live_sessions_skips_records_with_the_wrong_number_of_fields(monkeypatch):
    tmux = fake_tmux("api")
    monkeypatch.setattr(tmux, "run_rc", lambda *_a: (0, "short\nalpha\x1f/tmp\x1f\x1f7\n"))
    assert [s.name for s in tmsnapshot.live_sessions(tmux)] == ["alpha"]


def test_live_sessions_keeps_a_session_whose_directory_name_holds_a_newline(monkeypatch):
    """macOS allows a newline in a directory name and tmux passes #{pane_current_path} through verbatim:
    splitting the output on "\\n" would break that record in two and drop the session for good."""
    tmux = fake_tmux("api")
    out = "api\x1f/tmp/two\nlines\x1f#0a84ff\x1f1000\nweb\x1f/tmp\x1f\x1f1001\n"
    monkeypatch.setattr(tmux, "run_rc", lambda *_a: (0, out))
    rows = tmsnapshot.live_sessions(tmux)
    assert [s.name for s in rows] == ["api", "web"]
    assert rows[0].cwd == "/tmp/two\nlines"      # the exact path, or the restore cannot find the directory


# ---------- the live state (server identity + sessions) ----------
def test_live_state_reads_the_server_identity_and_the_sessions():
    """The identity is what tells a crash from a kill: a new pid means the sessions were not killed."""
    tmux = fake_tmux("api", "web")
    tmux.pid, tmux.started = 9191, 1_790_848_700
    state = tmsnapshot.live_state(tmux)
    assert state.server == tmsnapshot.ServerId(pid=9191, started=1_790_848_700)
    assert [s.name for s in state.sessions] == ["api", "web"]
    assert ("run", "display", "-p", tmsnapshot.SERVER_FMT) in tmux.calls


def test_live_state_asks_tmux_for_the_pid_and_the_start_time():
    assert tmsnapshot.SERVER_FMT == "#{pid}" + tmcore.SEP + "#{start_time}"


def test_live_state_of_a_server_with_no_session_is_not_none():
    """With `exit-empty off` a server stays up with zero sessions, and that honest [] must be written."""
    state = tmsnapshot.live_state(FakeTmux(panes=[]))
    assert state is not None and state.sessions == () and state.server.pid > 0


@pytest.mark.parametrize("missing", ["display", "list-sessions"])
def test_live_state_is_none_when_either_call_fails(missing):
    tmux = fake_tmux("api")
    tmux.fail.add(missing)
    assert tmsnapshot.live_state(tmux) is None


def test_live_state_is_none_when_no_server_is_running():
    tmux = fake_tmux("api")
    tmux.down = True
    assert tmsnapshot.live_state(tmux) is None


@pytest.mark.parametrize("answer", ["", "nonsense\n", "0\x1f123\n", "abc\x1fdef\n", "123\n", "-1\x1f2\n"])
def test_live_state_is_none_when_the_identity_line_makes_no_sense(answer, monkeypatch):
    """A pid of 0 (or a missing field) would compare equal across two servers and silence the restore."""
    tmux = fake_tmux("api")
    monkeypatch.setattr(tmux, "run_rc", lambda *a: (0, answer) if a[0] == "display" else (0, "api\x1f/tmp\x1f\x1f1\n"))
    assert tmsnapshot.live_state(tmux) is None


def test_server_id_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        tmsnapshot.ServerId(pid=1, started=2).pid = 3


# ---------- the snapshot file ----------
def test_saved_session_and_snapshot_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        saved("api").name = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        tmsnapshot.Snapshot(boot=1, saved_at=2).boot = 3


def test_write_then_read_a_snapshot_round_trips(tmp_path):
    path = tmp_path / "state" / "sessions.json"
    snap = tmsnapshot.Snapshot(boot=11, saved_at=22, sessions=(saved("api", color="#0a84ff"), saved("web", created=1001)))
    assert tmsnapshot.write_snapshot(path, snap) is True
    back = tmsnapshot.read_snapshot(path)
    assert back == snap and back.names == ("api", "web")
    assert json.loads(path.read_text())["version"] == tmsnapshot.SNAPSHOT_VERSION


def test_the_snapshot_file_is_private(tmp_path):
    """It lists every project the user works on: 0600 in a 0700 directory, like the rest of the state dir."""
    path = tmp_path / "state" / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),)))
    assert oct(path.stat().st_mode)[-3:] == "600" and oct(path.parent.stat().st_mode)[-3:] == "700"
    assert not list(path.parent.glob("*.tmp"))               # the temp file is renamed, never left behind


def test_write_snapshot_replaces_the_previous_file_atomically(tmp_path):
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),)))
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=3, sessions=()))
    assert tmsnapshot.read_snapshot(path).sessions == ()


def test_write_snapshot_never_reuses_one_temp_path(tmp_path, monkeypatch):
    """A fixed `sessions.json.tmp` is one shared, truncated buffer: two writers (two threads, or an old
    server shutting down while a new one starts) overwrite each other there and rename a mixed file in."""
    path = tmp_path / "sessions.json"
    snap = tmsnapshot.Snapshot(boot=1, saved_at=2, sessions=(saved("api"),))
    seen, real = [], os.replace
    monkeypatch.setattr(tmsnapshot.os, "replace", lambda src, dst: (seen.append(str(src)), real(src, dst))[1])
    assert tmsnapshot.write_snapshot(path, snap) and tmsnapshot.write_snapshot(path, snap)
    assert len(set(seen)) == 2 and str(path) + ".tmp" not in seen
    assert all(name.startswith(str(path) + ".") and name.endswith(".tmp") for name in seen)


def test_write_snapshot_cleans_up_its_temp_file_when_the_rename_fails(tmp_path, monkeypatch):
    def refuse(_src, _dst):
        raise OSError("read-only file system")

    monkeypatch.setattr(tmsnapshot.os, "replace", refuse)
    path = tmp_path / "sessions.json"
    assert tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=2)) is False
    assert not path.exists() and list(tmp_path.glob("*.tmp")) == []


def test_write_snapshot_reports_a_directory_it_cannot_create(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    assert tmsnapshot.write_snapshot(blocker / "sessions.json", tmsnapshot.Snapshot(boot=1, saved_at=2)) is False


@pytest.mark.parametrize("body", [
    "", "not json", "[]", '{"version": 3, "boot": 1, "saved_at": 2, "sessions": []}',
    '{"version": 1, "boot": "x", "saved_at": 2, "sessions": []}',
    '{"version": 1, "boot": 1, "saved_at": 2, "sessions": {}}',
])
def test_read_snapshot_refuses_anything_that_is_not_our_document(tmp_path, body):
    path = tmp_path / "sessions.json"
    path.write_text(body)
    assert tmsnapshot.read_snapshot(path) is None


# ---------- the snapshot carries the server it describes (version 2) ----------
def test_a_snapshot_round_trips_the_server_identity(tmp_path):
    path = tmp_path / "sessions.json"
    snap = tmsnapshot.Snapshot(boot=11, saved_at=22, sessions=(saved("api"),),
                              server=tmsnapshot.ServerId(pid=77, started=1_790_848_700))
    assert tmsnapshot.write_snapshot(path, snap) is True
    assert tmsnapshot.read_snapshot(path) == snap
    written = json.loads(path.read_text())
    assert written["version"] == 2 and written["server"] == {"pid": 77, "started": 1_790_848_700}


def test_a_version_1_file_is_still_read_and_has_no_server(tmp_path):
    """0.5.0 wrote version 1. Upgrading must not throw the list away — it is read with server = None,
    and the boot rule it was written for keeps deciding for it."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": 1, "boot": 7, "saved_at": 8,
                                "sessions": [{"name": "api", "cwd": "/tmp", "color": "", "created": 9}]}))
    snap = tmsnapshot.read_snapshot(path)
    assert snap.names == ("api",) and snap.server is None and snap.boot == 7


@pytest.mark.parametrize("server", [None, {}, {"pid": 7}, {"pid": 0, "started": 7}, {"pid": 7, "started": 0},
                                    {"pid": "7", "started": 7}, {"pid": True, "started": 7}, {"pid": -1, "started": 7},
                                    "7:7", [7, 7]])
def test_a_server_that_is_not_two_positive_ints_is_read_as_unknown(tmp_path, server):
    """The file decides what gets recreated: a half-read identity must fall back to "unknown", never to a
    value that happens to compare equal (or unequal) to the live server."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": 2, "boot": 7, "saved_at": 8, "server": server,
                                "sessions": [{"name": "api", "cwd": "/tmp", "color": "", "created": 9}]}))
    snap = tmsnapshot.read_snapshot(path)
    assert snap is not None and snap.names == ("api",) and snap.server is None


def test_read_snapshot_of_a_missing_file_is_none(tmp_path):
    assert tmsnapshot.read_snapshot(tmp_path / "nope.json") is None


def test_read_snapshot_drops_entries_that_are_not_restorable(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"version": 1, "boot": 1, "saved_at": 2, "sessions": [
        {"name": "api", "cwd": "/tmp", "color": "#0a84ff", "created": 7},
        {"name": "bad name", "cwd": "/tmp", "color": "", "created": 8},
        {"name": "nocwd", "created": 9},
        "junk",
        {"name": "badcolor", "cwd": "/tmp", "color": "red", "created": 10},
    ]}))
    snap = tmsnapshot.read_snapshot(path)
    assert snap.names == ("api", "nocwd", "badcolor")
    assert snap.sessions[1].cwd == "" and snap.sessions[2].color == ""


def test_copy_snapshot_keeps_the_previous_boots_list(tmp_path):
    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=7, saved_at=8, sessions=(saved("api"),)))
    assert tmsnapshot.copy_snapshot(path, previous) is True
    assert tmsnapshot.read_snapshot(previous).boot == 7 and oct(previous.stat().st_mode)[-3:] == "600"
    assert tmsnapshot.copy_snapshot(tmp_path / "gone.json", previous) is False
