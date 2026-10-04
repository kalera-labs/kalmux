"""Tests for src/kalmux/tmrestore.py: the keeper that watches the tmux server, and the restore that
runs when the server holding the saved sessions turns out to be gone.

Everything runs against the fakes and tmp_path, except the end-to-end tests at the bottom, which drive a
REAL tmux on a private socket (`-L kalmux-test-<pid>` / `-L kalmux-crash-<pid>`) and kill only the
sessions they created — never a server: the empty one is let go with `exit-empty on` (incident
2026-09-14, when `tmux kill-server` took down every Claude session the user had open).
"""
import threading
import time

import pytest

from fakes import FakeIt2, FakeTmux, pane
from helpers_restore import Clock, fake_tmux, saved
from kalmux import tmrestore, tmsnapshot


# ---------- the snapshotter ----------
def test_snapshotter_writes_only_when_the_session_list_changed(tmp_path):
    tmux = fake_tmux("api")
    clock = Clock()
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=42, clock=clock)
    assert shot.tick() is True
    assert tmsnapshot.read_snapshot(shot.path).names == ("api",) and tmsnapshot.read_snapshot(shot.path).boot == 42
    clock.now += 5
    assert shot.tick() is False                              # same list: no write, no churn on the disk
    tmux.panes.append(pane("web", pane_id="%9", path="/tmp/web"))
    assert shot.tick() is True and tmsnapshot.read_snapshot(shot.path).names == ("api", "web")
    assert tmsnapshot.read_snapshot(shot.path).saved_at == int(clock.now)


def test_snapshotter_can_be_forced_to_write_an_unchanged_list(tmp_path):
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.tick() is True and shot.tick() is False and shot.tick(force=True) is True


def test_snapshotter_never_writes_while_no_server_answers(tmp_path):
    """0.5.0 waited 30 s and then wrote []. An iTerm2 crash takes tmux down with the sessions still in it,
    so a silent server is never "the user has no sessions": nothing is written until one answers again."""
    tmux = fake_tmux("api")
    clock = Clock()
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=clock)
    shot.tick()
    tmux.down = True
    for _ in range(20):
        clock.now += 60
        assert shot.tick() is False and shot.tick(force=True) is False
    assert tmsnapshot.read_snapshot(shot.path).names == ("api",)
    tmux.down = False
    assert shot.tick() is False                              # the same list, from the same server
    assert not any(hasattr(m, "UNREACHABLE_GRACE") for m in (tmrestore, tmsnapshot))   # gone, not merely unused


def test_snapshotter_writes_an_empty_list_a_live_server_reports(tmp_path):
    """`exit-empty off` keeps the server up after the last session is killed: that [] is the truth."""
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    shot.tick()
    tmux.kill_session("api")
    assert shot.tick() is True and tmsnapshot.read_snapshot(shot.path).names == ()


def test_snapshotter_records_the_server_the_list_came_from(tmp_path):
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.tick() is True
    assert tmsnapshot.read_snapshot(shot.path).server == tmsnapshot.ServerId(tmux.pid, tmux.started)


def test_snapshotter_writes_again_when_only_the_server_changed(tmp_path):
    """Same names, another server: the file must stop claiming the dead one or the doctor (and the next
    `kalmux restore`) would keep reporting a restore that is already done."""
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.tick() is True and shot.tick() is False
    tmux.new_server(keep=("api",))
    assert shot.tick() is True
    assert tmsnapshot.read_snapshot(shot.path).server == tmsnapshot.ServerId(tmux.pid, tmux.started)


def test_snapshotter_keeps_the_lost_list_before_writing_one_from_another_server(tmp_path):
    tmux = fake_tmux("api", "web")
    previous = tmp_path / "sessions.previous.json"
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(), previous_path=previous)
    shot.tick()
    tmux.new_server()                                        # the crash: another server, none of the names
    assert shot.tick() is True
    assert tmsnapshot.read_snapshot(previous).names == ("api", "web")
    assert tmsnapshot.read_snapshot(shot.path).names == ()


