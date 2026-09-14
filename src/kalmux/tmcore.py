"""tmcore — shared core for Kalmux (the `kalmux` CLI and the `kalmux ui` server).

State comes from two first-party sources, never from screen scraping:
  * tmux pane user options written by the cc-status-tmux hook (@cc_state, @cc_detail, @cc_since)
  * Claude Code's live registry ~/.claude/sessions/<pid>.json (field `tmux`, `status`)
iTerm2 is driven through its bundled `it2` CLI; tab identity comes from iTerm2's own
`tmuxWindowPane` session variable, so a tmux pane maps to exactly one iTerm2 tab.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

VERSION = "0.4.1"
# 20 identity colors in spectrum order (the UI shows them in this order); "grey" is an alias of "gray"
# that the UI filters out. Every value of the first nine is the one Kalmux shipped with: changing a hex would
# silently repaint sessions that already carry it in @tm_color.
PALETTE = {
    "red": "#ff453a", "orange": "#ff9500", "yellow": "#ffd60a", "lime": "#a3e635", "green": "#30d158",
    "mint": "#66d4cf", "teal": "#40c8e0", "cyan": "#22d3ee", "blue": "#0a84ff", "indigo": "#5e5ce6",
    "purple": "#bf5af2", "magenta": "#ff4fd8", "pink": "#ff375f", "coral": "#ff6f61", "brown": "#ac8e68",
    "gold": "#d4af37", "olive": "#8a9a5b", "slate": "#6c7a89", "gray": "#8e8e93", "white": "#f2f2f7",
    "grey": "#8e8e93",
}
CLEAR_WORDS = {"none", "clear", "default", "off", ""}
# "busy" = a non-Claude command occupies the pane. It ranks below EVERY Claude-derived state, including stale
# and gone: a session whose Claude died must still read as gone, not as busy because its shell runs something.
STATE_ORDER = {"waiting": 0, "working": 1, "unknown": 2, "idle": 3, "stale": 4, "gone": 5, "busy": 6, "": 7}
ANSI = {"waiting": "\033[1;34m", "working": "\033[1;33m", "idle": "\033[32m", "unknown": "\033[2m",
        "busy": "\033[36m", "stale": "\033[2m", "gone": "\033[2;31m", "reset": "\033[0m"}
STALE_AFTER = 30 * 60          # a working/waiting state older than this is flagged with "?"
STATUS_FRESH = 60 * 60         # a status-line record older than this is a dead session's leftover: ignored
MAX_STATUS_BYTES = 64 * 1024   # one status file is a few hundred bytes; anything bigger is not ours
DAY = 24 * 60 * 60
# SessionEnd reasons: a conversation that was cleared or resumed lives on elsewhere, so its trail is not a death
END_SUPERSEDED = {"clear", "resume"}
SEP = "\x1f"                   # field separator for tmux -F output (never appears in tmux data)
FIELDS = ["session", "window_id", "pane_id", "cc_state", "cc_detail", "cc_since", "title", "path", "cmd", "tm_color",
          "attached", "window_active", "pane_active", "tty", "start_cmd", "shell", "dead"]
TMUX_FMT = SEP.join([
    "#{session_name}", "#{window_id}", "#{pane_id}", "#{@cc_state}", "#{@cc_detail}", "#{@cc_since}",
    "#{pane_title}", "#{pane_current_path}", "#{pane_current_command}", "#{@tm_color}", "#{session_attached}",
    "#{window_active}", "#{pane_active}", "#{pane_tty}", "#{pane_start_command}", "#{default-shell}", "#{pane_dead}",
])
assert len(FIELDS) == len(TMUX_FMT.split(SEP)), "FIELDS and TMUX_FMT must stay in lockstep (parse_panes drops every record otherwise)"
IT2_BUNDLED = "/Applications/iTerm.app/Contents/Resources/utilities/it2"
PKG_DIR = Path(__file__).resolve().parent              # where the modules live (a clone or site-packages)
ASSETS = PKG_DIR / "assets"                            # cc-status-tmux, index.html, it2_toolbelt.py
_checkout = PKG_DIR.parent.parent
# Running straight from a git clone (bin/kalmux, or an editable install) rather than from an installed wheel.
SOURCE_CHECKOUT = _checkout if (_checkout / "pyproject.toml").is_file() and (_checkout / "bin" / "kalmux").is_file() else None
ROOT = SOURCE_CHECKOUT or PKG_DIR                      # a symlink pointing inside this is one of ours
DEFAULT_REGISTRY = Path.home() / ".claude/sessions"
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
WINDOW_ID_RE = re.compile(r"^@\d+$")
PANE_ID_RE = re.compile(r"^%\d+$")
TTY_RE = re.compile(r"^/dev/(pts/\d+|[A-Za-z0-9._-]+)$")
IT2_TIMEOUT = 6
# A pane whose foreground command is its own shell sits at a prompt; anything else is running something.
SHELL_NAMES = {"sh", "bash", "zsh", "fish", "ksh", "mksh", "dash", "ash", "csh", "tcsh", "nu", "xonsh", "elvish"}
# Where tmux lives when it is not on PATH: a server started by iTerm2's AutoLaunch script inherits the
# login PATH (/usr/bin:/bin:/usr/sbin:/sbin), which does not include Homebrew (2026-09-13: empty toolbelt after reboot).
TMUX_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", str(Path.home() / ".local/bin"), "/usr/bin")


# ----------------------------------------------------------------------------- colors
def parse_color(value: str) -> str:
    """Return a normalized '#rrggbb' from a palette name or hex ('#abc' / '#aabbcc')."""
    v = value.strip().lower()
    if v in PALETTE:
        return PALETTE[v]
    if re.fullmatch(r"#?[0-9a-f]{3}", v):
        return "#" + "".join(c * 2 for c in v.lstrip("#"))
    if re.fullmatch(r"#?[0-9a-f]{6}", v):
        return "#" + v.lstrip("#")
    raise ValueError(f"unknown color: {value!r} (use #rrggbb or one of {', '.join(sorted(set(PALETTE)))})")


def osc6_tab_color(hex_color: str | None) -> str:
    """iTerm2 tab color escape sequences (they pass through tmux -CC)."""
    if not hex_color:
        return "\x1b]6;1;bg;*;default\x07"
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "".join(f"\x1b]6;1;bg;{name};brightness;{val}\x07" for name, val in (("red", r), ("green", g), ("blue", b)))


def write_tty(tty: str, data: str) -> bool:
    """Best-effort, non-blocking write to a terminal: a stalled pty must never hang the caller.

    Only character devices under /dev are accepted. The path comes out of tmux's format output, and a
    forged record must not be able to turn a status replay into an append to some regular file."""
    if not tty.startswith("/dev/"):
        return False
    try:
        fd = os.open(tty, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        if not stat.S_ISCHR(os.fstat(fd).st_mode):
            return False
        os.write(fd, data.encode("utf-8", "replace"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def valid_session_name(name: str) -> bool:
    return bool(SESSION_NAME_RE.match(name or ""))


# ----------------------------------------------------------------------------- shell vs command
def _command_name(value: str) -> str:
    """Bare program name of a tmux command field: '/bin/bash' -> 'bash', '"sleep 40"' -> 'sleep', '-zsh' -> 'zsh'."""
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        return ""
    return os.path.basename(text.split()[0]).lstrip("-").lower()


def pane_shells(row: dict) -> set[str]:
    """Shell names that mean "this pane is sitting at its prompt".

    `pane_start_command` is set when the pane was opened with an explicit command, and tmux also copies the
    `default-command` option into it, so it is often a wrapper AROUND the shell ("exec /bin/zsh",
    "reattach-to-user-namespace -l zsh", "cd /tmp && exec zsh"). Every word is checked, not just the first.
    A start command with no shell in it at all is a real program: that pane never has a prompt.
    No start command = the `default-shell` option (a session option: changing it later does not move panes
    that already exist, which is accepted).
    """
    start = (row.get("start_cmd") or "").strip().strip('"').strip("'")
    if start:
        return {_command_name(w) for w in start.split()} & SHELL_NAMES
    shell = _command_name(row.get("shell", ""))
    return {shell} if shell else set()


def foreground_busy(row: dict) -> bool:
    """True when the pane's foreground command is something other than its own shell.

    tmux reports the foreground process of the pane, so a shell sitting at its prompt reports itself. A dead
    pane (remain-on-exit) reports the command it died with, so it is never busy. Blind spot: a shell running a
    script of its OWN kind (a bash script under a bash shell) still reports that shell's name, so it reads as
    a prompt. Tracking that needs a shell hook, not a tmux format.
    """
    if row.get("dead") == "1":
        return False
    cmd = _command_name(row.get("cmd", ""))
    return bool(cmd) and cmd not in pane_shells(row)


# ----------------------------------------------------------------------------- tmux
def _well_formed(row: dict) -> bool:
    """tmux never escapes newlines in user options / paths, so a hostile value can smuggle a whole extra
    record into `list-panes` output. Anything that does not look like tmux's own ids is dropped."""
    return (bool(WINDOW_ID_RE.match(row["window_id"])) and bool(PANE_ID_RE.match(row["pane_id"]))
            and row["attached"].isdigit() and row["window_active"] in ("0", "1") and row["pane_active"] in ("0", "1")
            and row["dead"] in ("", "0", "1") and (row["tty"] == "" or bool(TTY_RE.match(row["tty"]))))


