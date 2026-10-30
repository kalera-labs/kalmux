"""tmserver — the local HTTP server behind the iTerm2 toolbelt web UI (`kalmux ui serve`).

Security model: binds 127.0.0.1 only; every /api call must carry the per-process CSRF token that is
embedded in the page; Host and Origin headers are pinned to the loopback origin (DNS rebinding);
the page runs under a nonce-based CSP, so no inline handlers or external resources.
"""
from __future__ import annotations

import html
import http.client
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import tmhooks, tmrestore
from .tmactions import (
    Result,
    TabMap,
    action_color,
    action_detach,
    action_dismiss_restored,
    action_forget,
    action_go,
    action_kill,
    action_new,
    action_open,
    action_rename,
    action_resume,
    find_trail,
    next_waiting,
    reapply,
    reapply_later,
    snapshot,
    trails_named,
)
from .tmconfig import config_path, load_config
from .tmcore import (
    ASSETS,
    DEFAULT_REGISTRY,
    PALETTE,
    VERSION,
    It2,
    Tmux,
    load_registry,
    resumable_trails,
)
from .tmsnapshot import clear_restored, pending_restored

APP_NAME = "kalmux"
LEGACY_APP_NAMES = ("kmux", "tmux-manager")   # a server started before a rename must stay stoppable
UI_PATH = ASSETS / "index.html"
MAX_BODY = 64 * 1024
UI_PALETTE = {k: v for k, v in PALETTE.items() if k != "grey"}
ACTIONS = ("go", "open", "color", "new", "kill", "rename", "detach", "next-waiting", "reapply", "resume", "forget",
           "open-restored", "dismiss-restored", "repair-hooks")
IDLE_OPENING = {"running": False, "done": 0, "failed": 0, "total": 0}
log = logging.getLogger("kalmux.ui")


def render_page(template: str, token: str, nonce: str, palette: dict) -> str:
    return (template.replace("__TM_NONCE__", nonce)
            .replace("__TM_TOKEN__", html.escape(token, quote=True))
            .replace("__TM_PALETTE__", html.escape(json.dumps(palette, separators=(",", ":")), quote=True)))


def csp(nonce: str) -> str:
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def state_dirs() -> tuple[Path, Path]:
    # lazy: tmsetup imports this module, so importing it at the top would loop
    from .tmsetup import STATUS_DIR, TRACE_DIR
    return STATUS_DIR, TRACE_DIR


def default_hook_health() -> tmhooks.HookHealth:
    """Hook health over the installed paths (lazy for the same reason as state_dirs())."""
    from .tmsetup import DEFAULT_SETTINGS, STATE_DIR, WRAPPER
    return tmhooks.HookHealth(DEFAULT_SETTINGS, WRAPPER, STATE_DIR)


def default_repair_hooks() -> str:
    """The UI's Repair button: ONLY the idempotent "Claude Code hooks" setup step, nothing else of setup."""
    from .tmsetup import DEFAULT_HOOK_LINK, DEFAULT_SETTINGS, STATE_DIR, WRAPPER
    return tmhooks.wire_claude_hooks(DEFAULT_SETTINGS, WRAPPER, STATE_DIR, hook_link=DEFAULT_HOOK_LINK)


def state_files() -> tuple[Path, Path]:
    """(sessions.json, sessions.previous.json) — lazy for the same reason as state_dirs()."""
    from .tmsetup import PREVIOUS_SESSIONS_FILE, SESSIONS_FILE
    return SESSIONS_FILE, PREVIOUS_SESSIONS_FILE


