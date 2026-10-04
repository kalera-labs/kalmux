"""End-to-end tests for the snapshot and the restore against a REAL tmux, on sockets of our own.

SAFETY: every command here goes to `-L kalmux-test-<pid>` or `-L kalmux-crash-<pid>`, never the user's
server, and nothing ever runs `kill-server` — on 2026-09-14 that command, run from a shell that had
inherited $TMUX, took down every Claude session the user had open. These tests kill only the sessions
they created; an empty server with `exit-empty off` is let go by turning the option back on.
"""
import contextlib
import os
import subprocess
import time

import pytest

from fakes import FakeIt2
from helpers_restore import Clock
from kalmux import tmcore, tmrestore, tmsnapshot
from kalmux.tmcore import Tmux, resolve_tmux

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


def _drop_stale_socket(socket: str = SOCKET):
    """Remove OUR socket file once no server answers on it, so a hundred runs do not leave a hundred files.

    Only ever a path this process named (kalmux-test-<pid> / kalmux-crash-<pid>), and only once
    `list-sessions` fails."""
    path = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", socket)
    if socket.startswith(("kalmux-test-", "kalmux-crash-")) and os.path.exists(path) \
            and Tmux(socket=socket).run_rc("list-sessions")[0] != 0:
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
    assert set(tmsnapshot.read_snapshot(path).names) >= set(names)

    for name in names:
        assert private_tmux.kill_session(name)
    assert not private_tmux.has_session(names[0])

    it2 = FakeIt2(window="pty-CUR")
    report = tmrestore.restore_on_start(private_tmux, it2, path, previous, boot=222, log=lambda _m: None)
    assert report["ran"] is True and set(report["created"]) >= set(names)
    live = {s.name: s for s in tmsnapshot.live_sessions(private_tmux)}
    for name, directory, color in zip(names, (first, second), ("#8a9a5b", "#0a84ff"), strict=True):
        assert os.path.realpath(live[name].cwd) == os.path.realpath(directory)
        assert live[name].color == color
    assert tmsnapshot.read_snapshot(previous).boot == 111
    assert [c for c, _w in it2.tabs] == [f"""/bin/zsh -lc 'exec tmux -L {SOCKET} -CC attach -t "={n}"'; exit"""
                                         for n in names]


# ---------- end to end, a tmux server that crashed and came back as another one ----------
CRASH_SOCKET = f"kalmux-crash-{os.getpid()}"


def _start_server(socket: str, name: str, cwd: str) -> None:
    """Start a tmux server on OUR socket with `-f /dev/null` and one detached session in it.

    The config matters: the user's ~/.tmux.conf carries hooks (and, after this release, `exit-empty off`)
    and the test has to own both. $TMUX is dropped as well, like Tmux does for a private socket."""
    subprocess.run([TMUX_BIN, "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", name, "-c", cwd],
                   check=True, capture_output=True, timeout=15,
                   env={k: v for k, v in os.environ.items() if k != "TMUX"})


def _wait_until_no_server(tmux: Tmux, timeout: float = 10.0) -> bool:
    """True once nothing answers on the socket. The server leaves on its own (`exit-empty on` with no
    session left): this suite never kills a tmux server, not even its own (incident 2026-09-14)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tmsnapshot.live_state(tmux) is None:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def crash_tmux():
    """A socket of our own for the crash test. It runs with `exit-empty off`, so the teardown kills the
    sessions it made and then puts the option back: the empty server then exits by itself."""
    tmux = Tmux(socket=CRASH_SOCKET)
    yield tmux
    for line in tmux.run("list-sessions", "-F", "#{session_name}").split("\n"):
        if line.strip():
            tmux.kill_session(line.strip())
    tmux.run_rc("set", "-s", "exit-empty", "on")
    _wait_until_no_server(tmux)
    _drop_stale_socket(CRASH_SOCKET)


@pytest.mark.skipif(not TMUX_BIN, reason="tmux is not installed")
def test_end_to_end_a_crashed_server_is_restored_on_the_next_keeper_round(tmp_path, crash_tmux, capsys):
    """The incident of 2026-10-04, start to finish: two sessions, the server disappears without a reboot,
    a new one takes the socket with one name in it, and the keeper's next round brings the other back."""
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    names = (f"crash-a-{os.getpid()}", f"crash-b-{os.getpid()}")
    _start_server(CRASH_SOCKET, names[0], str(first))
    assert crash_tmux.run_rc("set", "-s", "exit-empty", "off")[0] == 0
    assert crash_tmux.new_session(names[1], str(second))
    for name, color in zip(names, ("#8a9a5b", "#0a84ff"), strict=True):
        assert crash_tmux.set_session_option(name, "@tm_color", color)

    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    it2 = FakeIt2(window="pty-CUR")
    k = tmrestore.SessionKeeper(crash_tmux, it2, path, previous, boot=111, clock=Clock())
    assert k.run_once() is True
    lost = tmsnapshot.read_snapshot(path)
    assert set(lost.names) == set(names) and lost.server.pid > 0

    for name in names:                                       # the sessions go, the server stays (exit-empty off)
        assert crash_tmux.kill_session(name)
    assert tmsnapshot.live_state(crash_tmux) is not None
    assert crash_tmux.run_rc("set", "-s", "exit-empty", "on")[0] == 0        # ... and now it leaves, like a crash
    assert _wait_until_no_server(crash_tmux)
    assert k.run_once() is False                             # nothing is written while nothing answers
    assert tmsnapshot.read_snapshot(path).names == lost.names

    _start_server(CRASH_SOCKET, names[0], str(first))        # the user starts one of them again by hand
    assert crash_tmux.run_rc("set", "-s", "exit-empty", "off")[0] == 0
    assert tmsnapshot.live_state(crash_tmux).server != lost.server
    assert k.run_once() is True
    k.restorer.join(10)                                      # the restore runs on a thread of its own

    back = {s.name: s for s in tmsnapshot.live_state(crash_tmux).sessions}
    assert set(back) == set(names)
    assert os.path.realpath(back[names[1]].cwd) == os.path.realpath(second) and back[names[1]].color == "#0a84ff"
    assert back[names[0]].color == ""                        # the one that was already there is left alone
    kept = tmsnapshot.read_snapshot(previous)
    assert set(kept.names) == set(names) and kept.server == lost.server
    assert [c for c, _w in it2.tabs] == [f"""/bin/zsh -lc 'exec tmux -L {CRASH_SOCKET} -CC attach -t "={names[1]}"'; exit"""]
    assert k.run_once() is True and set(tmsnapshot.read_snapshot(path).names) == set(names)
    assert "restore:" in capsys.readouterr().out


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
