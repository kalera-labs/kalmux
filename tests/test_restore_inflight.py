"""The lost list stays on disk until the restore that brings it back has finished: a stop in the middle
(a volume wait is up to a minute) leaves it for the next start, whatever else ticked the keeper meanwhile."""
import threading
import time

from fakes import FakeIt2, FakeTmux, pane
from helpers_restore import Clock, saved
from kalmux import tmrestore, tmsnapshot

ABSENT = "/Volumes/kalmux-absent-inflight"
OLD = tmsnapshot.ServerId(4, 400)


def keeper(tmp_path, tmux, **kw):
    return tmrestore.SessionKeeper(tmux, FakeIt2(window="pty-CUR"), tmp_path / "sessions.json",
                                   tmp_path / "sessions.previous.json", boot=200, clock=Clock(), **kw)


def two_sessions(tmp_path):
    """api lives in a folder that exists, usb on a volume that is not mounted."""
    return FakeTmux([pane("api", path=str(tmp_path)), pane("usb", pane_id="%2", window_id="@2", path=f"{ABSENT}/p")])


class HeldVolume:
    """wait_for_volumes stand-in: says when the wait began, then waits for the shutdown (or a release)."""

    def __init__(self):
        self.waiting, self.release, self.calls = threading.Event(), threading.Event(), 0

    def __call__(self, roots, log=None, stop=lambda: False, **_kw):
        self.calls += 1
        self.waiting.set()
        while not stop() and not self.release.is_set():
            time.sleep(0.005)
        return set(roots)


# ---------- the snapshotter ----------
def test_a_lost_list_stays_on_disk_until_the_restore_adopts_the_new_server(tmp_path):
    tmux = FakeTmux([pane("api"), pane("web", pane_id="%2", window_id="@2")])
    lost = []
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(),
                                 previous_path=tmp_path / "prev.json", on_lost=lost.append)
    shot.tick()
    old = tmsnapshot.read_snapshot(shot.path).server
    tmux.new_server(keep=("api",))
    assert shot.tick() is False and [s.names for s in lost] == [("api", "web")]
    assert tmsnapshot.read_snapshot(shot.path).server == old            # still the lost list, for the next start
    assert shot.tick(force=True) is False and len(lost) == 1            # held: no second restore, no write
    assert shot.final() is False and tmsnapshot.read_snapshot(shot.path).server == old
    assert shot.adopt() is True
    now = tmsnapshot.read_snapshot(shot.path)
    assert now.server == tmsnapshot.ServerId(tmux.pid, tmux.started) and now.names == ("api",)
    assert tmsnapshot.read_snapshot(tmp_path / "prev.json").names == ("api", "web")
    assert shot.tick() is False and len(lost) == 1                      # the restore is done: never again


def test_adopt_without_a_server_writes_nothing_and_stays_held(tmp_path):
    tmux = FakeTmux([pane("api")])
    shot = tmrestore.Snapshotter(tmp_path / "sessions.json", tmux, boot=1, clock=Clock(), on_lost=lambda _s: None)
    shot.tick()
    tmux.new_server()
    shot.tick()
    tmux.down = True
    assert shot.adopt() is False
    tmux.down = False
    assert shot.tick() is False                                          # still waiting for its adopt


# ---------- the keeper ----------
def test_a_stop_during_the_in_flight_volume_wait_leaves_the_rest_for_the_next_start(tmp_path, monkeypatch):
    tmux = two_sessions(tmp_path)
    held = HeldVolume()
    monkeypatch.setattr(tmrestore, "wait_for_volumes", held)
    k = keeper(tmp_path, tmux)
    k.startup()
    tmux.new_server(keep=())                                             # the iTerm2 crash took tmux down
    assert k.run_once() is False                                         # the round that starts the restore
    assert held.waiting.wait(5) and tmux.has_session("api")
    other = threading.Thread(target=k.run_once)                          # a toolbelt action during the wait
    other.start()
    other.join(5)
    k.stop()
    k.restorer.join(5)
    on_disk = tmsnapshot.read_snapshot(tmp_path / "sessions.json")
    assert on_disk.server != tmsnapshot.ServerId(tmux.pid, tmux.started) and on_disk.names == ("api", "usb")
    monkeypatch.setattr(tmrestore, "wait_for_volumes", lambda roots, **kw: set(roots))
    assert keeper(tmp_path, tmux).startup()["created"] == ["usb"]