def test_snapshotter_keeps_no_copy_while_the_same_server_answers(tmp_path):
    tmux = fake_tmux("api", "web")
    previous = tmp_path / "sessions.previous.json"
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(), previous_path=previous)
    shot.tick()
    tmux.kill_session("web")                                 # really killed: nothing to keep, nothing to bring back
    assert shot.tick() is True and not previous.exists()


def test_snapshotter_keeps_no_copy_for_a_list_without_an_identity_on_the_same_boot(tmp_path):
    """A version-1 file (0.5.0) has no server in it, so it can never equal the live one — but on the same
    boot nothing was lost, and the copy `kalmux restore` falls back to must survive the first 0.5.1 round."""
    tmux = fake_tmux("api")
    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=200, saved_at=1, sessions=(saved("api"),)))
    tmsnapshot.write_snapshot(previous, tmsnapshot.Snapshot(boot=100, saved_at=1,
                                                            sessions=(saved("lost-a"), saved("lost-b"))))
    shot = tmrestore.Snapshotter(path, tmux, boot=200, clock=Clock(), previous_path=previous)
    assert shot.tick(force=True) is True                     # what startup() does right after restore_on_start
    assert tmsnapshot.read_snapshot(previous).names == ("lost-a", "lost-b")


def test_snapshotter_keeps_a_copy_of_a_list_without_an_identity_from_an_older_boot(tmp_path):
    """The same file after a reboot: the boot rule is all a version-1 list has, and it says this one is lost."""
    tmux = fake_tmux("api")
    path, previous = tmp_path / "sessions.json", tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=100, saved_at=1, sessions=(saved("gone"),)))
    shot = tmrestore.Snapshotter(path, tmux, boot=200, clock=Clock(), previous_path=previous)
    assert shot.tick(force=True) is True
    assert tmsnapshot.read_snapshot(previous).names == ("gone",)


def test_snapshotter_hands_a_list_whose_server_is_gone_to_the_callback(tmp_path):
    """The keeper's in-flight restore: tmux died, the kalmux server lived on, a new tmux appeared."""
    tmux = fake_tmux("api", "web")
    lost = []
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(),
                                 previous_path=tmp_path / "prev.json", on_lost=lost.append)
    shot.tick()
    tmux.new_server(keep=("api",))                           # the user started one of them again by hand
    shot.tick()
    assert [s.names for s in lost] == [("api", "web")]
    shot.tick()
    assert len(lost) == 1                                    # the stored server is the live one now: once only


def test_snapshotter_calls_back_only_when_a_name_is_actually_missing(tmp_path):
    tmux = fake_tmux("api")
    lost = []
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(), on_lost=lost.append)
    shot.tick()
    tmux.new_server(keep=("api",))                           # everything came back under the same names
    assert shot.tick() is True and lost == []


def test_snapshotter_calls_back_for_nothing_an_empty_or_version_1_list_cannot_prove(tmp_path):
    """No sessions saved, or a 0.5.0 file with no identity: the boot rule at startup owns those cases."""
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=2, sessions=(saved("web"),)))
    lost = []
    shot = tmrestore.Snapshotter(path, fake_tmux("api"), boot=1, clock=Clock(), on_lost=lost.append)
    assert shot.tick() is True and lost == []                # no identity saved: never guessed at
    empty = tmp_path / "empty.json"
    tmsnapshot.write_snapshot(empty, tmsnapshot.Snapshot(boot=1, saved_at=2, server=tmsnapshot.ServerId(1, 2)))
    other = tmrestore.Snapshotter(empty, fake_tmux("api"), boot=1, clock=Clock(), on_lost=lost.append)
    assert other.tick() is True and lost == []


def test_snapshotter_reads_the_stored_snapshot_when_it_is_built(tmp_path):
    """The crash the keeper has to catch happens BEFORE its first round: a Snapshotter that only learns
    the list it wrote itself would see no change at all on the round after a server restart."""
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=1, saved_at=2, sessions=(saved("api"), saved("web")),
                                                      server=tmsnapshot.ServerId(pid=11, started=22)))
    lost = []
    shot = tmrestore.Snapshotter(path, fake_tmux("api"), boot=1, clock=Clock(), on_lost=lost.append)
    assert shot.tick() is True
    assert [s.names for s in lost] == [("api", "web")]


