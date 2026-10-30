"""A restore recreates the sessions but opens no tab unless [restore] open_tabs says so: every session it
recreated carries a mark in tmux (@kalmux_restored=<epoch>) until it is opened, attached or dismissed."""
import threading

from fakes import FakeIt2, FakeTmux, pane
from helpers_restore import Clock, fake_tmux, saved
from kalmux import tmcore, tmrestore, tmsnapshot
from kalmux.tmcore import RESTORED_OPTION
from test_tmrestore import OLD, keeper, snapshot_file


def marked(tmux, name):
    return tmux.options.get((name, RESTORED_OPTION), "")


# ---------- the mark, as tmux carries it ----------
def test_the_mark_is_a_session_option_every_pane_format_can_read():
    assert tmcore.RESTORED_OPTION == "@kalmux_restored" and "#{@kalmux_restored}" in tmcore.TMUX_FMT
    assert tmcore.INTENDED_OPTION == "@kalmux_cwd" and "#{@kalmux_cwd}" in tmcore.TMUX_FMT
    assert len(tmcore.FIELDS) == len(tmcore.TMUX_FMT.split(tmcore.SEP))


def test_pending_restored_lists_the_marked_sessions_nobody_has_attached():
    panes = [pane("api", restored="1700", attached="0"), pane("api", pane_id="%2", restored="1700", attached="0"),
             pane("web", restored="1800", attached="1"), pane("db", restored="", attached="0"),
             pane("junk", restored="soon", attached="0")]
    assert tmsnapshot.pending_restored(panes) == {"api": 1700}
    assert tmsnapshot.opened_restored(panes) == ["web"]


def test_mark_and_clear_go_through_the_session_option():
    tmux = fake_tmux("api")
    assert tmsnapshot.mark_restored(tmux, "api", 1700) is True and marked(tmux, "api") == "1700"
    assert tmsnapshot.clear_restored(tmux, "api") is True and marked(tmux, "api") == ""
    assert ("set", "api", RESTORED_OPTION, None) in tmux.calls
    assert tmsnapshot.mark_restored(tmux, "nope", 1) is False


# ---------- restore marks what it recreates ----------
def test_restore_marks_every_session_it_recreates_and_says_not_opened(tmp_path):
    tmux, lines = fake_tmux("api"), []
    sessions = [saved("api", cwd=str(tmp_path)), saved("web", cwd=str(tmp_path), color="#0a84ff")]
    created = tmrestore.restore(tmux, sessions, log=lines.append, opening=False, now=1_790_000_123)
    assert created == ["web"]
    assert marked(tmux, "web") == "1790000123" and marked(tmux, "api") == ""      # api was never ours
    assert any("recreated web" in line and "not opened" in line for line in lines)


def test_restore_on_start_opens_no_tab_by_default(tmp_path):
    path = snapshot_file(tmp_path, 100, "api", "web")
    tmux, it2, lines = fake_tmux("api"), FakeIt2(window="pty-CUR"), []
    report = tmrestore.restore_on_start(tmux, it2, path, tmp_path / "prev.json", boot=200, log=lines.append)
    assert report["ran"] is True and report["created"] == ["web"] and report["tabs"] == 0
    assert it2.tabs == [] and it2.new_windows == [] and marked(tmux, "web")
    assert "1 session(s) recreated, not opened" in lines[-1] and "kalmux open --restored" in lines[-1]


def test_restore_on_start_with_tabs_opens_them_and_clears_the_marks_it_opened(tmp_path):
    path = snapshot_file(tmp_path, 100, "web", "db")
    tmux, it2, lines = fake_tmux(), FakeIt2(window="pty-CUR"), []
    report = tmrestore.restore_on_start(tmux, it2, path, tmp_path / "prev.json", boot=200, log=lines.append,
                                        tabs=True)
    assert report["tabs"] == 2 and len(it2.tabs) == 2
    assert marked(tmux, "web") == "" and marked(tmux, "db") == ""
    assert "2 session(s) recreated, 2 tab(s) opened" in lines[-1]


def test_a_tab_that_fails_to_open_keeps_its_mark_for_the_toolbelt(tmp_path):
    path = snapshot_file(tmp_path, 100, "web")
    tmux, it2 = fake_tmux(), FakeIt2(window="pty-CUR")
    it2.fail_tab = True
    tmrestore.restore_on_start(tmux, it2, path, tmp_path / "prev.json", boot=200, tabs=True)
    assert marked(tmux, "web")                                # still waiting for a tab: the banner shows it


