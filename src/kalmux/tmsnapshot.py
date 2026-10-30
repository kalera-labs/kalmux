"""tmsnapshot — the session snapshot: what tmux answers right now, and the file that remembers it.

The file (${STATE}/sessions.json) is the only thing standing between a tmux server that is gone and the
projects that were open in it, so everything here is deliberately dull: two tmux reads, a validated
record, and an atomic private write. The decisions live in tmrestore; this module only tells the truth
about what is running and what was saved.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .tmcore import HEX_RE, INTENDED_OPTION, RESTORED_OPTION, SEP, Tmux, valid_session_name

SNAPSHOT_VERSION = 2             # 2 adds "server"; 1 (0.5.0, no identity) is still read
READABLE_VERSIONS = (1, 2)
BOOT_COMMAND = ("/usr/sbin/sysctl", "-n", "kern.boottime")
BOOT_RE = re.compile(r"sec\s*=\s*(\d+)")      # "{ sec = 1790848664, usec = 496772 } Tue Sep 30 21:37:44 2026"
SESSION_FMT = SEP.join(["#{session_name}", "#{pane_current_path}", "#{" + INTENDED_OPTION + "}", "#{@tm_color}",
                        "#{session_created}"])
SERVER_FMT = SEP.join(["#{pid}", "#{start_time}"])
MAX_CWD = 1024
MAX_SESSIONS = 200               # a bound on what one file can ask the server to create at startup

# One SESSION_FMT record. The records are newline-separated, but #{pane_current_path} is whatever the
# directory is called (macOS allows a newline in there), and so is the intended directory a restore left
# behind: only those two fields may span lines, and the other three anchor where a record really starts
# and ends. No field can hold SEP, so the two path fields never run into each other.
_PLAIN = rf"[^{SEP}\n]*"
_PATH = rf"[^{SEP}]*?"
SESSION_RECORD_RE = re.compile(rf"({_PLAIN}){SEP}({_PATH}){SEP}({_PATH}){SEP}({_PLAIN}){SEP}(\d*)(?:\n|\Z)", re.S)


# ----------------------------------------------------------------------------- boot time
def _sysctl(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return p.returncode, p.stdout.decode("utf-8", "replace")


def boot_time(runner: Callable[[list[str]], tuple[int, str]] | None = None) -> int:
    """The second this kernel booted, or 0 when it cannot be read (non-macOS included).

    0 means "unknown" and disables the restore on both sides of the comparison: a wrong "this is a new
    boot" would recreate sessions that are still running under those names."""
    rc, out = (runner or _sysctl)(list(BOOT_COMMAND))
    found = BOOT_RE.search(out) if rc == 0 else None
    return int(found.group(1)) if found else 0


# ----------------------------------------------------------------------------- the records
@dataclass(frozen=True)
class SavedSession:
    """One line of the snapshot: everything a restore can bring back."""

    name: str
    cwd: str = ""
    color: str = ""
    created: int = 0

    def as_dict(self) -> dict:
        return {"name": self.name, "cwd": self.cwd, "color": self.color, "created": self.created}


@dataclass(frozen=True)
class ServerId:
    """Which tmux server a list came from: its pid and the second it started.

    Neither half is enough on its own — macOS reuses pids, and two servers can start in the same second
    — and together they answer the one question here: is this the server that wrote the list?"""

    pid: int
    started: int

    def as_dict(self) -> dict:
        return {"pid": self.pid, "started": self.started}


@dataclass(frozen=True)
class LiveState:
    """What tmux answers right now: which server, and the sessions in it (an empty tuple is an answer)."""

    server: ServerId
    sessions: tuple[SavedSession, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """The whole file: which server (and boot) these sessions belonged to, and when the list was taken."""

    boot: int
    saved_at: int
    sessions: tuple[SavedSession, ...] = ()
    server: ServerId | None = None            # None = a version-1 file: only the boot can decide for it

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sessions)

    def as_dict(self) -> dict:
        return {"version": SNAPSHOT_VERSION, "boot": self.boot, "saved_at": self.saved_at,
                "server": self.server.as_dict() if self.server is not None else None,
                "sessions": [s.as_dict() for s in self.sessions]}


def _record(name: str, cwd: str, color: str, created) -> SavedSession | None:
    """One validated record, or None when the name is not one tmux (or a tab command line) can carry."""
    if not valid_session_name(name):
        return None
    return SavedSession(name=name, cwd=str(cwd)[:MAX_CWD], color=color if HEX_RE.match(color or "") else "",
                        created=int(created) if str(created).isdigit() else 0)


# ----------------------------------------------------------------------------- the restore's marks
def _homes(home: str = "") -> set[str]:
    home = home or os.path.expanduser("~")
    return {home, os.path.realpath(home)}


def recorded_cwd(path: str, intended: str, home: str = "") -> str:
    """The directory a session counts as being in: the one a restore could not put it in (INTENDED_OPTION)
    for as long as the pane still sits in the $HOME it fell back to, else wherever the pane is.

    Without this the first snapshot after a fallback writes $HOME over the real directory, and the
    session never finds its way back (a project on a USB volume that was not mounted yet at login)."""
    return intended if intended and path in _homes(home) else path


def restored_at(row: dict) -> int:
    """The second a restore recreated this pane's session, or 0 (no mark, or not a number)."""
    value = str(row.get("restored") or "")
    return int(value) if value.isdigit() else 0


def _attached(row: dict) -> bool:
    return str(row.get("attached") or "0") not in ("", "0")


def pending_restored(panes: list[dict]) -> dict[str, int]:
    """name -> when, for every session a restore recreated that no client has attached yet, in the order
    tmux lists them. A client that attached is an opened session, whether or not the mark is cleared yet."""
    out: dict[str, int] = {}
    for p in panes:
        at = restored_at(p)
        if at and not _attached(p):
            out.setdefault(p["session"], at)
    return out