def test_snapshotter_does_not_hold_its_lock_while_the_callback_restores(tmp_path):
    """Backend.do takes the server lock, then _kept -> run_once takes this one. A callback that restored
    under this lock would take the two in the other order, and two threads in that order deadlock."""
    tmux = fake_tmux("api", "web")
    seen = []
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(),
                                 on_lost=lambda _s: seen.append(shot._lock.locked()))
    shot.tick()
    tmux.new_server()
    shot.tick()
    assert seen == [False]


def test_snapshotter_writes_the_new_list_before_it_calls_back(tmp_path):
    """The file is the keeper's "already handled" mark: a callback that ran first (and restored, and
    ticked again from the HTTP thread) would be handed the same list twice."""
    tmux = fake_tmux("api", "web")
    on_disk = []
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(),
                                 on_lost=lambda _s: on_disk.append(tmsnapshot.read_snapshot(shot.path).server))
    shot.tick()
    tmux.new_server()
    shot.tick()
    assert on_disk == [tmsnapshot.ServerId(tmux.pid, tmux.started)]


def test_snapshotter_final_snapshot_needs_a_live_tmux(tmp_path):
    tmux = fake_tmux("api")
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock())
    assert shot.final() is True and tmsnapshot.read_snapshot(shot.path).names == ("api",)
    tmux.down = True
    assert shot.final() is False
    assert tmsnapshot.read_snapshot(shot.path).names == ("api",)      # the dying server never empties the list


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

    def run_rc(self, *args):
        if args and args[0] == "display":
            return 0, "4242\x1f1790000000\n"                 # one server, every round: only the list flips
        self._n += 1
        return 0, self._long if self._n % 2 else self._short


def test_snapshotter_rounds_never_overlap(tmp_path):
    """A round reads tmux and then writes what it read. Two of them inside each other set _last from one
    list while another list is what actually reached the file, and the difference is never written again."""
    busy, peak, guard = [0], [0], threading.Lock()

    class CountingTmux:
        socket = ""

        def run_rc(self, *args):
            with guard:
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            time.sleep(0.002)
            with guard:
                busy[0] -= 1
            return 0, "4242\x1f1790000000\n" if args[0] == "display" else "api\x1f/tmp\x1f\x1f1\n"

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
            if path.exists() and tmsnapshot.read_snapshot(path) is None:
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
    assert tmsnapshot.read_snapshot(path) is not None


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
def snapshot_file(tmp_path, boot, *names, cwd=None, server=None):
    path = tmp_path / "sessions.json"
    sessions = tuple(saved(n, cwd=cwd or str(tmp_path), created=1000 + i) for i, n in enumerate(names))
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=boot, saved_at=1, sessions=sessions, server=server))
    return path


# ---------- the one rule that decides a restore ----------
def stored_snap(*names, boot=100, server=None):
    return tmsnapshot.Snapshot(boot=boot, saved_at=1, sessions=tuple(saved(n) for n in names), server=server)


def live(server=(5, 500), *names):
    return tmsnapshot.LiveState(server=tmsnapshot.ServerId(*server), sessions=tuple(saved(n) for n in names))


OLD, NEW = tmsnapshot.ServerId(4, 400), tmsnapshot.ServerId(5, 500)


@pytest.mark.parametrize("stored,state,boot,due,why", [
    (None, live(), 200, False, "no file at all"),
    (stored_snap(boot=100, server=OLD), live(), 200, False, "the saved list is empty"),
    (stored_snap("api", server=OLD), None, 200, True, "no tmux server is running"),
    (stored_snap("api", server=OLD), live(), 200, True, "another server holds the socket"),
    (stored_snap("api", server=NEW), live(), 200, False, "the saved server IS the live one"),
    (stored_snap("api", server=NEW), live(), 0, False, "same server, boot unknown and irrelevant"),
    (stored_snap("api", boot=100), live(), 200, True, "version 1: a new boot is all there is to go on"),
    (stored_snap("api", boot=200), live(), 200, False, "version 1, same boot: the user killed them"),
    (stored_snap("api", boot=0), live(), 200, False, "version 1, boot unknown: never guess"),
    (stored_snap("api", boot=100), live(), 0, False, "version 1, this boot unknown: never guess"),
    (stored_snap("api", boot=100), None, 0, True, "nothing is running: the boot does not matter"),
])
def test_restore_due_is_one_rule_with_no_hidden_cases(stored, state, boot, due, why):
    assert tmrestore.restore_due(stored, state, boot) is due, why