class Backend:
    """Everything the handler needs, injectable for tests (fake tmux / it2 / ui file / state dirs)."""

    def __init__(self, tmux: Tmux, it2: It2, registry_dir: Path = DEFAULT_REGISTRY, ui_path: Path = UI_PATH,
                 reapply_delay: float = 2.0, status_dir: Path | None = None, trace_dir: Path | None = None,
                 config: dict | None = None, hooks: tmhooks.HookHealth | None = None, repair_hooks=None) -> None:
        self.tmux, self.it2, self.registry_dir, self.ui_path = tmux, it2, registry_dir, ui_path
        self.hooks = hooks if hooks is not None else default_hook_health()
        self.repair_hooks = repair_hooks or default_repair_hooks
        default_status, default_trace = state_dirs()
        self.status_dir = Path(default_status if status_dir is None else status_dir)
        self.trace_dir = Path(default_trace if trace_dir is None else trace_dir)
        self._injected_config = config
        self._config_key: tuple | None = None
        self._config_cache: dict = {}
        self.tabmap = TabMap(it2)
        self.token = secrets.token_urlsafe(24)
        self.started_at = int(time.time())
        self.reapply_delay = reapply_delay
        self.lock = threading.Lock()
        # set by serve(keep_sessions=True) only: a Backend built in a test keeps no sessions and starts
        # no thread, so nothing of ours ever writes the real state directory from a test run.
        self.keeper: tmrestore.SessionKeeper | None = None
        # "Open all": the thread opening the restored sessions' tabs, and its progress for /api/state.
        # The progress dict is replaced whole, never edited, so a poll reads one consistent picture.
        self.opener: threading.Thread | None = None
        self.opening: dict = dict(IDLE_OPENING)
        self.opening_now = ""                  # the session whose tab the batch is opening this very moment
        self.out = None                        # where ui.log lines go (serve() sets it; None = stdout)

    @property
    def config(self) -> dict:
        """The user's config, re-read whenever the file changes: editing it must not need a server restart.

        The server is long-lived (an AutoLaunch daemon), and a stale `keep_days` is not cosmetic — it decides
        which trail files `load_trails` DELETES. Cached on (path, mtime, size) so a poll stays one stat()."""
        if self._injected_config is not None:
            return self._injected_config
        path = config_path()
        try:
            st = path.stat()
            key: tuple = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = (str(path), None, None)       # no file (or unreadable): the defaults, re-checked next call
        if key != self._config_key:
            self._config_cache, self._config_key = load_config(path), key
        return self._config_cache

    @property
    def keep_days(self) -> int:
        return self.config["tombstones"]["keep_days"]

    def hook_status(self) -> dict:
        """{ok, last_seen, reason}: do Claude Code's hooks still reach Kalmux? (see tmhooks)"""
        return self.hooks.check(load_registry(self.registry_dir))

    def snapshot(self, hook: dict | None = None) -> dict:
        hook = hook if hook is not None else self.hook_status()
        return snapshot(self.tmux, self.registry_dir, self.tabmap, status_dir=self.status_dir,
                        trace_dir=self.trace_dir, keep_days=self.keep_days, prune_trails=hook["ok"])

    def health(self) -> dict:
        # `tmux` tells `tm doctor` whether THIS process can see tmux (its PATH may differ from the shell's)
        return {"app": APP_NAME, "version": VERSION, "pid": os.getpid(), "started_at": self.started_at,
                "tmux": getattr(self.tmux, "path", "")}

    def state(self) -> dict:
        hook = self.hook_status()
        snap = self.snapshot(hook)
        snap["hook"] = hook
        installed = self.it2.available()
        # available = iTerm2 is answering right now, not merely installed (the UI shows it as a liveness badge)
        snap["iterm2"] = {"available": installed and not self.tabmap.is_down(), "installed": installed, "current_window": ""}
        snap["server"] = {"version": VERSION, "started_at": self.started_at}
        snap["restored"] = {**snap["restored"], "opening": dict(self.opening)}
        return snap

    def page(self, nonce: str) -> str:
        return render_page(self.ui_path.read_text(encoding="utf-8"), self.token, nonce, UI_PALETTE)

    def _after_open(self, session: str) -> None:
        if self.reapply_delay > 0:
            reapply_later(self.tmux, session, self.reapply_delay)
        else:
            reapply(self.tmux, session)

    def _kept(self, r: Result) -> Result:
        """Snapshot right after an action that changed the session list, instead of up to 5 s later: a
        session created (or killed) just before a shutdown must not be missing from the saved list."""
        if r.ok and self.keeper is not None:
            self.keeper.run_once()
        return r

    def _open_new(self, r: Result, body: dict) -> Result:
        """Show a freshly created (or resumed) session in iTerm2 unless the caller opted out."""
        if r.ok and bool(body.get("open", True)):
            opened = action_open(self.tmux, self.it2, r.data["name"])
            r.data["open"] = opened.as_dict()
            if opened.ok:
                self._after_open(r.data["name"])
        return r

    def _resume(self, query: str) -> Result:
        # resumable_trails, never load_trails: a conversation that is still running is not a resume candidate
        registry = load_registry(self.registry_dir)
        trails = resumable_trails(self.trace_dir, registry, int(time.time()), self.keep_days,
                                  self.hooks.check(registry)["ok"])
        trail = find_trail(trails, query)
        if trail is None:
            return Result(False, f"no dead session matches {query!r}")
        return action_resume(self.tmux, self.config, trail, registry, matches=len(trails_named(trails, query)))

    def say(self, message: str) -> None:
        print(message, file=self.out or sys.stdout, flush=True)

    def _open_restored(self) -> Result:
        """Open All: a tab for every session a restore recreated and nobody has opened yet, all in the
        current iTerm2 window, on a thread of its own like the restore. Called under self.lock, which the
        batch takes per tab (around the tmux calls only), so other clicks are served in between."""
        if self.opening["running"]:
            return Result(False, "already opening the restored sessions", dict(self.opening))
        names = list(pending_restored(self.tmux.list_panes()))
        if not names:
            return Result(False, "no restored session is waiting for a tab")
        if not self.it2.available():
            return Result(False, "iTerm2 (it2) not available here")
        self.opening = {**IDLE_OPENING, "running": True, "total": len(names)}
        self.opener = threading.Thread(target=self._open_batch, args=(names,), name="kalmux-open-restored",
                                       daemon=True)
        self.opener.start()
        return Result(True, f"opening {len(names)} restored session(s)", {"total": len(names), "names": names})

    def _open_batch(self, names: list[str]) -> None:
        pending = tmrestore.still_pending(self.tmux)

        def claim(name: str) -> bool:                      # runs under self.lock, right before each tab
            if pending(name):
                self.opening_now = name
                return True
            # opened by a click, dismissed or attached meanwhile: not this batch's to open any more
            self.opening = {**self.opening, "total": self.opening["total"] - 1}
            return False

        def on_tab(name: str, ok: bool) -> None:            # runs under self.lock (open_tabs takes it)
            self.opening_now = ""
            if ok:
                clear_restored(self.tmux, name)
                self._after_open(name)
            key = "done" if ok else "failed"
            self.opening = {**self.opening, key: self.opening[key] + 1}

        try:
            tmrestore.open_tabs(self.it2, names, socket=getattr(self.tmux, "socket", ""), log=self.say,
                                lock=self.lock, on_tab=on_tab, claim=claim)
        except Exception as exc:  # noqa: BLE001 - a failed batch must still end, and say why
            self.say(f"open: {type(exc).__name__}: {exc}")
        finally:
            self.opening, self.opening_now = {**self.opening, "running": False}, ""
        self.say(f"open: {self.opening['done']} of {self.opening['total']} restored session(s) opened")

    def _repair_hooks(self) -> Result:
        try:
            note = self.repair_hooks()
        except (OSError, ValueError) as exc:
            return Result(False, f"could not repair the hooks: {exc}")
        hook = self.hook_status()
        # an entry Repair could write is fixed now; what is left (a session that never loaded them) is not
        still = "" if hook["ok"] else f". The hooks are still not reaching Kalmux: {hook['reason']}"
        return Result(True, f"Claude Code hooks: {note}{still}", {"hook": hook})

    def do(self, action: str, body: dict) -> Result:
        session = body.get("session", "")
        with self.lock:
            if action in ("go", "open") and session and session == self.opening_now:
                return Result(False, f"Open all is opening {session} right now")
            if action == "go":
                r = action_go(self.tmux, self.it2, self.tabmap, session)
                if r.ok and r.data.get("opened"):
                    self._after_open(session)
                return r
            if action == "open":
                r = action_open(self.tmux, self.it2, session, body.get("window", ""))
                if r.ok:
                    self._after_open(session)
                return r
            if action == "color":
                return action_color(self.tmux, session, body.get("color", ""))
            if action == "new":
                r = action_new(self.tmux, body.get("name", ""), body.get("cwd", ""), body.get("color", ""),
                               bool(body.get("start_claude", False)), self.config["claude"]["new"])
                return self._open_new(self._kept(r), body)
            if action == "resume":
                return self._open_new(self._kept(self._resume(body.get("session_id", "") or session)), body)
            if action == "forget":
                return action_forget(self.trace_dir, body.get("session_id", ""), self.status_dir)
            if action == "repair-hooks":
                return self._repair_hooks()
            if action == "kill":
                return self._kept(action_kill(self.tmux, session))
            if action == "rename":
                return self._kept(action_rename(self.tmux, session, body.get("name", "")))
            if action == "detach":
                return action_detach(self.tmux, session)
            if action == "reapply":
                return reapply(self.tmux, session or None)
            if action == "open-restored":
                return self._open_restored()
            if action == "dismiss-restored":
                return action_dismiss_restored(self.tmux, session)
            if action == "next-waiting":
                name = next_waiting(self.snapshot()["sessions"])
                if not name:
                    return Result(False, "no session is waiting for you right now")
                r = action_go(self.tmux, self.it2, self.tabmap, name)
                r.data["session"] = name
                return r
        return Result(False, f"unknown action {action!r}")