def parse_panes(text: str) -> list[dict]:
    rows = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        parts = line.split(SEP)
        if len(parts) != len(FIELDS):
            continue  # malformed record: never let one field shift the columns of the others
        row = dict(zip(FIELDS, parts, strict=True))
        if _well_formed(row):
            rows.append(row)
    return rows


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def resolve_tmux(path_env: str | None = None) -> str:
    """Absolute path of tmux: PATH first, then the usual install dirs (see TMUX_FALLBACK_DIRS)."""
    found = shutil.which("tmux", path=path_env)
    if found:
        return found
    return next((c for c in (os.path.join(d, "tmux") for d in TMUX_FALLBACK_DIRS) if os.access(c, os.X_OK)), "")


class Tmux:
    def __init__(self, path: str | None = None) -> None:
        self.path = resolve_tmux() if path is None else path

    def run_rc(self, *args: str) -> tuple[int, str]:
        if not self.path:
            return 127, ""
        try:
            p = subprocess.run([self.path, *args], capture_output=True, check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return p.returncode, _decode(p.stdout)

    def run(self, *args: str) -> str:
        return self.run_rc(*args)[1]

    def list_panes(self) -> list[dict]:
        return parse_panes(self.run("list-panes", "-a", "-F", TMUX_FMT))

    def session_id(self, name: str) -> str:
        """tmux's own session id ($N) for an EXACT name, or "" when there is no such session."""
        for line in self.run("list-sessions", "-F", "#{session_id}" + SEP + "#{session_name}").split("\n"):
            sid, _, sname = line.partition(SEP)
            if sid and sname == name:
                return sid
        return ""

    def show_session_option(self, session: str, name: str) -> str:
        return self.run("show", "-t", "=" + session, "-qv", name).strip()

    def set_session_option(self, session: str, name: str, value: str | None) -> bool:
        # By id, not by "=name": set-option is the one command that does not honour the exact-match prefix
        # (tmux 3.6a answers "no such session: =my-proj"), and a bare name would prefix-match a longer session.
        target = self.session_id(session)
        if not target:
            return False
        args = ("set", "-t", target, "-u", name) if value is None else ("set", "-t", target, name, value)
        return self.run_rc(*args)[0] == 0

    def pane_tty(self, pane_id: str) -> str:
        return self.run("display", "-p", "-t", pane_id, "#{pane_tty}").strip()

    def new_session(self, name: str, cwd: str | None) -> bool:
        args = ["new-session", "-d", "-s", name]
        if cwd:
            args += ["-c", cwd]
        return self.run_rc(*args)[0] == 0

    def new_session_pane(self, name: str, cwd: str | None) -> str:
        """Create a detached session and return the pane id of its first pane ("" on failure).

        A pane id is the only safe send-keys target: a session name can be renamed (or prefix-match another
        session) between the create and the keystroke."""
        args = ["new-session", "-d", "-s", name, *(["-c", cwd] if cwd else []), "-P", "-F", "#{pane_id}"]
        rc, out = self.run_rc(*args)
        pane_id = out.strip().split("\n")[0].strip()
        return pane_id if rc == 0 and PANE_ID_RE.match(pane_id) else ""

    def has_session(self, name: str) -> bool:
        return self.run_rc("has-session", "-t", "=" + name)[0] == 0

    def kill_session(self, name: str) -> bool:
        return self.run_rc("kill-session", "-t", "=" + name)[0] == 0

    def rename_session(self, old: str, new: str) -> bool:
        return self.run_rc("rename-session", "-t", "=" + old, new)[0] == 0

    def detach_session(self, name: str) -> bool:
        return self.run_rc("detach-client", "-s", "=" + name)[0] == 0

    def send_keys(self, target: str, text: str) -> bool:
        """Type a command and run it. Two calls on purpose: `send-keys -t X <text> Enter` parses a text that
        starts with '-' as a flag, so a configured `claude.new` like "--dangerously-skip-permissions" would
        be swallowed. send_text ends the options with '--'."""
        return self.send_text(target, text) and self.press_enter(target)

    def send_text(self, target: str, text: str) -> bool:
        """Type text into a pane without running it: -l sends the literal characters, -- ends the options
        (a command starting with '-' would otherwise be parsed as a flag) and no Enter is appended."""
        return self.run_rc("send-keys", "-t", target, "-l", "--", text)[0] == 0

    def press_enter(self, target: str) -> bool:
        return self.run_rc("send-keys", "-t", target, "Enter")[0] == 0


# ----------------------------------------------------------------------------- iTerm2 (it2 CLI)
class It2:
    """Talks to iTerm2 through its bundled `it2` CLI. Prefer the bundled binary: a different
    `it2` (pip package used by Claude Code teammate mode) may shadow it on PATH."""

    def __init__(self) -> None:
        candidates = [IT2_BUNDLED, shutil.which("it2")]
        self.path = next((p for p in candidates if p and os.path.exists(p)), None)
        self.last_rc = 0

    def available(self) -> bool:
        return bool(self.path) and sys.platform == "darwin"

    def _run_rc(self, *args: str) -> tuple[int, str]:
        # Each call costs ~150 ms: it2 fetches a single-use API cookie from iTerm2 through an Apple Event
        # (a cookie cannot be shared across calls, so there is nothing to cache here; TabMap caches results).
        if not self.path:
            self.last_rc = 127
            return 127, ""
        try:
            p = subprocess.run([self.path, *args], capture_output=True, check=False, timeout=IT2_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            self.last_rc = 127
            return 127, ""
        self.last_rc = p.returncode
        return p.returncode, _decode(p.stdout) + (_decode(p.stderr) if p.returncode else "")

    def _run(self, *args: str) -> str:
        return self._run_rc(*args)[1]

    def list_sessions(self) -> list[dict]:
        """iTerm2 sessions as dicts: guid, name, title, window, tab, tty, is_tmux (JSON with TSV fallback)."""
        raw = self._run("session", "list", "--json")
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, list):
            rows = []
            for s in data:
                if isinstance(s, dict) and isinstance(s.get("id"), str):
                    rows.append({"guid": s["id"], "name": str(s.get("name", "")), "title": str(s.get("title", "")),
                                 "window": str(s.get("window_id", "")), "tab": str(s.get("tab_id", "")),
                                 "tty": str(s.get("tty", "")).replace("\\/", "/"), "is_tmux": bool(s.get("is_tmux"))})
            return rows
        rows = []
        for line in self._run("session", "list").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                rows.append({"guid": parts[0], "name": parts[1], "title": parts[2] if len(parts) > 2 else parts[1],
                             "window": "", "tab": "", "tty": parts[4] if len(parts) > 4 else "", "is_tmux": None})
        return rows

    def get_var(self, guid: str, name: str) -> str:
        rc, out = self._run_rc("session", "get-var", name, "--session", guid)
        out = out.strip()
        if rc != 0 or not out or out.startswith("Variable "):
            return ""
        if len(out) >= 2 and out[0] == out[-1] == '"':
            out = out[1:-1]
        return out

    def focus(self, guid: str) -> bool:
        return self._run_rc("session", "focus", guid)[0] == 0

    def current_window(self) -> str:
        for line in self._run("app", "get-focus").splitlines():
            if line.startswith("Current window:"):
                return line.split(":", 1)[1].strip()
        return ""

    def list_windows(self) -> list[str]:
        """Window ids, front-most first (`it2 window list` prints one 'pty-… <n> tabs …' line per window)."""
        return [line.split("\t", 1)[0].strip() for line in self._run("window", "list").splitlines() if line.startswith("pty-")]

    def new_window(self, command: str) -> tuple[bool, str]:
        rc, out = self._run_rc("window", "new", "--command", command)
        return rc == 0, out.strip()

    def new_tab(self, command: str, window: str = "") -> tuple[bool, str]:
        args = ["tab", "new"]
        if window:
            args += ["--window", window]
        rc, out = self._run_rc(*args, "--command", command)
        return rc == 0, out.strip()

    def activate(self) -> None:
        self._run_rc("app", "activate")


# ----------------------------------------------------------------------------- registry
def pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _better(new: dict, old: dict) -> bool:
    """Prefer a live process, then the most recently updated record."""
    if new["alive"] != old["alive"]:
        return new["alive"]
    return (new.get("statusUpdatedAt") or 0) > (old.get("statusUpdatedAt") or 0)


def load_registry(directory: Path, alive=pid_alive) -> dict:
    """Map 'session:@window.%pane' -> Claude Code registry entry (~/.claude/sessions/<pid>.json)."""
    out: dict = {}
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        key = d.get("tmux")
        if not isinstance(key, str) or not key:
            continue
        pid = d.get("pid")
        ts = d.get("statusUpdatedAt")
        entry = {
            "pid": pid, "sessionId": str(d.get("sessionId", "")), "name": str(d.get("name", "")), "cwd": str(d.get("cwd", "")),
            "status": str(d.get("status", "")), "statusUpdatedAt": ts if isinstance(ts, (int, float)) else None,
            "alive": bool(alive(pid)) if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else False,
        }
        if key not in out or _better(entry, out[key]):
            out[key] = entry
    return out


# ----------------------------------------------------------------------------- status line records
def load_status(directory: Path) -> dict:
    """Map Claude session id -> the record `kalmux statusline` last wrote (${STATE}/status/<id>.json).

    Unreadable, oversized or shapeless files are skipped: the gauge is a nicety, never a reason to fail."""
    out: dict = {}
    directory = Path(directory)
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            if f.stat().st_size > MAX_STATUS_BYTES:
                continue
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        session_id = data.get("session_id") if isinstance(data, dict) else None
        if isinstance(session_id, str) and session_id:
            out[session_id] = data
    return out


def _number(value) -> int | float | None:
    """A finite JSON number, or None. NaN/Infinity are dropped: they are not representable in JSON."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return value if math.isfinite(value) else None


def _window(raw) -> dict | None:
    """One rate-limit window, keeping only numbers JSON can carry (the statusline tap is external data)."""
    if not isinstance(raw, dict):
        return None
    pct, at = _number(raw.get("used_pct")), _number(raw.get("resets_at"))
    return None if pct is None and at is None else {"used_pct": pct, "resets_at": at}


def _quota(raw) -> dict | None:
    """rate_limits as the UI reads it: {window: {used_pct, resets_at}}, or None when there is nothing usable."""
    if not isinstance(raw, dict):
        return None
    clean = {k: w for k, w in ((k, _window(v)) for k, v in raw.items() if isinstance(k, str)) if w}
    return clean or None


def status_fields(record, now: int) -> dict:
    """ctx_pct / model / cost_usd / quota / status_age for one merged row (blank when there is no fresh record)."""
    blank = {"ctx_pct": None, "model": "", "cost_usd": None, "quota": None, "status_age": None}
    ts = _number(record.get("ts")) if isinstance(record, dict) else None
    if ts is None or now - ts > STATUS_FRESH:
        return blank                                   # no record, or the leftover of a session that died
    pct = _number(record.get("context_pct"))
    return {"ctx_pct": max(0, min(100, int(pct))) if pct is not None else None,
            "model": record.get("model") if isinstance(record.get("model"), str) else "",
            "cost_usd": _number(record.get("cost_usd")), "quota": _quota(record.get("rate_limits")),
            "status_age": max(0, int(now - ts))}


# ----------------------------------------------------------------------------- merge / render
def looks_like_claude(cmd: str) -> bool:
    return "claude" in cmd or bool(VERSION_RE.match(cmd))


def merge(panes: list[dict], registry: dict, now: int, status: dict | None = None) -> list[dict]:
    status = status or {}
    rows = []
    for p in panes:
        key = f"{p['session']}:{p['window_id']}.{p['pane_id']}"
        reg = registry.get(key)
        claude_proc = looks_like_claude(p.get("cmd", ""))
        claude = claude_proc or bool(reg and reg.get("alive"))
        hook_state, detail, source = p.get("cc_state", ""), p.get("cc_detail", ""), ""
        since = int(p["cc_since"]) if p.get("cc_since", "").isdigit() else None
        state = ""
        if reg and not reg.get("alive"):
            state, source = "gone", "registry"             # Claude process died (no SessionEnd)
        elif hook_state and not claude:
            state, source = "stale", "hook"                # hook state left behind, pane runs something else
        elif hook_state:
            state, source = hook_state, "hook"
        elif reg:
            state, source = {"busy": "working", "idle": "idle"}.get(reg.get("status", ""), "unknown"), "registry"
            ts = reg.get("statusUpdatedAt")
            since = int(ts / 1000) if ts else None
        elif claude:
            state = "unknown"
        elif foreground_busy(p):
            state, detail = "busy", p.get("cmd", "")      # a plain command in the pane, e.g. a build or a script
        age = (now - since) if since is not None else None
        path = p.get("path", "")
        session_id = reg.get("sessionId", "") if reg else ""
        rows.append({
            **status_fields(status.get(session_id), now),
            "session": p["session"], "pane": f"{p['window_id']}.{p['pane_id']}", "window_id": p["window_id"], "pane_id": p["pane_id"],
            "project": os.path.basename(path.rstrip("/")) or path,
            "path": path, "claude": claude, "state": state, "detail": detail, "source": source, "age": age,
            "stale": state in ("working", "waiting") and age is not None and age > STALE_AFTER,
            "title": p.get("title", ""), "color": p.get("tm_color", "") if HEX_RE.match(p.get("tm_color", "")) else "",
            "claude_session_id": session_id, "claude_name": reg.get("name", "") if reg else "",
            "attached": p.get("attached", ""), "active": p.get("window_active", "") == "1" and p.get("pane_active", "") == "1",
            "tty": p.get("tty", ""),
        })
    return rows


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (STATE_ORDER.get(r.get("state", ""), 9), r.get("session", "")))


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def state_label(r: dict) -> str:
    label = r["state"] or ("-" if not r["claude"] else "?")
    if r.get("source") == "registry" and r["state"] not in ("gone", ""):
        label += "~"      # last known value from Claude's registry, not a live hook event
    if r.get("stale"):
        label += "?"      # working/waiting for suspiciously long: probably needs a look
    return label


def _clean(cell: str, width: int) -> str:
    return CTRL_RE.sub("?", str(cell))[:width]


def ctx_label(row: dict) -> str:
    pct = row.get("ctx_pct")
    return f"{int(pct)}%" if isinstance(pct, (int, float)) and not isinstance(pct, bool) else "-"


def render_table(rows: list[dict], color: bool = False, columns: int | None = None) -> str:
    columns = columns or shutil.get_terminal_size((120, 24)).columns
    header = ["SESSION", "PROJECT", "STATE", "CTX", "AGE", "TITLE", "DETAIL", "COLOR"]
    fixed = [max(len(header[0]), *(len(r["session"]) for r in rows or [{"session": ""}])),
             max(len(header[1]), *(len(r["project"]) for r in rows or [{"project": ""}])), 9, 4, 6, 0, 0, 7]
    free = max(24, columns - sum(fixed) - 2 * (len(header) - 1))
    title_w, detail_w = max(12, free // 2), max(12, free - free // 2)
    body = []
    for r in rows:
        swatch = r["color"]
        if color and HEX_RE.match(r["color"] or ""):
            h = r["color"].lstrip("#")
            swatch = f"\033[48;2;{int(h[0:2], 16)};{int(h[2:4], 16)};{int(h[4:6], 16)}m  \033[0m {r['color']}"
        body.append([_clean(r["session"], 40), _clean(r["project"], 40), state_label(r), ctx_label(r), fmt_age(r["age"]),
                     _clean(r["title"], title_w), _clean(r["detail"], detail_w), swatch])
    widths = [max(len(h), *(len(CTRL_RE.sub("", c)) if i < len(header) - 1 else 7 for c in col)) for i, (h, col) in enumerate(zip(header, zip(*body, strict=True), strict=True))] if body else [len(h) for h in header]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)).rstrip()]
    for r, cells in zip(rows, body, strict=True):
        text = "  ".join(str(c).ljust(w) for c, w in zip(cells[:-1], widths[:-1], strict=True)) + "  " + cells[-1]
        if color and ANSI.get(r["state"]):
            text = ANSI[r["state"]] + text + ANSI["reset"]
        lines.append(text.rstrip())
    return "\n".join(lines)


# ----------------------------------------------------------------------------- trails / tombstones
def _blank_trail(session_id: str) -> dict:
    return {"session_id": session_id, "tmux_session": "", "pane": "", "cwd": "", "color": "", "started": None,
            "last_ts": None, "last_event": "", "end_reason": "", "last_message": ""}


def _trail_event(trail: dict, event: str, line: dict) -> None:
    if event == "SessionEnd":
        reason = line.get("reason")
        trail["end_reason"] = reason if isinstance(reason, str) else ""
    elif event == "Stop":
        detail = line.get("detail")
        trail["last_message"] = detail if isinstance(detail, str) else trail["last_message"]


def _trail_line(trail: dict, line: dict) -> None:
    event = line.get("event") if isinstance(line.get("event"), str) else ""
    ts = _number(line.get("ts"))
    for key in ("tmux_session", "pane", "cwd", "color"):
        value = line.get(key)
        if isinstance(value, str) and value:
            trail[key] = value                      # a resumed conversation may have moved: last one wins
    if ts is not None:
        trail["last_ts"] = int(ts)
        if event == "SessionStart" and trail["started"] is None:
            trail["started"] = int(ts)
    if event:
        trail["last_event"] = event
        _trail_event(trail, event, line)


def parse_trail(session_id: str, text: str) -> dict | None:
    """Fold one <session_id>.jsonl trail into a summary dict, or None when no line parsed."""
    trail, seen = _blank_trail(session_id), False
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue                                # a truncated append (the hook writes best effort)
        if isinstance(line, dict):
            seen = True
            _trail_line(trail, line)
    return trail if seen else None


def load_trails(trace_dir: Path, now: int, keep_days: int = 30) -> list[dict]:
    """Every trail in ${STATE}/trace, NEWEST FIRST: by last_ts, then by file mtime for trails with no timestamp.

    The order is part of the contract: `find_trail` picks the first match, so a name reused by several
    conversations resolves to the most recent one. Files older than keep_days are pruned (trails are ours)."""
    found: list[tuple[int, float, dict]] = []
    trace_dir = Path(trace_dir)
    if not trace_dir.is_dir():
        return []
    cutoff = now - max(1, keep_days) * DAY
    for f in sorted(trace_dir.glob("*.jsonl")):
        try:
            mtime = f.stat().st_mtime
            if mtime < cutoff:
                f.unlink()
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        trail = parse_trail(f.stem, text)
        if trail:
            found.append((trail["last_ts"] or 0, mtime, trail))
    found.sort(key=lambda item: (-item[0], -item[1], item[2]["session_id"]))
    return [trail for _, _, trail in found]


def ended_how(trail: dict) -> str:
    """clean = Claude said goodbye, superseded = cleared/resumed elsewhere, killed = no SessionEnd at all."""
    if trail.get("last_event") != "SessionEnd":
        return "killed"
    return "superseded" if trail.get("end_reason") in END_SUPERSEDED else "clean"


def alive_session_ids(registry: dict) -> set[str]:
    """The Claude session ids the registry still reports as running."""
    return {e.get("sessionId") for e in registry.values() if e.get("alive") and e.get("sessionId")}


def tombstones(trails: list[dict], registry: dict) -> list[dict]:
    """Trails of Claude sessions that are not running any more, killed first, then newest first."""
    alive = alive_session_ids(registry)
    out = []
    for t in trails:
        if t.get("session_id") in alive:
            continue
        cwd = t.get("cwd") or ""
        out.append({**t, "ended": ended_how(t), "project": os.path.basename(cwd.rstrip("/")) or cwd})
    out.sort(key=lambda r: (r["ended"] != "killed", -(r["last_ts"] or 0)))
    return out


def resumable_trails(trace_dir: Path, registry: dict, now: int, keep_days: int = 30) -> list[dict]:
    """The only list a resume may choose from: tombstones, so a LIVE conversation can never be a candidate."""
    return tombstones(load_trails(trace_dir, now, keep_days), registry)


# ----------------------------------------------------------------------------- attach helpers
def attach_command(name: str, env) -> list[str]:
    """The right verb for the current terminal: switch inside tmux, -CC in iTerm2, plain elsewhere."""
    if env.get("TMUX"):
        return ["tmux", "switch-client", "-t", name]
    if env.get("TERM_PROGRAM") == "iTerm.app" or env.get("LC_TERMINAL") == "iTerm2":
        return ["tmux", "-CC", "attach", "-t", name]
    return ["tmux", "attach", "-t", name]


def cc_tab_command(name: str) -> str:
    """Command line for a fresh iTerm2 tab that attaches in control mode (login shell => user PATH).

    The target keeps its quotes: zsh expands a bare `=word` to the path of the command `word` (the EQUALS
    option, on by default), so `-t =my-proj` aborts the whole line with "not found" and the tab silently
    stays a plain shell. Only plain names are accepted: iTerm2 parses the string, then zsh does."""
    if not valid_session_name(name):
        raise ValueError(f"session name {name!r} is not safe to put on a command line")
    return f"""/bin/zsh -lc 'exec tmux -CC attach -t "={name}"'"""