def test_restore_on_start_recreates_the_previous_boots_sessions(tmp_path):
    path = snapshot_file(tmp_path, 100, "api", "web")
    previous = tmp_path / "sessions.previous.json"
    tmux, it2 = fake_tmux("api"), FakeIt2(window="pty-CUR")
    lines = []
    report = tmrestore.restore_on_start(tmux, it2, path, previous, boot=200, log=lines.append)
    assert report["ran"] is True and report["created"] == ["web"] and report["tabs"] == 1
    assert tmux.has_session("web")
    assert tmsnapshot.read_snapshot(previous).names == ("api", "web")      # kept before anything is touched


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
    assert tmsnapshot.read_snapshot(previous).names == ("web",) and "enabled" in report["reason"]


def test_restore_on_start_brings_back_the_sessions_of_a_server_that_crashed(tmp_path):
    """The 0.5.0 miss: iTerm2 was force-quit, tmux went down with it, no reboot. Same boot, other server."""
    tmux = fake_tmux("api")
    path = snapshot_file(tmp_path, 200, "api", "web", server=tmsnapshot.ServerId(tmux.pid - 1, tmux.started - 60))
    previous = tmp_path / "sessions.previous.json"
    report = tmrestore.restore_on_start(tmux, FakeIt2(window="pty-CUR"), path, previous, boot=200)
    assert report["ran"] is True and report["created"] == ["web"] and report["tabs"] == 1
    assert tmsnapshot.read_snapshot(previous).names == ("api", "web")


def test_restore_on_start_brings_them_back_when_no_server_is_running_at_all(tmp_path):
    tmux = fake_tmux()
    path = snapshot_file(tmp_path, 200, "web", server=tmsnapshot.ServerId(99, 1000))
    tmux.down = True                                         # `tmux new-session` will start the next server
    report = tmrestore.restore_on_start(tmux, FakeIt2(), path, tmp_path / "prev.json", boot=200)
    assert report["ran"] is True and report["created"] == ["web"]


def test_restore_on_start_leaves_alone_a_list_the_live_server_wrote(tmp_path):
    """The identity beats the boot time in both directions: `kalmux ui restart` must restore nothing,
    and nor must a boot number that disagrees with a server that is demonstrably still the right one."""
    tmux = fake_tmux("api")
    path = snapshot_file(tmp_path, 100, "api", "web", server=tmsnapshot.ServerId(tmux.pid, tmux.started))
    previous = tmp_path / "sessions.previous.json"
    report = tmrestore.restore_on_start(tmux, FakeIt2(), path, previous, boot=200)
    assert report["ran"] is False and not tmux.has_session("web") and not previous.exists()


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
    snap = tmsnapshot.read_snapshot(k.snapshotter.path)
    assert snap.boot == 200 and snap.names == ("api", "web")     # a later `ui restart` must not restore again


def test_keeper_startup_writes_an_empty_list_when_there_is_nothing_at_all(tmp_path):
    k = keeper(tmp_path, tmux=FakeTmux(panes=[]))
    k.startup()
    assert tmsnapshot.read_snapshot(k.snapshotter.path).names == ()


def test_keeper_run_once_and_stop_take_snapshots(tmp_path):
    tmux = fake_tmux("api")
    k = keeper(tmp_path, tmux=tmux)
    assert k.run_once() is True and k.run_once() is False
    tmux.panes.append(pane("web", pane_id="%9", path="/tmp/web"))
    assert k.run_once() is True
    tmux.kill_session("web")
    k.stop()
    assert tmsnapshot.read_snapshot(k.snapshotter.path).names == ("api",)


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