class Handler(BaseHTTPRequestHandler):
    server_version = f"kalmux/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ---- plumbing
    @property
    def backend(self) -> Backend:
        return self.server.backend  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.address_string(), *args)

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _origin_ok(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in self._allowed_hosts():
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in self._allowed_hosts()}

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-TM-Token", ""), self.backend.token)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:            # tell the client too, or it will pipeline into a socket we are closing
            self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        # allow_nan=False: NaN / Infinity are not JSON, and the page's JSON.parse() would throw on them
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except ValueError as exc:
            log.warning("dropped a response holding a value JSON cannot represent: %s", exc)
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            raw = b'{"ok": false, "message": "internal error: unrepresentable value in the response", "data": {}}'
        self._send(status, raw, "application/json; charset=utf-8")

    def _fail(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "message": message, "data": {}})

    def _refuse(self, status: int, message: str) -> None:
        """Reject a request whose body was NOT read: close the connection, or the unread bytes would be
        parsed as the next request on this keep-alive socket."""
        self.close_connection = True
        self._fail(status, message)

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True      # unread body would poison a kept-alive connection
            return None
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ---- routes
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._origin_ok():
            return self._fail(HTTPStatus.FORBIDDEN, "bad host/origin")
        path = self.path.split("?", 1)[0]
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            try:
                page = self.backend.page(nonce)
            except OSError as exc:
                return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"ui file missing: {exc}")
            return self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8",
                              {"Content-Security-Policy": csp(nonce)})
        if path == "/healthz":
            return self._json(HTTPStatus.OK, self.backend.health())
        if path == "/favicon.ico":
            return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        if path == "/api/state":
            if not self._token_ok():
                return self._fail(HTTPStatus.FORBIDDEN, "bad token")
            try:
                return self._json(HTTPStatus.OK, {"ok": True, "message": "", "data": self.backend.state()})
            except Exception as exc:  # never take the UI down because one poll failed
                log.exception("state failed")
                return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"state failed: {type(exc).__name__}: {exc}")
        return self._fail(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        if not self._origin_ok():
            return self._refuse(HTTPStatus.FORBIDDEN, "bad host/origin")
        if not self._token_ok():
            return self._refuse(HTTPStatus.FORBIDDEN, "bad token")
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._refuse(HTTPStatus.NOT_FOUND, "not found")
        action = path[len("/api/"):]
        if action not in ACTIONS:
            return self._refuse(HTTPStatus.NOT_FOUND, f"unknown action {action!r}")
        body = self._read_json_body()
        if body is None:
            return self._fail(HTTPStatus.BAD_REQUEST, "body must be a JSON object under 64 KiB")
        try:
            result = self.backend.do(action, body)
        except Exception as exc:
            log.exception("action %s failed", action)
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"{action} failed: {type(exc).__name__}: {exc}")
        return self._json(HTTPStatus.OK if result.ok else HTTPStatus.BAD_REQUEST, result.as_dict())