# ---------- open_tabs: one window, a lock per tab, a report per name ----------
def test_open_tabs_reports_every_name_under_the_lock_and_opens_the_tabs_outside_it():
    lock, seen = threading.Lock(), []
    it2 = FakeIt2(window="pty-CUR")
    tab = it2.new_tab

    def watched(command, window=""):
        seen.append(lock.locked())
        return tab(command, window)

    it2.new_tab = watched
    reports = []
    assert tmrestore.open_tabs(it2, ["api", "bad name", "web"], lock=lock,
                               on_tab=lambda name, ok: reports.append((name, ok, lock.locked()))) == 2
    assert seen == [False, False] and not lock.locked()
    assert reports == [("api", True, True), ("bad name", False, True), ("web", True, True)]


def test_open_tabs_honours_an_explicit_window():
    it2 = FakeIt2(window="pty-CUR")
    assert tmrestore.open_tabs(it2, ["api"], window="pty-OTHER") == 1 and it2.tabs[0][1] == "pty-OTHER"


def test_open_tabs_stops_between_two_tabs_on_shutdown():
    it2, lines = FakeIt2(window="pty-CUR"), []
    assert tmrestore.open_tabs(it2, ["api", "web"], log=lines.append, stop=lambda: len(it2.tabs) >= 1) == 1
    assert lines == [tmrestore.STOPPED_NOTE]


def test_open_tabs_without_iterm2_reports_every_name_as_not_opened():
    reports = []
    tmrestore.open_tabs(FakeIt2(available=False), ["api", "web"], on_tab=lambda n, ok: reports.append((n, ok)))
    assert reports == [("api", False), ("web", False)]


# ---------- the keeper ----------
def test_keeper_in_flight_restore_follows_open_tabs(tmp_path, capsys):
    tmux, it2 = fake_tmux("api", "web"), FakeIt2(window="pty-CUR")
    k = keeper(tmp_path, tmux=tmux, it2=it2)
    k.startup()
    tmux.new_server(keep=("api",))
    k.run_once()
    k.restorer.join(5)
    assert tmux.has_session("web") and it2.tabs == [] and marked(tmux, "web")
    assert "recreated, not opened" in capsys.readouterr().out


def test_keeper_reads_open_tabs_when_it_restores_not_when_it_is_built(tmp_path):
    tmux, it2, wanted = fake_tmux(), FakeIt2(window="pty-CUR"), {"tabs": False}
    snapshot_file(tmp_path, 200, "web", server=OLD)
    k = tmrestore.SessionKeeper(tmux, it2, tmp_path / "sessions.json", tmp_path / "sessions.previous.json",
                                boot=200, clock=Clock(), tabs=lambda: wanted["tabs"])
    wanted["tabs"] = True                                     # the user edited the config after the start
    k.startup()
    assert len(it2.tabs) == 1 and marked(tmux, "web") == ""


def test_keeper_settle_clears_the_mark_of_a_session_a_client_attached():
    tmux = FakeTmux(panes=[pane("api", attached="1"), pane("web", pane_id="%2", attached="0")])
    tmsnapshot.mark_restored(tmux, "api", 1700)
    tmsnapshot.mark_restored(tmux, "web", 1700)
    k = tmrestore.SessionKeeper(tmux, FakeIt2(), "/nonexistent/sessions.json", "/nonexistent/prev.json", boot=1)
    assert k.settle() == ["api"]
    assert marked(tmux, "api") == "" and marked(tmux, "web") == "1700"


def test_keeper_loop_settles_the_marks_every_round(tmp_path):
    tmux = FakeTmux(panes=[pane("api", attached="1")])
    tmsnapshot.mark_restored(tmux, "api", 1700)
    k = keeper(tmp_path, tmux=tmux, every=0.01)
    thread = k.start()
    for _ in range(200):
        if not marked(tmux, "api"):
            break
        threading.Event().wait(0.01)
    k.stop()
    thread.join(5)
    assert marked(tmux, "api") == ""


def test_open_restored_names_follow_the_order_tmux_lists_them():
    tmux = FakeTmux(panes=[pane("b", attached="0"), pane("a", pane_id="%2", attached="0")])
    for name in ("a", "b"):
        tmsnapshot.mark_restored(tmux, name, 5)
    assert list(tmsnapshot.pending_restored(tmux.list_panes())) == ["b", "a"]
