"""SIGTERM / SIGINT while the ui server shuts down: the handler only records the signal, everything else
runs once on the main thread after serve_forever() returns (ui.log of 2026-10-28: a second SIGTERM
re-entered the handler, raised "reentrant call inside <_io.BufferedWriter name='<stdout>'>" and printed a
false "a restore is in flight")."""
import io
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

from fakes import FakeIt2, FakeTmux, pane
from helpers_restore import Clock, fake_tmux
from kalmux import tmconfig, tmrestore, tmserver
from test_tmserver import TEMPLATE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SlowKeeper:
    """stop() takes a while, like a final snapshot against a slow tmux; it remembers which thread called."""

    def __init__(self):
        self.threads = []

    def start(self):
        pass

    def run_once(self):
        return True

    def stop(self):
        self.threads.append(threading.current_thread())
        time.sleep(0.2)


def make_backend(tmp_path):
    ui = tmp_path / "index.html"
    ui.write_text(TEMPLATE)
    return tmserver.Backend(FakeTmux(panes=[pane("api")]), FakeIt2(), registry_dir=tmp_path / "none", ui_path=ui,
                            reapply_delay=0, status_dir=tmp_path / "status", trace_dir=tmp_path / "trace",
                            config=tmconfig.load_config(path=tmp_path / "none.toml"))


def start_serving(tmp_path, monkeypatch, keeper, out):
    handlers, ready = {}, {}
    monkeypatch.setattr(tmserver, "build_keeper", lambda b, o: keeper)
    monkeypatch.setattr(tmserver.signal, "signal", handlers.__setitem__)
    thread = threading.Thread(target=tmserver.serve, args=(0, make_backend(tmp_path)), daemon=True,
                              kwargs={"keep_sessions": True, "out": out,
                                      "on_ready": lambda h: ready.setdefault("httpd", h)})
    thread.start()
    for _ in range(200):
        if "httpd" in ready:
            break
        time.sleep(0.01)
    return thread, handlers


def test_two_signals_shut_down_once_and_never_from_inside_the_handler(tmp_path, monkeypatch):
    keeper, out = SlowKeeper(), io.StringIO()
    thread, handlers = start_serving(tmp_path, monkeypatch, keeper, out)
    handler = handlers[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    assert handlers[signal.SIGTERM] is signal.SIG_IGN and handlers[signal.SIGINT] is signal.SIG_IGN
    handler(signal.SIGTERM, None)                             # one more, delivered before the ignore took hold
    thread.join(timeout=5)
    assert not thread.is_alive() and keeper.threads == [thread]       # once, on the serving thread
    assert out.getvalue().count("signal 15, shutting down") == 1


def test_serve_puts_the_previous_signal_handlers_back_when_it_returns(tmp_path, monkeypatch):
    installed = []
    monkeypatch.setattr(tmserver.signal, "signal", lambda sig, fn: installed.append((sig, fn)) or "OLD")
    tmserver.serve(0, make_backend(tmp_path), out=io.StringIO(),
                   on_ready=lambda h: threading.Thread(target=h.shutdown).start())
    assert installed[-2:] == [(signal.SIGTERM, "OLD"), (signal.SIGINT, "OLD")]


def test_a_handler_python_did_not_install_is_not_put_back(tmp_path, monkeypatch):
    installed = []
    monkeypatch.setattr(tmserver.signal, "signal", lambda sig, fn: installed.append((sig, fn)))
    tmserver.serve(0, make_backend(tmp_path), out=io.StringIO(),
                   on_ready=lambda h: threading.Thread(target=h.shutdown).start())
    assert all(fn is not None for _sig, fn in installed) and len(installed) == 2


def test_a_shutdown_without_a_signal_still_stops_the_keeper_once(tmp_path, monkeypatch):
    keeper, out = SlowKeeper(), io.StringIO()
    monkeypatch.setattr(tmserver, "build_keeper", lambda b, o: keeper)
    tmserver.serve(0, make_backend(tmp_path), out=out, keep_sessions=True,
                   on_ready=lambda h: threading.Thread(target=h.shutdown).start())
    assert len(keeper.threads) == 1 and "shutting down" not in out.getvalue()


def test_keeper_stop_is_idempotent_and_says_nothing_the_second_time(tmp_path, capsys):
    k = tmrestore.SessionKeeper(fake_tmux("api"), FakeIt2(), tmp_path / "sessions.json", tmp_path / "prev.json",
                                boot=200, clock=Clock())
    k.stop()
    written = (tmp_path / "sessions.json").stat().st_mtime_ns
    k.stop()
    assert capsys.readouterr().out == "" and (tmp_path / "sessions.json").stat().st_mtime_ns == written


def test_keeper_stop_racing_itself_runs_the_final_snapshot_once(tmp_path):
    k = tmrestore.SessionKeeper(fake_tmux("api"), FakeIt2(), tmp_path / "sessions.json", tmp_path / "prev.json",
                                boot=200, clock=Clock())
    finals = []
    k.snapshotter.final = lambda: finals.append(1) or time.sleep(0.05)
    threads = [threading.Thread(target=k.stop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert finals == [1]


SCRIPT = textwrap.dedent("""
    import faulthandler, os, signal, sys, threading, time
    faulthandler.dump_traceback_later(20, exit=True)     # a hang fails with every thread's stack, not a bare timeout
    print("child up", flush=True)
    sys.path[:0] = [{tests!r}, {src!r}]
    from fakes import FakeIt2, FakeTmux, pane
    from kalmux import tmconfig, tmserver

    class Keeper:
        def start(self): pass
        def run_once(self): return True
        def stop(self):
            print("keeper stop", flush=True)
            time.sleep(0.3)

    tmserver.build_keeper = lambda backend, out: Keeper()
    backend = tmserver.Backend(FakeTmux(panes=[pane("api")]), FakeIt2(), registry_dir={tmp!r}, reapply_delay=0,
                               status_dir={tmp!r}, trace_dir={tmp!r}, config=tmconfig.load_config(path={cfg!r}))

    def twice(_httpd):
        def send():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=send, daemon=True).start()

    sys.exit(tmserver.serve(0, backend, keep_sessions=True, on_ready=twice))
""")


def test_two_real_sigterms_in_a_row_shut_the_server_down_cleanly(tmp_path):
    """The real thing, in a child process: two SIGTERM 0.1 s apart while the shutdown is still running."""
    script = SCRIPT.format(tests=os.path.join(ROOT, "tests"), src=os.path.join(ROOT, "src"), tmp=str(tmp_path),
                           cfg=str(tmp_path / "none.toml"))
    env = {**{k: v for k, v in os.environ.items() if k != "TMUX"}, "KALMUX_STATE_DIR": str(tmp_path / "state")}
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=30, env=env, check=False)
    out, err = p.stdout.decode(), p.stderr.decode()
    assert p.returncode == 0, (out, err)
    assert out.count("shutting down") == 1 and out.count("keeper stop") == 1, out
    assert "reentrant" not in err and "Traceback" not in err, err
