"""A ui server left over from an earlier macOS login session (2026-11-01: a WindowServer crash logged the
user out and back in without a reboot; the server and the tmux server survived in the old session, where
every Apple Event to the new iTerm2 fails, so the toolbelt answered "iTerm2 is not answering")."""
import ctypes
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

from fakes import FakeIt2
from kalmux import cli, tmactions, tmserver, tmsession, tmsetup


# ---------- the audit session ----------
def test_the_auditinfo_layout_matches_bsm_audit_h():
    # struct auditinfo_addr on arm64 and x86_64 macOS: auid 0, mask 4, termid 12, asid 36, flags 40, size 48
    assert ctypes.sizeof(tmsession._AuditInfo) == 48
    assert tmsession._AuditInfo.asid.offset == 36 and tmsession._AuditInfo.flags.offset == 40


@pytest.mark.skipif(sys.platform != "darwin", reason="getaudit_addr is macOS only")
def test_audit_session_reads_this_process_session_on_macos():
    info = tmsession.audit_info()
    assert info is not None and info.auid == os.getuid()
    asid = tmsession.audit_session()
    assert isinstance(asid, int) and asid > 0 and asid == info.asid
    assert tmsession.audit_session() == asid                  # one process, one session


def test_only_a_local_graphic_session_may_replace_a_server():
    assert tmsession.graphic_session(0x6030)                  # console login: graphic, console, tty, authenticated
    assert not tmsession.graphic_session(0x5020)              # ssh: remote, no graphic access
    assert not tmsession.graphic_session(0x6030 | 0x1000)     # remote, even with the graphic bit
    assert not tmsession.graphic_session(None)


def test_audit_session_is_none_away_from_macos(monkeypatch):
    monkeypatch.setattr(tmsession.sys, "platform", "linux")
    assert tmsession.audit_session() is None


# ---------- is the running server the one left over? ----------
def test_a_caller_without_graphic_access_never_replaces_a_server():
    probes = []
    assert not tmsession.left_over({"asid": 100019}, mine=108882, reaches_iterm2=lambda: probes.append(1) or True,
                                   graphic=False)
    assert probes == []                                       # not even asked: ssh must never kill the desk's server


def test_a_server_of_another_session_is_left_over_only_when_this_process_reaches_iterm2():
    probes = []

    def reaches():
        probes.append(1)
        return True

    assert tmsession.left_over({"asid": 100019}, mine=108882, reaches_iterm2=reaches)
    assert not tmsession.left_over({"asid": 100019}, mine=108882, reaches_iterm2=lambda: False)   # ssh, an old tmux pane
    assert not tmsession.left_over({"asid": 108882}, mine=108882, reaches_iterm2=reaches)          # same session
    assert not tmsession.left_over({}, mine=108882, reaches_iterm2=reaches)                        # a pre-0.6.1 server
    assert not tmsession.left_over({"asid": 100019}, mine=None, reaches_iterm2=reaches)            # not macOS
    assert len(probes) == 1                                   # it2 is asked only when the sessions differ


def test_it2_answers_needs_a_successful_call():
    ok, down = FakeIt2(), FakeIt2()
    ok.ping_rc, down.ping_rc = 0, 2
    assert tmsession.it2_answers(ok)
    assert not tmsession.it2_answers(down)
    assert not tmsession.it2_answers(FakeIt2(available=False))


def test_it2_ping_is_one_call(monkeypatch):
    from kalmux.tmcore import It2
    calls = []
    it2 = It2()
    it2.path = "/bin/it2"
    monkeypatch.setattr(it2, "_run_rc", lambda *a: calls.append(a) or (1, "Error: Authentication failed"))
    assert it2.ping() is False and calls == [("session", "list")]