# ----------------------------------------------------------------------------- lifecycle
def health(port: int, timeout: float = 1.0) -> dict | None:
    """The /healthz document of a server on this port, or None when nothing (of ours) answers.

    http.client on purpose, not urllib: urllib honours the system / env HTTP proxy even for 127.0.0.1
    (macOS system proxies do not bypass loopback), and a proxy that is down made a live server look dead."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8")) if resp.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return data if isinstance(data, dict) and data.get("app") in (APP_NAME, *LEGACY_APP_NAMES) else None


def make_server(port: int, backend: Backend) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.backend = backend  # type: ignore[attr-defined]
    return httpd


def build_keeper(backend: Backend, out) -> tmrestore.SessionKeeper:
    """Start the thread that restores the previous boot's sessions and then keeps the list up to date.

    Built from the real state files, so only `kalmux ui serve` ever asks for one (keep_sessions=True)."""
    path, previous = state_files()
    # open_tabs is read when a restore happens, from the live config: editing it needs no restart
    keeper = tmrestore.SessionKeeper(backend.tmux, backend.it2, path, previous,
                                     enabled=backend.config["restore"]["enabled"], lock=backend.lock, out=out,
                                     tabs=lambda: bool(backend.config["restore"].get("open_tabs", False)))
    backend.keeper = keeper
    keeper.start()
    return keeper


def serve(port: int, backend: Backend | None = None, pidfile: Path | None = None, out=None, on_ready=None,
          keep_sessions: bool = False) -> int:
    out = out or sys.stdout
    backend = backend or Backend(Tmux(), It2())
    try:
        httpd = make_server(port, backend)
    except OSError as exc:
        if health(port):
            print(f"kalmux ui: already running on http://127.0.0.1:{port}/", file=out)
            return 0
        print(f"kalmux ui: cannot bind 127.0.0.1:{port}: {exc}", file=out)
        return 1
    backend.out = out
    # after the bind, so a second instance that found the port taken never restores anything
    keeper = build_keeper(backend, out) if keep_sessions else None
    if pidfile:
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(os.getpid()))
        except OSError as exc:
            print(f"kalmux ui: could not write pidfile {pidfile}: {exc}", file=out)

    received: list[int] = []
    previous = _install_stop_handlers(httpd, received)
    print(f"kalmux ui: listening on http://127.0.0.1:{port}/ (pid {os.getpid()})", file=out, flush=True)
    tmux_path = getattr(backend.tmux, "path", None)
    if tmux_path is not None:
        print(f"kalmux ui: tmux = {tmux_path or 'NOT FOUND (PATH=' + os.environ.get('PATH', '') + ')'}", file=out, flush=True)
    if on_ready:
        on_ready(httpd)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        _shut_down(httpd, keeper, received, pidfile, out)
        _restore_handlers(previous)
    return 0


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _install_stop_handlers(httpd: ThreadingHTTPServer, received: list[int]) -> dict:
    """SIGTERM / SIGINT stop the server; returns the handlers they replaced.

    The handler does as little as Python allows: it ignores any further stop signal FIRST (a pending one
    is then dropped, never run nested), records the number and wakes a thread that calls shutdown(). No
    print and no snapshot in there: a second SIGTERM used to re-enter the handler half-way through its
    print ("reentrant call inside <_io.BufferedWriter name='<stdout>'>") and through the final snapshot.
    Everything else runs once, on this thread, after serve_forever() returns (_shut_down)."""
    wake = threading.Event()

    def stop(signum, _frame):
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        received.append(signum)
        wake.set()

    def shutdown_when_woken() -> None:
        wake.wait()
        httpd.shutdown()        # from a thread of its own: shutdown() waits for serve_forever() to return

    previous = {}
    for sig in STOP_SIGNALS:
        try:
            previous[sig] = signal.signal(sig, stop)
        except ValueError:          # not the main thread (tests): rely on on_ready/shutdown instead
            pass
    if previous:
        threading.Thread(target=shutdown_when_woken, name="kalmux-stop", daemon=True).start()
    return previous


def _restore_handlers(previous: dict) -> None:
    """Put back what serve() replaced, so an in-process caller keeps its own Ctrl-C. A handler Python did
    not install reads back as None and cannot be re-installed: that one is left as it is."""
    for sig, handler in previous.items():
        if handler is not None:
            signal.signal(sig, handler)


def _shut_down(httpd: ThreadingHTTPServer, keeper, received: list[int], pidfile: Path | None, out) -> None:
    """The shutdown proper, on the serving thread and exactly once, whatever stopped serve_forever()."""
    if received:
        print(f"kalmux ui: signal {received[0]}, shutting down", file=out, flush=True)
    if keeper:
        keeper.stop()            # one last snapshot while tmux is still there to answer
    httpd.server_close()
    if pidfile:
        try:
            pidfile.unlink()
        except OSError:
            pass