# ---------- the keeper's in-flight restore (tmux died, the ui server lived on) ----------
def test_keeper_restores_in_flight_when_another_tmux_server_takes_the_socket(tmp_path, capsys):
    tmux, it2 = fake_tmux("api", "web"), FakeIt2(window="pty-CUR")
    k = keeper(tmp_path, tmux=tmux, it2=it2)
    k.startup()                                              # the keeper was already running: this is our list
    tmux.new_server(keep=("api",))                           # tmux died; one session was started again by hand
    assert k.run_once() is True
    k.restorer.join(5)                                       # the restore runs on a thread of its own
    assert tmux.has_session("web") and [w for _c, w in it2.tabs] == ["pty-CUR"]
    assert tmsnapshot.read_snapshot(tmp_path / "sessions.previous.json").names == ("api", "web")
    out = capsys.readouterr().out
    assert "restore:" in out and "web" in out and "api" in out


def test_keeper_restores_in_flight_only_once(tmp_path):
    tmux = fake_tmux("api", "web")
    k = keeper(tmp_path, tmux=tmux)
    k.startup()
    tmux.new_server()
    k.run_once()
    k.restorer.join(5)
    made = [c for c in tmux.calls if c[0] == "new"]
    for _ in range(3):
        k.run_once()
    assert [c for c in tmux.calls if c[0] == "new"] == made and len(made) == 2


def test_keeper_startup_and_the_in_flight_round_never_restore_the_same_list(tmp_path):
    """startup() restores first and then writes the new server's list, so the round that follows sees a
    stored server that IS the live one and has nothing left to bring back."""
    tmux = fake_tmux("api")
    snapshot_file(tmp_path, 200, "api", "web", server=tmsnapshot.ServerId(tmux.pid - 1, tmux.started - 60))
    k = keeper(tmp_path, tmux=tmux)
    k.startup()
    made = [c for c in tmux.calls if c[0] == "new"]
    k.run_once()
    k.run_once()
    assert made == [("new", "web", str(tmp_path))] and [c for c in tmux.calls if c[0] == "new"] == made


def test_keeper_with_the_automatic_restore_off_only_keeps_the_lost_list(tmp_path, capsys):
    tmux = fake_tmux("api", "web")
    k = keeper(tmp_path, tmux=tmux, enabled=False)
    k.startup()
    tmux.new_server()
    assert k.run_once() is True
    assert not tmux.has_session("api") and not tmux.has_session("web")
    assert tmsnapshot.read_snapshot(tmp_path / "sessions.previous.json").names == ("api", "web")
    out = capsys.readouterr().out                            # a list lost without a word is the 0.5.0 bug
    assert "enabled = false" in out and "2 session(s)" in out and str(tmp_path / "sessions.previous.json") in out


def test_keeper_survives_an_in_flight_restore_that_explodes(tmp_path, monkeypatch, capsys):
    """The callback runs inside a snapshot round, which runs inside the keeper thread AND inside an HTTP
    request: an exception there must never take either of them down."""
    tmux = fake_tmux("api", "web")
    k = keeper(tmp_path, tmux=tmux)
    k.startup()

    def boom(*_a, **_k):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(tmrestore, "restore", boom)
    tmux.new_server()
    assert k.run_once() is True                              # the snapshot itself still happened
    k.restorer.join(5)
    assert "tmux exploded" in capsys.readouterr().out


def test_an_in_flight_restore_under_the_backend_lock_does_not_hang(tmp_path):
    """Backend.do holds the server lock around _kept() -> run_once(), and threading.Lock is not reentrant:
    a restore that asked for that lock again, on that same thread, would wedge the whole ui server."""
    backend_lock = threading.Lock()
    tmux = fake_tmux("api", "web")
    k = keeper(tmp_path, tmux=tmux, lock=backend_lock)
    k.startup()
    tmux.new_server()
    done = threading.Event()

    def http_request():
        with backend_lock:                                   # exactly what Backend.do holds while it calls in
            k.run_once()
        done.set()

    threading.Thread(target=http_request, daemon=True).start()
    assert done.wait(10), "the in-flight restore waited for a lock the calling thread already holds"
    k.restorer.join(5)
    assert tmux.has_session("web")


