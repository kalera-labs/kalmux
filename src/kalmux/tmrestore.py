"""tmrestore — remember the live tmux sessions, and bring them back after a reboot.

A reboot kills the tmux server with every session in it, and nothing restores them today. Quitting iTerm2
only DETACHES its control-mode clients (iTerm2 3.7.3 sends no tmux command on quit), so a session that
disappears while the machine is up was really killed, while one that disappears across a reboot was not.
That is the whole difference this module records: the UI server, the only long-lived Kalmux process, keeps
the live list in ${STATE}/sessions.json, and the first server of a NEW boot (`kern.boottime`, not "the
tmux server is down": `kalmux ui restart` is not a reboot) recreates what the previous boot still had open.

Name, directory and color come back, one iTerm2 tab each — no windows, no panes, no processes and no
conversation. A restored session is a shell in the right directory, so `claude --continue` picks the
conversation up from there.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .tmcore import CTRL_RE, HEX_RE, IT2_WINDOW_RE, SEP, It2, Tmux, cc_tab_command, fmt_age, valid_session_name

SNAPSHOT_VERSION = 1
SNAPSHOT_EVERY = 5.0             # seconds between two rounds of the keeper thread
UNREACHABLE_GRACE = 30.0         # how long tmux must stay silent before an empty list is believed
BOOT_COMMAND = ("/usr/sbin/sysctl", "-n", "kern.boottime")
BOOT_RE = re.compile(r"sec\s*=\s*(\d+)")      # "{ sec = 1790848664, usec = 496772 } Tue Sep 30 21:37:44 2026"
SESSION_FMT = SEP.join(["#{session_name}", "#{pane_current_path}", "#{@tm_color}", "#{session_created}"])
MAX_CWD = 1024
MAX_SESSIONS = 200               # a bound on what one file can ask the server to create at startup
DRIFT_NOTE = "out of step with tmux"                     # the doctor re-reads once before believing this one

# One SESSION_FMT record. The records are newline-separated, but #{pane_current_path} is whatever the
# directory is called — macOS allows a newline in there — so only the cwd field may span lines, and the
# other three anchor where a record really starts and ends.
_PLAIN = rf"[^{SEP}\n]*"
SESSION_RECORD_RE = re.compile(rf"({_PLAIN}){SEP}([^{SEP}]*?){SEP}({_PLAIN}){SEP}(\d*)(?:\n|\Z)", re.S)


def _quiet(_message: str) -> None:
    """Default log sink: the server passes its own stream, the CLI prints, tests collect."""


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
class Snapshot:
    """The whole file: which boot these sessions belonged to, and when the list was taken."""

    boot: int
    saved_at: int
    sessions: tuple[SavedSession, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sessions)

    def as_dict(self) -> dict:
        return {"version": SNAPSHOT_VERSION, "boot": self.boot, "saved_at": self.saved_at,
                "sessions": [s.as_dict() for s in self.sessions]}


def _record(name: str, cwd: str, color: str, created) -> SavedSession | None:
    """One validated record, or None when the name is not one tmux (or a tab command line) can carry."""
    if not valid_session_name(name):
        return None
    return SavedSession(name=name, cwd=str(cwd)[:MAX_CWD], color=color if HEX_RE.match(color or "") else "",
                        created=int(created) if str(created).isdigit() else 0)


# ----------------------------------------------------------------------------- reading tmux
def live_sessions(tmux: Tmux) -> tuple[SavedSession, ...] | None:
    """The live sessions in creation order, or None when tmux does not answer.

    None is not an empty list: with no server running `list-sessions` exits 1, and writing [] for that
    would erase the very list a reboot needs (hence the Snapshotter's grace period)."""
    rc, out = tmux.run_rc("list-sessions", "-F", SESSION_FMT)
    if rc != 0:
        return None
    # tmux does not escape its data, so a record that does not match whole is never guessed at
    rows = [row for match in SESSION_RECORD_RE.finditer(out) if (row := _record(*match.groups())) is not None]
    rows.sort(key=lambda s: (s.created, s.name))
    return tuple(rows[:MAX_SESSIONS])


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


def read_snapshot(path: Path) -> Snapshot | None:
    """The saved list, or None when the file is missing, unreadable or not one of ours.

    The file is ours, but it is still parsed like external data: it decides what the server creates at
    startup, and a state directory is not a place anything should be trusted blindly."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != SNAPSHOT_VERSION:
        return None
    boot, saved_at, raw = data.get("boot"), data.get("saved_at"), data.get("sessions")
    if not _is_int(boot) or not _is_int(saved_at) or not isinstance(raw, list):
        return None
    return Snapshot(boot=boot, saved_at=saved_at, sessions=tuple(_records(raw)))


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


class Snapshotter:
    """Keeps one snapshot file in step with the live sessions, writing only when the list changed.

    Three threads drive one instance — the keeper's own loop, an HTTP handler right after an action that
    changed the list, and the main thread's final() in the SIGTERM handler — so every round runs under
    this object's own lock. Never the server's: the handler already holds that one when it calls in, and
    a tmux round trip every 5 s under it would stall every request."""

    def __init__(self, path: Path, tmux: Tmux, boot: int, clock: Callable[[], float] = time.time,
                 grace: float = UNREACHABLE_GRACE, log: Callable[[str], None] = _quiet) -> None:
        self.path = Path(path)
        self.tmux, self.boot, self.clock, self.grace, self.log = tmux, boot, clock, grace, log
        self._lock = threading.Lock()
        self._last: tuple[SavedSession, ...] | None = None
        self._unreachable_since: float | None = None

    def tick(self, force: bool = False) -> bool:
        """One round. True when the file was written."""
        with self._lock:
            sessions = live_sessions(self.tmux)
            if sessions is None:
                return self._tick_unreachable()
            self._unreachable_since = None
            return self._write(sessions) if force or sessions != self._last else False

    def final(self) -> bool:
        """The last snapshot before the server stops, skipped unless tmux still answers: it closes the
        5 s window at shutdown without ever replacing a good list with an empty one."""
        with self._lock:
            sessions = live_sessions(self.tmux)
            return self._write(sessions) if sessions is not None else False

    def _tick_unreachable(self) -> bool:
        """tmux is gone. At a shutdown every process gets SIGTERM at once and this one dies long before
        the grace period is up, so the last good list survives the reboot; only a server that stays
        unreachable means the user killed the last session and `exit-empty` stopped it."""
        now = self.clock()
        if self._unreachable_since is None:
            self._unreachable_since = now
        if now - self._unreachable_since < self.grace or self._last == ():
            return False
        return self._write(())

    def _write(self, sessions: tuple[SavedSession, ...]) -> bool:
        if not write_snapshot(self.path, Snapshot(boot=self.boot, saved_at=int(self.clock()), sessions=sessions)):
            self.log(f"restore: could not write {self.path}; the saved session list is now behind")
            return False
        self._last = sessions
        return True


# ----------------------------------------------------------------------------- restoring
def _printable(text: str) -> str:
    """A path as a log line may carry it: tmux hands these over verbatim and a directory name can hold
    anything, including the newline that would forge a second line in ui.log."""
    return CTRL_RE.sub("?", text)


def _destination(session: SavedSession, home: str) -> tuple[str, str]:
    """Where the session starts, plus a note when that is not where it used to be.

    `tmux new-session -c <missing dir>` SUCCEEDS and silently starts the pane in $HOME, so a directory
    that has moved has to be noticed here or it is never reported at all."""
    if session.cwd and os.path.isdir(session.cwd):
        return session.cwd, ""
    return home, f" (its directory {_printable(session.cwd) or '?'} is gone)"


def restore(tmux: Tmux, sessions: Sequence[SavedSession], lock: threading.Lock | None = None,
            log: Callable[[str], None] = _quiet, home: str = "") -> list[str]:
    """Recreate the saved sessions that are not there any more; returns the names created, in order.

    A name that is taken is never stolen and never renamed: whatever runs under it now wins. The lock is
    the server's own, held around the tmux calls only (a few ms per session), never around the tabs."""
    home = home or os.path.expanduser("~")
    created: list[str] = []
    for s in sessions:
        if not valid_session_name(s.name):
            log(f"restore: skipped {s.name!r}: not a session name kalmux can create")
            continue
        if tmux.has_session(s.name):
            log(f"restore: {s.name} already exists; left alone")
            continue
        directory, note = _destination(s, home)
        with lock or contextlib.nullcontext():
            ok = tmux.new_session(s.name, directory)
            if ok and s.color:
                tmux.set_session_option(s.name, "@tm_color", s.color)
        if not ok:
            log(f"restore: tmux refused to create {s.name}")
            continue
        created.append(s.name)
        log(f"restore: created {s.name} in {_printable(directory)}{note}")
    return created


def plan(tmux: Tmux, sessions: Sequence[SavedSession], home: str = "") -> list[str]:
    """What a restore would do, one line per saved session (`kalmux restore --dry-run`)."""
    home = home or os.path.expanduser("~")
    lines = []
    for s in sessions:
        if not valid_session_name(s.name):
            lines.append(f"skip    {s.name!r}: not a session name kalmux can create")
        elif tmux.has_session(s.name):
            lines.append(f"exists  {s.name}")
        else:
            directory, note = _destination(s, home)
            lines.append(f"create  {s.name}  {_printable(directory)}{note}")
    return lines


def _window_id(out: str) -> str:
    """The window id out of `it2 window new`, so every later tab lands in that same window."""
    found = IT2_WINDOW_RE.search(out or "")
    return found.group(1) if found else ""


def _window_just_opened(it2: It2, out: str) -> str:
    """The window the first tab landed in: the id it2 printed, else the front-most window it knows.

    "Created new window: pty-XYZ" is a message of a brand-new CLI, not a contract; without an id every
    later session would open a window of its own instead of a tab in this one."""
    return _window_id(out) or next(iter(it2.list_windows()), "")


def open_tabs(it2: It2, names: Sequence[str], socket: str = "", log: Callable[[str], None] = _quiet) -> int:
    """One control-mode tab per restored session; returns how many opened.

    Right after a reboot there is no iTerm2 window at all (AutoLaunch runs before the first window, and
    this user has OpenNoWindowsAtStartup=1), and `it2 tab new` without --window fails with "No current
    window". So the first session opens a window and the rest go into it; with OpenTmuxWindowsIn=2 every
    attach then keeps its tab in that one window."""
    if not names:
        return 0
    if not it2 or not it2.available():
        log(f"restore: iTerm2 (it2) is not available here; {len(names)} session(s) got no tab")
        return 0
    window, opened, said = it2.current_window() or next(iter(it2.list_windows()), ""), 0, False
    for name in names:
        try:
            command = cc_tab_command(name, socket)
        except ValueError as exc:
            log(f"restore: no tab for {name}: {exc}")
            continue
        ok, out = it2.new_tab(command, window) if window else it2.new_window(command)
        if not ok:
            log(f"restore: it2 could not open a tab for {name}: {out or 'unknown error'}")
            continue
        opened += 1
        if not window:
            window = _window_just_opened(it2, out)
            if not window and not said:
                said = True
                log("restore: it2 did not say which window it opened; every session gets its own window")
    return opened


def _report(ran: bool, reason: str, created: Sequence[str] = (), tabs: int = 0) -> dict:
    return {"ran": ran, "reason": reason, "created": list(created), "tabs": tabs}


def restore_on_start(tmux: Tmux, it2: It2, path: Path, previous_path: Path, boot: int, enabled: bool = True,
                     lock: threading.Lock | None = None, log: Callable[[str], None] = _quiet) -> dict:
    """Decide, keep a copy, recreate: everything the first server of a new boot does before it snapshots.

    The copy is made even when the automatic restore is off, so `kalmux restore` still has the list after
    the server has overwritten sessions.json with the (empty) state of this boot."""
    snap = read_snapshot(path)
    if snap is None:
        return _report(False, f"no saved session list at {path}")
    if not boot or not snap.boot or snap.boot == boot:
        return _report(False, f"not a new boot (this boot {boot}, saved under {snap.boot})")
    copy_snapshot(path, previous_path)
    if not enabled:
        log(f"restore: [restore] enabled = false; {len(snap.sessions)} saved session(s) kept in {previous_path}")
        return _report(False, "[restore] enabled = false")
    log(f"restore: boot {boot} is new (snapshot took boot {snap.boot}); {len(snap.sessions)} session(s) to bring back")
    created = restore(tmux, snap.sessions, lock=lock, log=log)
    tabs = open_tabs(it2, created, socket=getattr(tmux, "socket", ""), log=log)
    log(f"restore: {len(created)} session(s) recreated, {tabs} tab(s) opened")
    return _report(True, "restored", created, tabs)


def restore_source(path: Path, previous_path: Path, boot: int) -> tuple[Snapshot | None, Path | None]:
    """The list `kalmux restore` should use, and the file it came from.

    A sessions.json still carrying another boot means the server has not started since the reboot, so it
    IS the previous boot's list; once the server has rewritten it for this boot, the copy it took first
    (sessions.previous.json) is the one that still holds what was open."""
    current = read_snapshot(path)
    if current is not None and current.boot and current.boot != boot:
        return current, Path(path)
    previous = read_snapshot(previous_path)
    if previous is not None:
        return previous, Path(previous_path)
    return (current, Path(path)) if current is not None else (None, None)


# ----------------------------------------------------------------------------- the server's thread
class SessionKeeper:
    """Restore once at startup, then snapshot every SNAPSHOT_EVERY seconds until the server stops.

    Built only by `kalmux ui serve` (production): a Backend in a test must never start a thread that
    writes the real state directory."""

    def __init__(self, tmux: Tmux, it2: It2, path: Path, previous_path: Path, boot: int | None = None,
                 enabled: bool = True, lock: threading.Lock | None = None, out=None,
                 clock: Callable[[], float] = time.time, every: float = SNAPSHOT_EVERY) -> None:
        self.boot = boot_time() if boot is None else boot
        self.tmux, self.it2, self.previous_path = tmux, it2, Path(previous_path)
        self.enabled, self.lock, self.out, self.every = enabled, lock, out, every
        self.snapshotter = Snapshotter(path, tmux, self.boot, clock, log=self.log)
        self.thread: threading.Thread | None = None
        self._stop = threading.Event()

    def log(self, message: str) -> None:
        print(message, file=self.out or sys.stdout, flush=True)

    def startup(self) -> dict:
        """Restore (when this is a new boot), then record the list for THIS boot even when it is empty,
        so a later `kalmux ui restart` does not run the restore a second time."""
        report = restore_on_start(self.tmux, self.it2, self.snapshotter.path, self.previous_path, self.boot,
                                  enabled=self.enabled, lock=self.lock, log=self.log)
        self.snapshotter.tick(force=True)
        return report

    def run_once(self) -> bool:
        """One snapshot round, from the thread or straight after an action that changed the list."""
        return self.snapshotter.tick()

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self._loop, name="kalmux-sessions", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self) -> None:
        """Wake the thread and take the final snapshot (SIGTERM, i.e. `kalmux ui stop` or a shutdown)."""
        self._stop.set()
        self._guarded(self.snapshotter.final)

    def _loop(self) -> None:
        self._guarded(self.startup)
        while not self._stop.wait(self.every):     # an Event, so a stop does not wait out the interval
            self._guarded(self.run_once)

    def _guarded(self, step: Callable[[], object]) -> None:
        """One bad round (a tmux hiccup, an unwritable state dir) must never take this thread — or the
        server it runs in — down with it."""
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - a snapshot is never worth crashing the ui server
            self.log(f"restore: {type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------------- doctor
def snapshot_health(path: Path, boot: int, live: Sequence[str] | None, now: int) -> tuple[bool, str]:
    """(ok, info) for `kalmux doctor`: does the saved list still describe this boot's live sessions?

    A snapshot that quietly stopped being written is exactly how the Claude trail broke for three weeks
    without anyone noticing, so the freshness of this file is a check of its own. A DRIFT_NOTE verdict
    is the one the caller re-reads after a keeper round: only the UI asks for an immediate snapshot, so
    `kalmux new` on the command line leaves the file behind for up to SNAPSHOT_EVERY seconds."""
    snap = read_snapshot(path)
    if snap is None:
        return False, f"{path} missing or unreadable (the ui server writes it every {int(SNAPSHOT_EVERY)}s)"
    info = f"{len(snap.sessions)} session(s), {fmt_age(max(0, now - snap.saved_at))} old, {path}"
    if boot and snap.boot != boot:
        return False, f"{info} — saved under boot {snap.boot}, this machine booted at {boot}"
    if live is not None and set(live) != set(snap.names):
        return False, f"{info} — {DRIFT_NOTE}: {', '.join(sorted(set(live) ^ set(snap.names)))}"
    return True, info
