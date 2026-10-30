"""A session whose directory is not there at restore time: wait for its volume, then fall back to $HOME
without losing the directory it belongs in (projects often live on a USB volume, /Volumes/Ext here)."""
from fakes import FakeTmux, pane
from helpers_restore import Clock, fake_tmux, saved
from kalmux import tmactions, tmrestore, tmsnapshot
from kalmux.tmcore import INTENDED_OPTION


class Ticker:
    """A monotonic clock that moves only when the code under test sleeps."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


# ---------- which volume a directory needs ----------
def test_volume_root_is_the_mount_point_under_volumes():
    assert tmrestore.volume_root("/Volumes/Ext/Dev/tmux-manager") == "/Volumes/Ext"
    assert tmrestore.volume_root("/Volumes/Ext") == "/Volumes/Ext"
    assert tmrestore.volume_root("/Users/x/Dev") == ""
    assert tmrestore.volume_root("/Volumes/") == "" and tmrestore.volume_root("") == ""


def test_a_session_waits_only_for_a_volume_that_is_not_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: root == "/Volumes/Here")
    assert tmrestore.missing_volume(saved("a", cwd="/Volumes/Away/proj")) == "/Volumes/Away"
    assert tmrestore.missing_volume(saved("b", cwd="/Volumes/Here/moved-away")) == ""   # mounted: just gone
    assert tmrestore.missing_volume(saved("c", cwd=str(tmp_path))) == ""                # it exists
    assert tmrestore.missing_volume(saved("d", cwd="/nowhere/at/all")) == ""            # no volume to wait for


def test_mounted_accepts_a_real_mount_point_and_the_boot_volume_link(tmp_path):
    assert tmrestore._mounted("/") is True
    assert tmrestore._mounted(str(tmp_path)) is False
    link = tmp_path / "Macintosh HD"
    link.symlink_to("/")
    assert tmrestore._mounted(str(link)) is True


# ---------- waiting ----------
def test_wait_for_volumes_returns_as_soon_as_every_volume_is_mounted(monkeypatch):
    t, mounted, lines = Ticker(), set(), []
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: root in mounted)

    def sleep(seconds):
        t.sleep(seconds)
        if t.now >= 3:
            mounted.add("/Volumes/Ext")

    missing = tmrestore.wait_for_volumes({"/Volumes/Ext"}, log=lines.append, timeout=60, poll=1,
                                         sleep=sleep, clock=t.clock)
    assert missing == set() and t.now == 3
    assert "waiting up to 60s for /Volumes/Ext" in lines[0] and "/Volumes/Ext is mounted" in lines[-1]


def test_wait_for_volumes_gives_up_after_its_budget_and_says_so(monkeypatch):
    t, lines = Ticker(), []
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: False)
    missing = tmrestore.wait_for_volumes({"/Volumes/Ext", "/Volumes/Media"}, log=lines.append, timeout=5,
                                         poll=2, sleep=t.sleep, clock=t.clock)
    assert missing == {"/Volumes/Ext", "/Volumes/Media"} and 5 <= t.now <= 6
    assert "did not come up" in lines[-1] and "/Volumes/Ext, /Volumes/Media" in lines[-1]


def test_wait_for_volumes_stops_at_once_on_shutdown(monkeypatch):
    t = Ticker()
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: False)
    missing = tmrestore.wait_for_volumes({"/Volumes/Ext"}, timeout=60, poll=1, sleep=t.sleep, clock=t.clock,
                                         stop=lambda: t.now >= 2)
    assert missing == {"/Volumes/Ext"} and t.now == 2


def test_wait_for_volumes_with_no_budget_does_not_wait_or_talk(monkeypatch):
    t, lines = Ticker(), []
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: False)
    assert tmrestore.wait_for_volumes({"/Volumes/X"}, log=lines.append, timeout=0, sleep=t.sleep,
                                      clock=t.clock) == {"/Volumes/X"}
    assert t.slept == [] and lines == []


# ---------- restore: the ready ones first, then the wait, then the rest ----------
def test_restore_creates_the_ready_sessions_before_it_waits_for_a_volume(tmp_path, monkeypatch):
    mounted, order = set(), []
    monkeypatch.setattr(tmrestore, "_mounted", lambda root: root in mounted)
    usb = tmp_path / "usb-proj"

    def wait(roots, **kw):
        order.append(("wait", tuple(sorted(roots))))
        mounted.add("/Volumes/Ext")
        usb.mkdir()
        return set()

    monkeypatch.setattr(tmrestore, "wait_for_volumes", wait)
    monkeypatch.setattr(tmrestore, "missing_volume",
                        lambda s: "/Volumes/Ext" if s.name == "usb" and "/Volumes/Ext" not in mounted else "")
    tmux = fake_tmux()
    make = tmux.new_session
    tmux.new_session = lambda name, cwd: order.append(("new", name)) or make(name, cwd)
    created = tmrestore.restore(tmux, [saved("usb", cwd=str(usb)), saved("home", cwd=str(tmp_path))],
                                volume_wait=30)
    assert order == [("new", "home"), ("wait", ("/Volumes/Ext",)), ("new", "usb")]
    assert created == ["home", "usb"] and tmux.options.get(("usb", INTENDED_OPTION)) is None


def test_restore_falls_back_to_home_and_remembers_the_intended_directory(tmp_path):
    tmux, lines = fake_tmux(), []
    home = str(tmp_path / "home")
    created = tmrestore.restore(tmux, [saved("reels", cwd="/Volumes/kalmux-absent/projects/reels")],
                                log=lines.append, home=home)
    assert created == ["reels"] and ("new", "reels", home) in tmux.calls
    assert tmux.options[("reels", INTENDED_OPTION)] == "/Volumes/kalmux-absent/projects/reels"
    assert "/Volumes/kalmux-absent/projects/reels is not there" in lines[-1]


def test_restore_with_no_saved_directory_remembers_nothing(tmp_path):
    tmux = fake_tmux()
    tmrestore.restore(tmux, [saved("blank", cwd="")], home=str(tmp_path))
    assert ("blank", INTENDED_OPTION) not in tmux.options


# ---------- the snapshot keeps the intended directory while the session sits in the fallback ----------
def test_recorded_cwd_keeps_the_intended_directory_only_while_the_pane_sits_in_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert tmsnapshot.recorded_cwd(str(tmp_path), "/Volumes/Ext/Dev/x") == "/Volumes/Ext/Dev/x"
    assert tmsnapshot.recorded_cwd("/elsewhere", "/Volumes/Ext/Dev/x") == "/elsewhere"   # the user moved on
    assert tmsnapshot.recorded_cwd(str(tmp_path), "") == str(tmp_path)


def test_the_next_snapshot_records_the_intended_directory_not_home(monkeypatch, tmp_path):
    """A real ui.log of 2026-10-28: a session came back in $HOME and the next snapshot wrote $HOME
    over /Volumes/kalmux-absent2/projects/clips for good."""
    monkeypatch.setenv("HOME", str(tmp_path))
    tmux = fake_tmux()
    tmrestore.restore(tmux, [saved("clips", cwd="/Volumes/kalmux-absent2/projects/clips")], home=str(tmp_path))
    rows = tmsnapshot.live_sessions(tmux)
    assert [(s.name, s.cwd) for s in rows] == [("clips", "/Volumes/kalmux-absent2/projects/clips")]
    path = tmp_path / "sessions.json"
    shot = tmrestore.Snapshotter(path, tmux, boot=1, clock=Clock())
    assert shot.tick() is True and tmsnapshot.read_snapshot(path).sessions[0].cwd.startswith("/Volumes/kalmux-absent2")


def test_a_later_restore_uses_the_original_directory_once_it_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    original = tmp_path / "usb" / "proj"
    first = fake_tmux()
    tmrestore.restore(first, [saved("proj", cwd=str(original))], home=str(tmp_path / "home"))
    recorded = tmsnapshot.live_sessions(first)
    original.mkdir(parents=True)                              # the volume is back by the next reboot
    second = fake_tmux()
    tmrestore.restore(second, recorded)
    assert ("new", "proj", str(original)) in second.calls and ("proj", INTENDED_OPTION) not in second.options


def test_live_sessions_parses_the_intended_field_even_when_a_path_spans_lines():
    tmux = FakeTmux()
    out = "api\x1f/Users/me\x1f/Volumes/X/two\nlines\x1f#0a84ff\x1f1000\nweb\x1f/tmp\x1f\x1f\x1f1001\n"
    tmux.run_rc = lambda *a: (0, out)
    rows = tmsnapshot.live_sessions(tmux, home="/Users/me")
    assert [(r.name, r.cwd) for r in rows] == [("api", "/Volumes/X/two\nlines"), ("web", "/tmp")]


# ---------- /api/state shows the fallback ----------
def test_snapshot_reports_a_session_sitting_in_its_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    tmux = FakeTmux(panes=[pane("reels", path=str(tmp_path), cmd="zsh", attached="0"),
                           pane("api", pane_id="%2", window_id="@2", path="/x/api")])
    tmux.options[("reels", INTENDED_OPTION)] = "/Volumes/Media/reels"
    snap = tmactions.snapshot(tmux, tmp_path / "no-registry", None, now=100)
    rows = {s["name"]: s for s in snap["sessions"]}
    assert rows["reels"]["cwd_fallback"] == "/Volumes/Media/reels" and rows["api"]["cwd_fallback"] == ""
    assert snap["restored"]["fallbacks"] == 1


# ---------- the intended directory is let go once it no longer explains anything ----------
def test_displaced_only_while_the_intended_directory_is_still_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    later = tmp_path / "usb" / "reels"
    panes = [pane("reels", path=str(tmp_path / "home"), intended=str(later))]
    assert tmsnapshot.displaced(panes, home=str(tmp_path / "home")) == {"reels": str(later)}
    later.mkdir(parents=True)                                 # the disk mounted a minute late
    assert tmsnapshot.displaced(panes, home=str(tmp_path / "home")) == {}


def test_settle_forgets_the_intended_directory_once_the_user_moved_the_session(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    tmux = FakeTmux(panes=[pane("reels", path=str(home), attached="0"),
                           pane("api", pane_id="%2", window_id="@2", path=str(home), attached="0")])
    for name in ("reels", "api"):
        tmux.options[(name, INTENDED_OPTION)] = "/Volumes/kalmux-absent3/x"
    k = tmrestore.SessionKeeper(tmux, None, tmp_path / "s.json", tmp_path / "p.json", boot=1)
    k.settle()
    assert (("reels", INTENDED_OPTION) in tmux.options) and (("api", INTENDED_OPTION) in tmux.options)
    tmux.panes[0]["path"] = "/private/tmp"                    # the user cd'd somewhere else in reels
    k.settle()
    assert ("reels", INTENDED_OPTION) not in tmux.options and ("api", INTENDED_OPTION) in tmux.options
    tmux.panes[0]["path"] = str(home)                         # ... and back to ~: ~ is where it now belongs
    assert [s.cwd for s in tmsnapshot.live_sessions(tmux, home=str(home)) if s.name == "reels"] == [str(home)]


def test_only_the_active_pane_decides_that_the_user_moved(monkeypatch, tmp_path):
    home = tmp_path / "home"
    tmux = FakeTmux(panes=[pane("reels", path=str(home), active=True),
                           pane("reels", pane_id="%2", window_id="@2", path="/private/tmp", active=False)])
    tmux.options[("reels", INTENDED_OPTION)] = "/Volumes/kalmux-absent3/x"
    assert tmsnapshot.moved_on(tmux.list_panes(), home=str(home)) == []
    tmux.panes[0]["path"] = "/srv"
    assert tmsnapshot.moved_on(tmux.list_panes(), home=str(home)) == ["reels"]