def test_the_in_flight_restore_leaves_the_requests_thread_and_takes_the_server_lock_itself(tmp_path):
    """A round is ticked from an HTTP handler that holds the server lock, and a restore is seconds of it2
    calls (6 s each when iTerm2 is wedged, up to 200 sessions): it gets a thread of its own, so the POST
    returns at once and nothing queues behind the lock, and that thread — holding no caller lock — takes
    the server's own around the tmux calls, exactly like the startup path does."""
    backend_lock = threading.Lock()
    tmux, it2 = fake_tmux("api", "web"), FakeIt2(window="pty-CUR")
    seen, caller = {}, threading.current_thread().name
    make, tab = tmux.new_session, it2.new_tab

    def watched_new_session(name, cwd):
        seen["locked_while_creating"] = backend_lock.locked()
        return make(name, cwd)

    def watched_new_tab(command, window=""):
        seen["tab_thread"], seen["locked_while_opening_tabs"] = threading.current_thread().name, backend_lock.locked()
        return tab(command, window)

    tmux.new_session, it2.new_tab = watched_new_session, watched_new_tab
    k = keeper(tmp_path, tmux=tmux, it2=it2, lock=backend_lock)
    k.startup()
    tmux.new_server(keep=("api",))
    with backend_lock:                                       # exactly what Backend.do holds while it calls in
        assert k.run_once() is True                          # ... and the request is done here, tabs or not
    k.restorer.join(5)
    assert not k.restorer.is_alive() and tmux.has_session("web")
    assert seen["tab_thread"] != caller and seen["locked_while_opening_tabs"] is False
    assert seen["locked_while_creating"] is True             # the tmux calls stay exclusive with Backend.do


def test_an_in_flight_restore_handed_over_after_the_shutdown_creates_nothing(tmp_path):
    """httpd.shutdown() lets the request already in flight finish, so a round — and the restore it hands
    over — can still be reached after SIGTERM. The thread that picks it up must touch nothing."""
    tmux, it2 = fake_tmux("api", "web"), FakeIt2(window="pty-CUR")
    k = keeper(tmp_path, tmux=tmux, it2=it2)
    k.startup()
    k.stop()                                                 # SIGTERM: the final snapshot is taken here
    tmux.new_server()
    assert k.run_once() is True                              # the last request in flight, still snapshotting
    k.restorer.join(5)
    assert not tmux.has_session("api") and not tmux.has_session("web") and it2.tabs == []


def test_a_restore_cut_short_by_the_shutdown_leaves_the_saved_list_for_the_next_start(tmp_path):
    """`kalmux ui restart` lands while the startup restore is half-way through A, B, C. The thread dies
    with the interpreter, so nothing may record A as this server's whole list: the saved one stays lost
    and the next start finishes the job."""
    tmux, it2 = fake_tmux(), FakeIt2(window="pty-CUR")
    path = snapshot_file(tmp_path, 200, "A", "B", "C", server=OLD)
    k = keeper(tmp_path, tmux=tmux, it2=it2)
    make = tmux.new_session

    def new_session_then_sigterm(name, cwd):
        ok = make(name, cwd)
        k.stop()                                             # the signal handler, on the main thread
        return ok

    tmux.new_session = new_session_then_sigterm
    k._loop()                                                # what the keeper thread runs, start to finish
    tmux.new_session = make
    assert [c for c in tmux.calls if c[0] == "new"] == [("new", "A", str(tmp_path))] and it2.tabs == []
    saved_list = tmsnapshot.read_snapshot(path)
    assert saved_list.server == OLD and saved_list.names == ("A", "B", "C")
    assert keeper(tmp_path, tmux=tmux, it2=it2).startup()["created"] == ["B", "C"]


# ---------- picking a list for `kalmux restore` ----------
def test_restore_source_prefers_the_file_the_server_has_not_rewritten_yet(tmp_path):
    path = snapshot_file(tmp_path, 100, "api")
    previous = tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(previous, tmsnapshot.Snapshot(boot=50, saved_at=1, sessions=(saved("ancient"),)))
    snap, source = tmrestore.restore_source(path, previous, live(), boot=200)
    assert snap.names == ("api",) and source == path         # the server has not started since the reboot


def test_restore_source_takes_sessions_json_when_its_server_is_not_the_live_one(tmp_path):
    """A crash with no reboot: sessions.json still names the dead server, so it IS the lost list."""
    path = snapshot_file(tmp_path, 200, "api", server=OLD)
    previous = tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(previous, tmsnapshot.Snapshot(boot=200, saved_at=1, sessions=(saved("ancient"),)))
    snap, source = tmrestore.restore_source(path, previous, live(), boot=200)
    assert snap.names == ("api",) and source == path