def opened_restored(panes: list[dict]) -> list[str]:
    """Marked sessions a client has attached (a tab, `tmux attach`, ssh): marks only left to clear."""
    return list(dict.fromkeys(p["session"] for p in panes if restored_at(p) and _attached(p)))


def displaced(panes: list[dict], home: str = "") -> dict[str, str]:
    """name -> intended directory, for every session still sitting in the $HOME a restore put it in while
    that directory is still missing (once it is there, a restore puts the session back in it)."""
    homes = _homes(home)
    return {p["session"]: p["intended"] for p in panes
            if p.get("intended") and p.get("path") in homes and not os.path.isdir(p["intended"])}


def moved_on(panes: list[dict], home: str = "") -> list[str]:
    """Sessions carrying an intended directory whose active pane has left $HOME: the user took the session
    somewhere, so the directory a restore could not use stops speaking for it."""
    homes = _homes(home)
    return list(dict.fromkeys(p["session"] for p in panes if p.get("intended") and p.get("path") not in homes
                              and p.get("window_active") == "1" and p.get("pane_active") == "1"))


def mark_restored(tmux: Tmux, name: str, when: int) -> bool:
    return tmux.set_session_option(name, RESTORED_OPTION, str(int(when)))


def clear_restored(tmux: Tmux, name: str) -> bool:
    return tmux.set_session_option(name, RESTORED_OPTION, None)


# ----------------------------------------------------------------------------- reading tmux
def live_sessions(tmux: Tmux, home: str = "") -> tuple[SavedSession, ...] | None:
    """The live sessions in creation order, or None when tmux does not answer.

    None is not an empty list: with no server running `list-sessions` exits 1, and writing [] for that
    would erase the very list a crash or a reboot needs."""
    rc, out = tmux.run_rc("list-sessions", "-F", SESSION_FMT)
    if rc != 0:
        return None
    # tmux does not escape its data, so a record that does not match whole is never guessed at
    rows = []
    for match in SESSION_RECORD_RE.finditer(out):
        name, path, intended, color, created = match.groups()
        row = _record(name, recorded_cwd(path, intended, home), color, created)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda s: (s.created, s.name))
    return tuple(rows[:MAX_SESSIONS])


def _parse_server(out: str) -> ServerId | None:
    """The ServerId in one `display -p` line, or None when it is not two positive integers: an identity
    half-read from a truncated line would compare equal to a server that never wrote it."""
    pid, sep, started = out.strip().partition(SEP)
    if not sep or not pid.isdigit() or not started.isdigit() or not int(pid) or not int(started):
        return None
    return ServerId(pid=int(pid), started=int(started))


def live_state(tmux: Tmux) -> LiveState | None:
    """The server answering on this socket and the sessions in it, or None when there is no server.

    `display -p` comes first because it answers even with zero sessions (verified on tmux 3.7c with
    `exit-empty off`) — the one case `list-sessions` cannot tell from "no server". Both calls must
    succeed: a server that goes away between them would otherwise be recorded as empty."""
    rc, out = tmux.run_rc("display", "-p", SERVER_FMT)
    server = _parse_server(out) if rc == 0 else None
    if server is None:
        return None
    sessions = live_sessions(tmux)
    return None if sessions is None else LiveState(server=server, sessions=sessions)


# ----------------------------------------------------------------------------- the file
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _records(raw: list) -> Iterator[SavedSession]:
    for item in raw[:MAX_SESSIONS]:
        if not isinstance(item, dict):
            continue
        row = _record(str(item.get("name", "")), str(item.get("cwd") or ""), str(item.get("color") or ""),
                      item.get("created") if _is_int(item.get("created")) else 0)
        if row is not None:
            yield row


def _read_server(raw) -> ServerId | None:
    """The identity in a snapshot file, or None ("unknown") for anything that is not two ints > 0.

    A version-1 file has none at all, and a malformed one reads the same way rather than rejecting the
    list: those sessions are still worth bringing back under the boot rule."""
    if not isinstance(raw, dict):
        return None
    pid, started = raw.get("pid"), raw.get("started")
    if not _is_int(pid) or not _is_int(started) or pid <= 0 or started <= 0:
        return None
    return ServerId(pid=pid, started=started)


def read_snapshot(path: Path) -> Snapshot | None:
    """The saved list, or None when the file is missing, unreadable or not one of ours.

    The file is ours, but it is still parsed like external data: it decides what the server creates at
    startup, and a state directory is not a place anything should be trusted blindly."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not _is_int(data.get("version")) or data["version"] not in READABLE_VERSIONS:
        return None
    boot, saved_at, raw = data.get("boot"), data.get("saved_at"), data.get("sessions")
    if not _is_int(boot) or not _is_int(saved_at) or not isinstance(raw, list):
        return None
    return Snapshot(boot=boot, saved_at=saved_at, sessions=tuple(_records(raw)), server=_read_server(data.get("server")))


def write_snapshot(path: Path, snap: Snapshot) -> bool:
    """Replace the file atomically (temp + os.replace), 0600 inside a 0700 directory.

    It lists every project the user has open, so it is created private from the first byte rather than
    chmod-ed afterwards (mkstemp opens 0600). The temp name is unique per writer: a shared one is a
    shared, truncated buffer, and two writers in it rename a half-and-half file into place. Returns
    False on any OSError: a snapshot is never worth an exception."""
    path = Path(path)
    raw = json.dumps(snap.as_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


def copy_snapshot(path: Path, destination: Path) -> bool:
    """Keep the previous boot's list under its own name before the restore touches anything."""
    snap = read_snapshot(path)
    return write_snapshot(destination, snap) if snap is not None else False
