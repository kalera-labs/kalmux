"""Shared fakes for the tm test-suite: an in-memory tmux and an in-memory it2."""
from __future__ import annotations

import json
import os
import pathlib

from kalmux.tmcore import FIELDS, SEP


def pane(session, pane_id="%1", window_id="@1", state="", detail="", since="", title="✳ x", path="/Volumes/Dev/proj",
         cmd="2.1.270", color="", attached="1", active=True, tty="", start_cmd="", shell="/bin/zsh", dead="0"):
    return {"session": session, "window_id": window_id, "pane_id": pane_id, "cc_state": state, "cc_detail": detail,
            "cc_since": since, "title": title, "path": path, "cmd": cmd, "tm_color": color, "attached": attached,
            "window_active": "1" if active else "0", "pane_active": "1" if active else "0", "tty": tty,
            "start_cmd": start_cmd, "shell": shell, "dead": dead}


def tmux_line(p: dict) -> str:
    """One `list-panes -F TMUX_FMT` record for a pane dict."""
    return SEP.join(str(p[k]) for k in FIELDS)


def it2_row(guid, name="✳ x (claude)", window="pty-W1", tab="2", is_tmux=True, tty=""):
    return {"guid": guid, "name": name, "title": name, "window": window, "tab": tab, "tty": tty, "is_tmux": is_tmux}


def it2_json_row(guid, name="✳ x (claude)", window="pty-W1", tab="2", is_tmux=True, tty=""):
    """What `it2 session list --json` prints for one session."""
    return {"id": guid, "name": name, "title": name, "window_id": window, "tab_id": tab, "tty": tty, "is_tmux": is_tmux,
            "rows": 50, "cols": 150}


class FakeTmux:
    path = "/fake/bin/tmux"

    def __init__(self, panes=None, colors=None, tty=None):
        self.panes = [dict(p) for p in (panes or [])]
        self.colors = colors or {}
        self.calls = []
        self.tty = tty
        self.fail: set[str] = set()

    def list_panes(self):
        return [dict(p) for p in self.panes]

    def show_session_option(self, session, name):
        self.calls.append(("show", session, name))
        return self.colors.get(session, "")

    def session_id(self, name):
        return next((f"${i}" for i, p in enumerate(self.panes) if p["session"] == name), "")

    def set_session_option(self, session, name, value):
        self.calls.append(("set", session, name, value))
        if not self.session_id(session):
            return False
        self.colors[session] = value
        return "set" not in self.fail

    def pane_tty(self, pane_id):
        self.calls.append(("tty", pane_id))
        return self.tty

    def _create(self, name, cwd):
        self.calls.append(("new", name, cwd))
        if "new" in self.fail:
            return ""
        n = 50 + len(self.panes)
        self.panes.append(pane(name, pane_id=f"%{n}", window_id=f"@{n}", cmd="zsh", title="", path=cwd or "/Users/x",
                               tty=self.tty or "", attached="0"))
        return f"%{n}"

    def new_session(self, name, cwd):
        return bool(self._create(name, cwd))

    def new_session_pane(self, name, cwd):
        return self._create(name, cwd)

    def has_session(self, name):
        return any(p["session"] == name for p in self.panes)

    def kill_session(self, name):
        self.calls.append(("kill", name))
        if "kill" in self.fail:
            return False
        self.panes = [p for p in self.panes if p["session"] != name]
        return True

    def rename_session(self, old, new):
        self.calls.append(("rename", old, new))
        if "rename" in self.fail:
            return False
        for p in self.panes:
            if p["session"] == old:
                p["session"] = new
        return True

    def detach_session(self, name):
        self.calls.append(("detach", name))
        return "detach" not in self.fail

    def send_keys(self, target, text):
        self.calls.append(("send", target, text))
        return True

    def send_text(self, target, text):
        self.calls.append(("text", target, text))
        return "text" not in self.fail

    def press_enter(self, target):
        self.calls.append(("enter", target))
        return "enter" not in self.fail


class FakeIt2:
    def __init__(self, rows=None, panes=None, available=True, window="pty-W1"):
        self.rows = [dict(r) for r in (rows or [])]
        self.pane_of = dict(panes or {})       # guid -> tmux pane number as a string
        self._available = available
        self.window = window
        self.focused = []
        self.tabs = []
        self.fail_focus = False
        self.fail_tab = False
        self.windows: list[str] = []
        self.new_windows: list[str] = []
        self.get_var_calls = 0

    def available(self):
        return self._available

    def list_sessions(self):
        return [dict(r) for r in self.rows]

    def get_var(self, guid, name):
        self.get_var_calls += 1
        return self.pane_of.get(guid, "") if name == "tmuxWindowPane" else ""

    def focus(self, guid):
        self.focused.append(guid)
        return not self.fail_focus

    def current_window(self):
        return self.window

    def list_windows(self):
        return list(self.windows)

    def new_window(self, command):
        self.new_windows.append(command)
        return (False, "boom") if self.fail_tab else (True, "Created new window: pty-NEW")

    def new_tab(self, command, window=""):
        self.tabs.append((command, window))
        return (False, "boom") if self.fail_tab else (True, "Created new tab: 17")

    def activate(self):
        pass


def it2_json(rows) -> str:
    return json.dumps(rows, ensure_ascii=False)


def write_trail(directory, session_id, lines, mtime=None):
    """Write a real trail file (the jsonl bin/cc-status-tmux appends) and return its path.

    Items of `lines` are dicts (serialised) or raw strings (written verbatim, for malformed lines)."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n" for x in lines), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_status(directory, session_id, **fields):
    """Write one status file, the way `kalmux statusline` does, and return its path."""
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    record = {"ts": 1000, "session_id": session_id, "model": "Opus 5", "context_pct": 42,
              "cost_usd": 1.23, "rate_limits": None}
    record.update(fields)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path