def test_restore_source_falls_back_to_the_copy_once_the_live_server_owns_the_file(tmp_path):
    path = snapshot_file(tmp_path, 200, "api", server=NEW)   # already rewritten by the server that runs now
    previous = tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(previous, tmsnapshot.Snapshot(boot=100, saved_at=1, sessions=(saved("web"),)))
    snap, source = tmrestore.restore_source(path, previous, live(), boot=200)
    assert snap.names == ("web",) and source == previous


def test_restore_source_falls_back_to_the_previous_boots_copy(tmp_path):
    path = snapshot_file(tmp_path, 200, "api")               # already rewritten for this boot
    previous = tmp_path / "sessions.previous.json"
    tmsnapshot.write_snapshot(previous, tmsnapshot.Snapshot(boot=100, saved_at=1, sessions=(saved("web"),)))
    snap, source = tmrestore.restore_source(path, previous, live(), boot=200)
    assert snap.names == ("web",) and source == previous


def test_restore_source_finds_nothing_when_there_is_nothing(tmp_path):
    assert tmrestore.restore_source(tmp_path / "a.json", tmp_path / "b.json", live(), boot=200) == (None, None)


# ---------- doctor ----------
def test_snapshot_health_is_green_when_the_file_matches_the_live_sessions(tmp_path):
    path = snapshot_file(tmp_path, 200, "api", "web", server=NEW)
    ok, info = tmrestore.snapshot_health(path, boot=200, live=live((5, 500), "web", "api"), now=10)
    assert ok is True and "2 session(s)" in info


def test_snapshot_health_flags_a_missing_stale_or_disagreeing_file(tmp_path):
    missing = tmrestore.snapshot_health(tmp_path / "none.json", boot=200, live=live(), now=10)
    assert missing[0] is False and "missing" in missing[1]
    path = snapshot_file(tmp_path, 100, "api")
    stale = tmrestore.snapshot_health(path, boot=200, live=live((5, 500), "api"), now=10)
    assert stale[0] is False and "boot" in stale[1]
    drifted = tmrestore.snapshot_health(snapshot_file(tmp_path, 200, "api"), boot=200,
                                        live=live((5, 500), "api", "web"), now=10)
    assert drifted[0] is False and "web" in drifted[1]


def test_snapshot_health_fails_when_the_saved_list_belongs_to_a_server_that_is_gone(tmp_path):
    """The restore the keeper could not run (or has not run yet) is a red line in `kalmux doctor`, not a
    file that merely looks fresh: the names agree, and the list is still the dead server's."""
    path = snapshot_file(tmp_path, 200, "api", server=OLD)
    ok, info = tmrestore.snapshot_health(path, boot=200, live=live((5, 500), "api"), now=10)
    assert ok is False and "restore pending" in info and "4" in info


def test_snapshot_health_reports_the_pending_restore_while_no_tmux_server_runs(tmp_path):
    """The one moment `kalmux doctor` is asked where the sessions went: a saved list under a server that
    is gone, and nothing running. That is a restore waiting to happen, not a healthy file."""
    path = snapshot_file(tmp_path, 200, "api", server=OLD)
    ok, info = tmrestore.snapshot_health(path, boot=200, live=None, now=61)
    assert ok is False and "restore pending" in info and "no tmux server is running" in info and "1m" in info


def test_snapshot_health_is_green_with_nothing_saved_and_no_tmux_running(tmp_path):
    """An empty list is nothing to bring back, so a machine with no tmux at all is not a red doctor."""
    path = snapshot_file(tmp_path, 200, server=OLD)
    ok, info = tmrestore.snapshot_health(path, boot=200, live=None, now=10)
    assert ok is True and "0 session(s)" in info


def test_snapshot_health_without_a_readable_boot_time_only_checks_the_names(tmp_path):
    path = snapshot_file(tmp_path, 100, "api")
    assert tmrestore.snapshot_health(path, boot=0, live=live((5, 500), "api"), now=10)[0] is True
