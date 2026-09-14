#!/usr/bin/env python3
"""Mock server for the Kalmux UI (development only).

    python3 scripts/dev/mock_server.py [port]      # default 47399

Serves ui/index.html (placeholders filled in and the same CSP as the real server) and fakes every
/api/* route from data held in RAM. Actions really mutate that data, so the UI can be clicked through
end to end. NOT for production.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "src" / "kalmux" / "assets" / "index.html"

TOKEN = secrets.token_hex(16)
PALETTE = {          # the 20 names of lib/tmcore.PALETTE (without the "grey" alias)
    "red": "#ff453a", "orange": "#ff9500", "yellow": "#ffd60a", "lime": "#a3e635", "green": "#30d158",
    "mint": "#66d4cf", "teal": "#40c8e0", "cyan": "#22d3ee", "blue": "#0a84ff", "indigo": "#5e5ce6",
    "purple": "#bf5af2", "magenta": "#ff4fd8", "pink": "#ff375f", "coral": "#ff6f61", "brown": "#ac8e68",
    "gold": "#d4af37", "olive": "#8a9a5b", "slate": "#6c7a89", "gray": "#8e8e93", "white": "#f2f2f7",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ORDER = {"waiting": 0, "working": 1, "unknown": 2, "idle": 3, "stale": 4, "gone": 5, "busy": 6, "": 7}
STARTED = int(time.time())
LOCK = threading.RLock()
_seq = [0]


def guid() -> str:
    _seq[0] += 1
    return f"{secrets.token_hex(4).upper()}-58DD-4ADD-9F17-{_seq[0]:012d}"


def pane(pid: str, win: str, **kw) -> dict:
    p = {
        "pane": f"{win}.{pid}", "window_id": win, "pane_id": pid, "claude": True,
        "state": "idle", "source": "hook", "detail": "", "title": "", "age": 0,
        "stale": False, "path": "", "guid": "", "iterm2_window": "", "iterm2_tab": "", "active": True,
    }
    p.update(kw)
    return p


def _now() -> int:
    return int(time.time())


def seed() -> list:
    t = _now()
    return [
        {
            "name": "api", "color": "#0a84ff", "attached": 1, "in_iterm2": True,
            "ctx_pct": 42, "model": "Opus 5", "cost_usd": 1.23,
            "state": "working", "since": t - 12, "source": "hook", "detail": "Bash",
            "title": "\u2733 Rate limiting for the public endpoints", "path": "~/code/api",
            "project": "api",
            "panes": [pane("%0", "@0", state="working", detail="Bash",
                           title="\u2733 Rate limiting for the public endpoints", path="~/code/api",
                           guid="174CDA7D-58DD-4ADD-9F17-7D0CBC0682D7",
                           iterm2_window="pty-13D0AA91", iterm2_tab="2")],
        },
        {
            "name": "web", "color": "#bf5af2", "attached": 1, "in_iterm2": True,
            "ctx_pct": 71, "model": "Sonnet 5", "cost_usd": 0.42,
            "state": "waiting", "since": t - 47, "source": "hook", "detail": "Allow Edit?",
            "title": "\u2733 Dark mode for the settings screen",
            "path": "~/code/web", "project": "web",
            "panes": [pane("%0", "@0", state="waiting", detail="Allow Edit?",
                           title="\u2733 Dark mode for the settings screen",
                           path="~/code/web",
                           guid="9F2C11AB-58DD-4ADD-9F17-7D0CBC0682D7",
                           iterm2_window="pty-13D0AA91", iterm2_tab="5")],
        },
        {
            "name": "worker", "color": "#30d158", "attached": 1, "in_iterm2": True,
            "ctx_pct": 93, "model": "Opus 5", "cost_usd": 7.8,
            "state": "working", "since": t - 2340, "source": "hook",
            "detail": "Edit src/queue/retry.ts",
            "title": "\u2733 Make the retry queue idempotent",
            "path": "~/code/worker", "project": "worker",
            "panes": [
                pane("%0", "@1", state="working", detail="Edit src/queue/retry.ts",
                     title="\u2733 Make the retry queue idempotent", age=2340, stale=True,
                     path="~/code/worker", guid="C31D7742-58DD-4ADD-9F17-7D0CBC0682D7",
                     iterm2_window="pty-13D0AA91", iterm2_tab="7"),
                pane("%1", "@1", state="waiting", detail="Allow Bash(pytest -q)?",
                     title="Run the tests again to be sure", age=18,
                     path="~/code/worker", guid="C31D7743-58DD-4ADD-9F17-7D0CBC0682D7",
                     iterm2_window="pty-13D0AA91", iterm2_tab="7"),
            ],
        },
        {
            "name": "docs", "color": "", "attached": 2, "in_iterm2": False,
            "ctx_pct": 12, "model": "Haiku 4.5", "cost_usd": 0.03,
            "state": "idle", "since": t - 410, "source": "registry",
            "detail": "\u2713 Done: the API reference is regenerated", "title": "",
            "path": "~/code/docs", "project": "docs",
            "panes": [pane("%0", "@0", state="idle", source="registry",
                           detail="\u2713 Done: the API reference is regenerated",
                           path="~/code/docs")],
        },
        {
            "name": "spike", "color": "#ff453a", "attached": 0, "in_iterm2": False,
            "state": "gone", "since": t - 7320, "source": "registry",
            "detail": "", "title": "An experiment, abandoned", "path": "~/code/spike",
            "project": "spike",
            "panes": [pane("%0", "@0", state="gone", source="registry", path="~/code/spike")],
        },
        {
            # no Claude here: a plain command occupies the pane (tmux reports the foreground process)
            "name": "infra", "color": "#40c8e0", "attached": 1, "in_iterm2": True,
            "state": "busy", "since": None, "source": "", "detail": "terraform", "title": "",
            "path": "~/code/infra", "project": "infra",
            "panes": [pane("%0", "@0", claude=False, state="busy", source="", detail="terraform",
                           path="~/code/infra",
                           guid="7A1B33CD-58DD-4ADD-9F17-7D0CBC0682D7",
                           iterm2_window="pty-13D0AA91", iterm2_tab="9")],
        },
        {
            "name": "dotfiles", "color": "", "attached": 0, "in_iterm2": False,
            "state": "", "since": None, "source": "hook", "detail": "", "title": "",
            "path": "~/.dotfiles", "project": ".dotfiles",
            "panes": [pane("%0", "@0", claude=False, state="", path="~/.dotfiles")],
        },
    ]


def seed_tombstones() -> list:
    t = _now()
    return [
        {"session_id": "11111111-2222-3333-4444-555555555555", "tmux_session": "importer", "pane": "%0",
         "cwd": "~/code/api", "color": "#0a84ff", "started": t - 9000, "last_ts": t - 1800,
         "last_event": "Stop", "end_reason": "", "last_message": "\u2713 Ported the importer, tests green",
         "ended": "killed", "project": "api"},
        {"session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "tmux_session": "notes", "pane": "%0",
         "cwd": "~/code/notes", "color": "", "started": t - 40000, "last_ts": t - 26000,
         "last_event": "SessionEnd", "end_reason": "logout", "last_message": "Wrapped up the weekly notes",
         "ended": "clean", "project": "notes"},
    ]


QUOTA = {"five_hour": {"used_pct": 64, "resets_at": _now() + 3600},
         "seven_day": {"used_pct": 22, "resets_at": _now() + 4 * 86400}}
SESSIONS = seed()
TOMBSTONES = seed_tombstones()


# ---------------------------------------------------------------- snapshot
def snapshot() -> dict:
    t = _now()
    out = []
    for s in sorted(SESSIONS, key=lambda x: (ORDER.get(x["state"], 9), x["name"])):
        age = None if s["since"] is None else max(0, t - s["since"])
        panes = []
        for p in s["panes"]:
            q = dict(p)
            q["age"] = (p["age"] if p.get("age") is not None else age) if q["state"] else None
            panes.append(q)
        out.append({
            "name": s["name"], "color": s["color"], "attached": s["attached"],
            "in_iterm2": s["in_iterm2"], "state": s["state"], "age": age,
            "stale": bool(s["state"] in ("working", "waiting") and age and age > 1800),
            "source": s["source"], "detail": s["detail"], "title": s["title"],
            "path": s["path"], "project": s["project"], "panes": panes,
            "claude_session_id": s.get("claude_session_id", ""), "ctx_pct": s.get("ctx_pct"),
            "model": s.get("model", ""), "cost_usd": s.get("cost_usd"), "quota": QUOTA if s.get("model") else None,
        })
    counts = dict.fromkeys(("waiting", "working", "idle", "busy", "unknown", "stale", "gone"), 0)
    for s in out:
        if s["state"] in counts:
            counts[s["state"]] += 1
    counts["total"] = len(out)
    return {
        "now": t,
        "iterm2": {"available": True, "installed": True, "current_window": "pty-041DDC06"},
        "server": {"version": "0.4.0-mock", "started_at": STARTED},
        "counts": counts,
        "sessions": out,
        "quota": QUOTA,
        "tombstones": list(TOMBSTONES),
    }


def find(name):
    return next((s for s in SESSIONS if s["name"] == name), None)


def attach_tab(s) -> None:
    """Pretend to open a new iTerm2 tab for the session."""
    s["in_iterm2"] = True
    s["attached"] += 1
    for p in s["panes"]:
        if not p["guid"]:
            p["guid"] = guid()
            p["iterm2_window"] = "pty-041DDC06"
            p["iterm2_tab"] = str(8 + _seq[0])


# ---------------------------------------------------------------- actions
def act_go(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No session named {!r}".format(b.get("session", "")), {}
    if s["in_iterm2"]:
        return 200, True, "Jumped to {}".format(s["name"]), {"opened": False}
    attach_tab(s)
    return 200, True, "Opened a tab for {}".format(s["name"]), {"opened": True}


def act_open(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No such session", {}
    attach_tab(s)
    return 200, True, "Opened another tab for {}".format(s["name"]), {"opened": True}


def act_color(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No such session", {}
    c = str(b.get("color", "")).strip()
    if c == "none":
        s["color"] = ""
        return 200, True, "Cleared the color of {}".format(s["name"]), {}
    if c in PALETTE:
        s["color"] = PALETTE[c]
    elif HEX_RE.match(c):
        s["color"] = c.lower()
    else:
        return 400, False, f"Invalid color: {c}", {}
    return 200, True, "Recolored {}".format(s["name"]), {}


def act_new(b):
    name = str(b.get("name", "")).strip()
    if not NAME_RE.match(name):
        return 400, False, "Invalid name (letters, digits, _ and - only)", {}
    if find(name):
        return 400, False, f"Session {name} already exists", {}
    cwd = str(b.get("cwd", "")).strip() or "~"
    path = os.path.expanduser(cwd)
    start = bool(b.get("start_claude"))
    s = {
        "name": name, "color": "", "attached": 0, "in_iterm2": False,
        "state": "working" if start else "", "since": _now() if start else None,
        "source": "hook", "detail": "Starting Claude…" if start else "",
        "title": "", "path": path, "project": os.path.basename(path.rstrip("/")) or path,
        "panes": [pane("%0", "@0", claude=start, state="working" if start else "", path=path)],
    }
    SESSIONS.append(s)
    act_color({"session": name, "color": str(b.get("color", "none")) or "none"})
    if b.get("open"):
        attach_tab(s)
    return 200, True, f"Created {name}", {"opened": bool(b.get("open"))}


def act_kill(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No such session", {}
    SESSIONS.remove(s)
    return 200, True, "Killed {}".format(s["name"]), {}


def act_rename(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No such session", {}
    new = str(b.get("name", "")).strip()
    if not NAME_RE.match(new):
        return 400, False, "Invalid new name", {}
    if find(new):
        return 400, False, f"The name {new} is taken", {}
    s["name"] = new
    return 200, True, f"Renamed to {new}", {}


def act_detach(b):
    s = find(b.get("session", ""))
    if not s:
        return 404, False, "No such session", {}
    s["attached"] = 0
    s["in_iterm2"] = False
    for p in s["panes"]:
        p["guid"] = p["iterm2_window"] = p["iterm2_tab"] = ""
    return 200, True, "Detached every client of {}".format(s["name"]), {}


def act_next_waiting(_b):
    for s in sorted(SESSIONS, key=lambda x: (ORDER.get(x["state"], 9), x["name"])):
        if s["state"] == "waiting":
            if not s["in_iterm2"]:
                attach_tab(s)
            return 200, True, "Went to {}".format(s["name"]), {"session": s["name"]}
    return 200, False, "No session is waiting for you right now", {}


def _tombstone(session_id):
    return next((t for t in TOMBSTONES if t["session_id"] == session_id), None)


def act_resume(b):
    t = _tombstone(str(b.get("session_id", "")))
    if not t:
        return 400, False, "No dead session with that id", {}
    name = t["tmux_session"] or "claude"
    while find(name):
        name = f"{name}-2"
    s = {"name": name, "color": t["color"], "attached": 0, "in_iterm2": False, "state": "working",
         "since": _now(), "source": "hook", "detail": "Resuming…", "title": t["last_message"],
         "path": t["cwd"], "project": t["project"], "ctx_pct": 0, "model": "Opus 5", "cost_usd": 0.0,
         "panes": [pane("%0", "@0", state="working", detail="Resuming…", path=t["cwd"])]}
    SESSIONS.append(s)
    TOMBSTONES.remove(t)
    if b.get("open", True):
        attach_tab(s)
    return 200, True, f"{name}: typed claude --resume {t['session_id']}", {"name": name, "pane_id": "%0"}


def act_forget(b):
    t = _tombstone(str(b.get("session_id", "")))
    if not t:
        return 400, False, "No dead session with that id", {}
    TOMBSTONES.remove(t)
    return 200, True, f"Forgot {t['session_id']}", {}


def act_mock(body):
    """Development only: patch a session in place so a demo (or a recording) can show state changing.

        curl -XPOST -H "X-TM-Token: $TOKEN" localhost:47399/api/_mock \
             -d '{"name": "api", "patch": {"state": "waiting", "detail": "Allow Bash(pytest -q)?"}}'
    """
    sess = find(body.get("name"))
    if not sess:
        return 400, False, "No such session", {}
    patch = body.get("patch") or {}
    if not isinstance(patch, dict):
        return 400, False, "patch must be an object", {}
    sess.update(patch)
    for key in ("state", "detail", "title", "since"):
        if key in patch and sess.get("panes"):
            sess["panes"][0][key] = patch[key]
    if "since" not in patch and "state" in patch:
        sess["since"] = _now()
        if sess.get("panes"):
            sess["panes"][0]["since"] = sess["since"]
    return 200, True, f"Patched {sess['name']}", {}


ROUTES = {
    "/api/_mock": act_mock,
    "/api/go": act_go, "/api/open": act_open, "/api/color": act_color,
    "/api/new": act_new, "/api/kill": act_kill, "/api/rename": act_rename,
    "/api/detach": act_detach, "/api/next-waiting": act_next_waiting,
    "/api/resume": act_resume, "/api/forget": act_forget,
}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "kalmux-mock/0.4"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # terser than the default, to keep the terminal readable
        sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- send helpers ---------------------------------------------------
    def _send(self, code, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, ok, message, data):
        payload = json.dumps({"ok": ok, "message": message, "data": data},
                             ensure_ascii=False).encode("utf-8")
        self._send(code, payload, "application/json; charset=utf-8")

    def _page(self):
        try:
            src = INDEX.read_text("utf-8")
        except OSError as e:
            self._send(500, str(e).encode(), "text/plain; charset=utf-8")
            return
        nonce = secrets.token_urlsafe(16)
        src = (src.replace("__TM_NONCE__", nonce)
                  .replace("__TM_TOKEN__", TOKEN)
                  .replace("__TM_PALETTE__", html.escape(json.dumps(PALETTE), quote=True)))
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
               "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
        self._send(200, src.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": csp})

    def _guard(self) -> bool:
        """Token + Origin, exactly like the real server. Wrong ones give 403 so the page reloads itself."""
        if self.headers.get("X-TM-Token") != TOKEN:
            self._json(403, False, "Bad token — the page will reload itself", {})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://" + (self.headers.get("Host") or ""),):
            self._json(403, False, "Unexpected origin", {})
            return False
        return True

    # -- routing -------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._page()
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/api/state":
            if not self._guard():
                return
            with LOCK:
                self._json(200, True, "", snapshot())
        else:
            self._json(404, False, f"No such path: {path}", {})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        fn = ROUTES.get(path)
        if fn is None:
            self._json(404, False, f"No such path: {path}", {})
            return
        if not self._guard():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise ValueError("the body must be a JSON object")  # noqa: TRY004 - answered as HTTP 400, not a type bug
        except Exception as e:  # noqa: BLE001 - a mock must answer 400, never die on a bad body
            self._json(400, False, f"Bad JSON: {e}", {})
            return
        try:
            with LOCK:
                code, ok, msg, data = fn(body)
        except Exception as e:  # noqa: BLE001 - never let the mock die mid-request
            self._json(500, False, f"Server error: {e}", {})
            return
        self._json(code, ok, msg, data)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 47399
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"kalmux mock  http://127.0.0.1:{port}/   token={TOKEN}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