def test_a_round_during_the_startup_volume_wait_neither_writes_nor_restores_twice(tmp_path, monkeypatch):
    tmux = FakeTmux([])
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=200, saved_at=1, server=OLD, sessions=(
        saved("api", cwd=str(tmp_path)), saved("usb", cwd=f"{ABSENT}/p", created=1001))))
    held = HeldVolume()
    monkeypatch.setattr(tmrestore, "wait_for_volumes", held)
    k = keeper(tmp_path, tmux, every=60)
    thread = k.start()
    assert held.waiting.wait(5) and tmux.has_session("api")
    assert k.run_once() is False                                         # Backend._kept after a new session
    assert tmsnapshot.read_snapshot(path).server == OLD and k.restorer is None
    k.stop()
    thread.join(5)
    assert tmsnapshot.read_snapshot(path).names == ("api", "usb") and held.calls == 1


def test_the_startup_restore_adopts_the_new_server_once_it_is_done(tmp_path, monkeypatch):
    tmux = FakeTmux([])
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=200, saved_at=1, server=OLD, sessions=(
        saved("api", cwd=str(tmp_path)), saved("usb", cwd=f"{ABSENT}/p", created=1001))))
    monkeypatch.setattr(tmrestore, "wait_for_volumes", lambda roots, **kw: set(roots))
    k = keeper(tmp_path, tmux)
    assert k.startup()["created"] == ["api", "usb"]
    now = tmsnapshot.read_snapshot(path)
    assert now.server == tmsnapshot.ServerId(tmux.pid, tmux.started) and now.names == ("api", "usb")
    assert k.run_once() is False and k.restorer is None


def test_an_in_flight_restore_that_explodes_still_adopts_the_new_server(tmp_path, monkeypatch):
    tmux = FakeTmux([pane("api", path=str(tmp_path))])
    k = keeper(tmp_path, tmux)
    k.startup()

    def boom(*_a, **_k):
        raise RuntimeError("tmux exploded")
    monkeypatch.setattr(tmrestore, "restore", boom)
    tmux.new_server()
    k.run_once()
    k.restorer.join(5)
    assert tmsnapshot.read_snapshot(tmp_path / "sessions.json").server == tmsnapshot.ServerId(tmux.pid, tmux.started)
    tmux.panes.append(pane("web", pane_id="%9"))
    assert k.run_once() is True                                          # snapshots carry on


# ---------- restore() waits only for what it still has to create ----------
def test_restore_waits_for_a_volume_only_when_a_session_there_is_missing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tmrestore, "wait_for_volumes", lambda roots, **kw: calls.append(roots) or set(roots))
    tmux = FakeTmux([pane("usb", path="/x")])
    log = []
    created = tmrestore.restore(tmux, [saved("api", cwd=str(tmp_path)), saved("usb", cwd=f"{ABSENT}/p"),
                                       saved("bad name", cwd=f"{ABSENT}/q")], log=log.append)
    assert created == ["api"] and calls == []
    assert any("usb already exists" in line for line in log) and any("bad name" in line for line in log)


def test_open_or_say_says_nothing_misleading_when_nothing_was_recreated():
    log = []
    assert tmrestore.open_or_say(FakeTmux([]), FakeIt2(), [], tabs=False, log=log.append) == 0
    assert tmrestore.open_or_say(FakeTmux([]), FakeIt2(), [], tabs=True, log=log.append) == 0
    assert log == ["restore: nothing left to recreate"] * 2


def test_a_session_that_appears_during_the_volume_wait_is_left_alone(tmp_path, monkeypatch):
    tmux = FakeTmux([])

    def user_starts_it(roots, **_kw):
        tmux.panes.append(pane("usb", path="/x"))
        return set(roots)
    monkeypatch.setattr(tmrestore, "wait_for_volumes", user_starts_it)
    log = []
    assert tmrestore.restore(tmux, [saved("usb", cwd=f"{ABSENT}/p")], log=log.append) == []
    assert "restore: usb already exists; left alone" in log and not [c for c in tmux.calls if c[0] == "new"]