def test_it2_answers_retries_while_iterm2_is_still_starting():
    class Late(FakeIt2):
        calls = 0

        def ping(self):
            Late.calls += 1
            return Late.calls >= 3

    naps = []
    assert tmsession.it2_answers(Late(), tries=5, pause=1.0, sleep=naps.append)
    assert Late.calls == 3 and naps == [1.0, 1.0]
    never = FakeIt2()
    never.ping_rc = 2
    assert not tmsession.it2_answers(never, tries=3, sleep=naps.append) and len(naps) == 4


# ---------- the server says which session it runs in ----------
def test_healthz_carries_the_server_audit_session(monkeypatch, tmp_path):
    monkeypatch.setattr(tmsession, "audit_session", lambda: 4242)
    from fakes import FakeTmux
    backend = tmserver.Backend(FakeTmux(), FakeIt2(), registry_dir=tmp_path / "none", status_dir=tmp_path / "s",
                               trace_dir=tmp_path / "t", config={})
    assert backend.health()["asid"] == 4242


# ---------- `kalmux ui start` (what iTerm2's AutoLaunch runs) replaces a left-over server ----------
def _start(monkeypatch, server, left_over, stop_rc=0):
    calls = []
    monkeypatch.setattr(cli, "health", lambda port: server)
    monkeypatch.setattr(cli, "_left_over", lambda h: left_over)
    monkeypatch.setattr(cli, "_ui_stop", lambda wait=5.0: calls.append("stop") or stop_rc)
    monkeypatch.setattr(tmsetup, "start_server", lambda path: calls.append("start") or True)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli._ui_start()
    return rc, calls, out.getvalue()


def test_ui_start_leaves_a_server_of_this_session_alone(monkeypatch):
    rc, calls, out = _start(monkeypatch, {"pid": 7, "asid": 1}, left_over=False)
    assert rc == 0 and calls == [] and "already running" in out


def test_ui_start_replaces_a_server_left_over_from_an_earlier_login(monkeypatch):
    rc, calls, out = _start(monkeypatch, {"pid": 7, "asid": 1}, left_over=True)
    assert rc == 0 and calls == ["stop", "start"]
    assert "earlier login session" in out and "pid 7" in out


def test_ui_start_gives_up_when_the_left_over_server_does_not_stop(monkeypatch):
    rc, calls, _out = _start(monkeypatch, {"pid": 7, "asid": 1}, left_over=True, stop_rc=1)
    assert rc == 1 and calls == ["stop"]


def test_ui_stop_reports_a_server_that_is_still_answering(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_pid", lambda: 7)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(cli, "health", lambda port: {"pid": 7})
    assert cli._ui_stop(wait=0.05) == 1
    assert "still answering" in capsys.readouterr().err


# ---------- doctor ----------
def _ui_checks(h, left_over):
    checks = tmsetup.Checks()
    tmsetup._check_ui(checks, h, tmsetup.STATE_DIR, lambda: (True, "ok"), left_over=left_over)
    return {c["name"]: c for c in checks}


def test_doctor_flags_a_server_from_an_earlier_login_session():
    h = {"pid": 7, "tmux": "/opt/homebrew/bin/tmux", "version": tmsetup.VERSION, "asid": 1}
    bad = _ui_checks(h, left_over=lambda h: True)[tmsetup.SESSION_CHECK]
    assert not bad["ok"] and "kalmux ui restart" in bad["remedy"]
    assert _ui_checks(h, left_over=lambda h: False)[tmsetup.SESSION_CHECK]["ok"]


# ---------- the toolbelt says what to do ----------
def test_the_iterm2_silent_answer_names_the_way_out():
    class DownMap:
        def refresh(self, force=False):
            return {}

        def is_down(self):
            return True

    from fakes import FakeTmux, pane
    res = tmactions.action_go(FakeTmux(panes=[pane("api")]), FakeIt2(), DownMap(), "api")
    assert not res.ok and "kalmux ui restart" in res.message and "api" in res.message


def test_the_statusline_path_does_not_load_ctypes():
    import subprocess
    code = "import sys; from kalmux import tmsetup; print('tmsession' in sys.modules, '_ctypes' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")})
    assert out.stdout.split() == ["False", "False"]
