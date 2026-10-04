"""tmrestore — bring the tmux sessions back when the server that held them is gone.

A reboot kills the tmux server with every session in it, and so does an iTerm2 crash: AutoLaunch starts
the Kalmux server and the Kalmux server starts tmux, so all three share iTerm2's macOS resource coalition
and go down together (verified from the system log on 2026-10-04). Quitting iTerm2 only DETACHES its
clients, and killing a session is the user saying so, so what decides a restore here is narrower than
"the sessions are gone": it is WHICH tmux server the saved list describes (tmsnapshot writes it down).

The UI server, the only long-lived Kalmux process, keeps that list up to date. When it has sessions and
the server that wrote it is not the one answering now — nothing is running, or another server took the
socket — those sessions were not killed, and they come back: at startup, and on any keeper round that
sees the change. Name, directory and color, one iTerm2 tab each; no windows, no panes, no processes, no
conversation. A restored session is a shell in the right directory, which `claude --continue` needs.
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .tmcore import CTRL_RE, IT2_WINDOW_RE, It2, Tmux, cc_tab_command, fmt_age, valid_session_name
from .tmsnapshot import (
    LiveState,
    SavedSession,
    Snapshot,
    boot_time,
    copy_snapshot,
    live_state,
    read_snapshot,
    write_snapshot,
)

SNAPSHOT_EVERY = 5.0             # seconds between two rounds of the keeper thread
DRIFT_NOTE = "out of step with tmux"                     # the doctor re-reads once before believing this one
STOPPED_NOTE = "restore: stopped: the ui server is shutting down; the next start finishes the restore"


def _quiet(_message: str) -> None:
    """Default log sink: the server passes its own stream, the CLI prints, tests collect."""


def _never() -> bool:
    """Default stop predicate: a restore nothing can interrupt (`kalmux restore` on the command line)."""
    return False


# ----------------------------------------------------------------------------- the one rule
def restore_due(stored: Snapshot | None, live: LiveState | None, boot: int) -> bool:
    """Do the saved sessions need bringing back? The one rule, used at startup and on every keeper round.

    Due when the list has sessions and the server that wrote it is not the one answering now: nothing is
    running (a reboot, or a tmux that died and nobody restarted it), or another server holds the socket
    (an iTerm2 crash took tmux down, and something started a new one). Killing a session is a list that
    shrinks under the SAME server, and is never due.

    A version-1 file carries no identity, so the 0.5.0 rule still decides for it: only a new boot counts,
    and an unknown boot on either side decides nothing — recreating sessions that are still running under
    those names is the one outcome worse than restoring nothing."""
    if stored is None or not stored.sessions:
        return False
    if live is None:
        return True
    if stored.server is not None:
        return stored.server != live.server
    return bool(boot and stored.boot and stored.boot != boot)


class Snapshotter:
    """Keeps one snapshot file in step with the live sessions, writing only when something changed.

    Three threads drive one instance — the keeper's own loop, an HTTP handler right after an action that
    changed the list, and the main thread's final() in the SIGTERM handler — so every round runs under
    this object's own lock. Never the server's: the handler already holds that one when it calls in.

    LOCK ORDER: the server's lock is always taken BEFORE this one (Backend.do holds it while _kept() ->
    run_once() calls in), so nothing here may wait for the server's lock while holding this one — hence
    `on_lost` fires after the lock is released, with the lost snapshot handed over by value. It must also
    return at once (the keeper hands the restore to a thread of its own): the round it interrupts can be
    an HTTP request, and no request may wait for sessions to be recreated and tabs to open."""

    def __init__(self, path: Path, tmux: Tmux, boot: int, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = _quiet, previous_path: Path | None = None,
                 on_lost: Callable[[Snapshot], None] | None = None) -> None:
        self.path = Path(path)
        self.previous_path = Path(previous_path) if previous_path else self.path.with_suffix(".previous.json")
        self.tmux, self.boot, self.clock, self.log, self.on_lost = tmux, boot, clock, log, on_lost
        self._lock = threading.Lock()
        self._last: tuple[SavedSession, ...] | None = None
        # What the file holds, read from disk rather than left empty: the server restart this has to
        # notice happens BEFORE the first round, and an instance that only knew its own writes would
        # compare the new server against nothing and see no change at all.
        self._stored: Snapshot | None = read_snapshot(self.path)

    def tick(self, force: bool = False) -> bool:
        """One round. True when the file was written; a list left behind by a server that is gone is
        handed to `on_lost` afterwards, outside the lock."""
        with self._lock:
            state = live_state(self.tmux)
            if state is None:
                # No server answers, so there is no list to write: an iTerm2 crash takes tmux down with
                # every session still in it, and 0.5.0 lost exactly that list here, writing [] after 30 s.
                return False
            if not (force or self._changed(state)):
                return False
            lost = self._lost_list(state)
            self._keep_previous(state)
            wrote = self._write(state)
        if wrote and lost is not None and self.on_lost is not None:
            self.on_lost(lost)
        return wrote

    def final(self) -> bool:
        """The last snapshot before the server stops, skipped unless tmux still answers: it closes the
        5 s window at shutdown without ever replacing a good list with an empty one."""
        with self._lock:
            state = live_state(self.tmux)
            if state is None:
                return False
            self._keep_previous(state)
            return self._write(state)

    def _changed(self, state: LiveState) -> bool:
        """A different list, or the same list from another server (recorded too, so that a restore
        already done stops being reported as pending)."""
        return state.sessions != self._last or (self._stored is not None and self._stored.server != state.server)

    def _lost_list(self, state: LiveState) -> Snapshot | None:
        """The stored list when its server is gone and it holds names this one does not: the in-flight
        restore (tmux died, the ui server lived on, a new tmux took the socket)."""
        stored = self._stored
        if stored is None or not restore_due(stored, state, self.boot):
            return None
        here = {s.name for s in state.sessions}
        return stored if any(s.name not in here for s in stored.sessions) else None

    def _keep_previous(self, state: LiveState) -> None:
        """Before the file stops describing the old server, put its list where `kalmux restore` can still
        find it — whether or not the automatic restore is on: the copy is what makes the manual one work.

        The same rule that decides a restore decides the copy, so a version-1 file (no identity, never
        equal to a live server) on an unchanged boot — the one case where nothing can be shown to be lost
        — leaves the previous copy alone instead of overwriting it with a list that is still running."""
        stored = self._stored
        if stored is not None and restore_due(stored, state, self.boot):
            copy_snapshot(self.path, self.previous_path)

    def _write(self, state: LiveState) -> bool:
        snap = Snapshot(boot=self.boot, saved_at=int(self.clock()), sessions=state.sessions, server=state.server)
        if not write_snapshot(self.path, snap):
            self.log(f"restore: could not write {self.path}; the saved session list is now behind")
            return False
        self._last, self._stored = state.sessions, snap
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
            log: Callable[[str], None] = _quiet, home: str = "", stop: Callable[[], bool] = _never) -> list[str]:
    """Recreate the saved sessions that are not there any more; returns the names created, in order.

    A name that is taken is never stolen and never renamed: whatever runs under it now wins. The lock is
    the server's own, held around the tmux calls only (a few ms per session), never around the tabs.
    `stop` ends the run between two sessions: a shutdown must not keep creating things, and the list on
    disk is left untouched so the next start picks the restore up where this one stopped."""
    home = home or os.path.expanduser("~")
    created: list[str] = []
    for s in sessions:
        if stop():
            log(STOPPED_NOTE)
            break
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


def open_tabs(it2: It2, names: Sequence[str], socket: str = "", log: Callable[[str], None] = _quiet,
              stop: Callable[[], bool] = _never) -> int:
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
        if stop():
            log(STOPPED_NOTE)
            break
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


def _why(snap: Snapshot, live: LiveState | None, boot: int) -> str:
    """The reason a restore is running, for ui.log: which of the three cases this is."""
    if live is None:
        return "no tmux server is running"
    if snap.server is not None:
        return f"another tmux server holds the socket (saved under pid {snap.server.pid}, live pid {live.server.pid})"
    return f"boot {boot} is new (the saved list took boot {snap.boot})"


def restore_on_start(tmux: Tmux, it2: It2, path: Path, previous_path: Path, boot: int, enabled: bool = True,
                     lock: threading.Lock | None = None, log: Callable[[str], None] = _quiet,
                     stop: Callable[[], bool] = _never) -> dict:
    """Decide, keep a copy, recreate: everything a starting ui server does before its first snapshot.

    The copy is made even when the automatic restore is off, so `kalmux restore` still has the list after
    the server has overwritten sessions.json with the state of the tmux server that runs now."""
    snap = read_snapshot(path)
    if snap is None:
        return _report(False, f"no saved session list at {path}")
    live = live_state(tmux)
    if not restore_due(snap, live, boot):
        return _report(False, f"the saved list belongs to the tmux server that is running "
                              f"(this boot {boot}, saved under {snap.boot})")
    copy_snapshot(path, previous_path)
    if not enabled:
        log(f"restore: [restore] enabled = false; {len(snap.sessions)} saved session(s) kept in {previous_path}")
        return _report(False, "[restore] enabled = false")
    log(f"restore: {_why(snap, live, boot)}; {len(snap.sessions)} session(s) to bring back")
    created = restore(tmux, snap.sessions, lock=lock, log=log, stop=stop)
    tabs = open_tabs(it2, created, socket=getattr(tmux, "socket", ""), log=log, stop=stop)
    log(f"restore: {len(created)} session(s) recreated, {tabs} tab(s) opened")
    return _report(True, "restored", created, tabs)


def restore_source(path: Path, previous_path: Path, live: LiveState | None,
                   boot: int) -> tuple[Snapshot | None, Path | None]:
    """The list `kalmux restore` should use, and the file it came from.

    A sessions.json whose server is not the live one is the lost list itself: no ui server has rewritten
    it since the crash (or the reboot). Once the live server owns the file, the copy taken just before
    that write — sessions.previous.json — is the one that still holds what was open."""
    current = read_snapshot(path)
    if current is not None and restore_due(current, live, boot):
        return current, Path(path)
    previous = read_snapshot(previous_path)
    if previous is not None:
        return previous, Path(previous_path)
    return (current, Path(path)) if current is not None else (None, None)


# ----------------------------------------------------------------------------- the server's thread
class SessionKeeper:
    """Restore once at startup, then snapshot every SNAPSHOT_EVERY seconds until the server stops, and
    restore again on any round where the tmux server turns out to have been replaced.

    Built only by `kalmux ui serve` (production): a Backend in a test must never start a thread that
    writes the real state directory."""

    def __init__(self, tmux: Tmux, it2: It2, path: Path, previous_path: Path, boot: int | None = None,
                 enabled: bool = True, lock: threading.Lock | None = None, out=None,
                 clock: Callable[[], float] = time.time, every: float = SNAPSHOT_EVERY) -> None:
        self.boot = boot_time() if boot is None else boot
        self.tmux, self.it2, self.previous_path = tmux, it2, Path(previous_path)
        self.enabled, self.lock, self.out, self.every = enabled, lock, out, every
        # `[restore] enabled = false` still records the list, still keeps the copy of a lost one (the
        # Snapshotter does both) and still says so in ui.log; only the recreating is off.
        self.snapshotter = Snapshotter(path, tmux, self.boot, clock, log=self.log, previous_path=self.previous_path,
                                       on_lost=self._restore_lost if enabled else self._note_lost)
        self.thread: threading.Thread | None = None
        self.restorer: threading.Thread | None = None
        self._stop = threading.Event()
        # Held by whatever is restoring right now (startup or an in-flight round), so the final snapshot
        # at SIGTERM can tell a half-finished restore from a quiet server and leave the saved list alone.
        self._busy = threading.Lock()

    def log(self, message: str) -> None:
        print(message, file=self.out or sys.stdout, flush=True)

    def startup(self) -> dict:
        """Restore (when the saved list's server is gone), then record the list of the server running
        now even when it is empty, so neither a later `ui restart` nor the next round restores twice.

        Nothing is recorded when the shutdown arrived mid-restore: the list that is still lost has to
        stay lost on disk, or the next start sees a file that matches the live server and gives up."""
        report = restore_on_start(self.tmux, self.it2, self.snapshotter.path, self.previous_path, self.boot,
                                  enabled=self.enabled, lock=self.lock, log=self.log, stop=self._stop.is_set)
        if self._stop.is_set():
            return report
        self.snapshotter.tick(force=True)
        return report

    def _note_lost(self, stored: Snapshot) -> None:
        """The same round with the automatic restore off: nothing is recreated, but a list lost without a
        word is how the 0.5.0 crash went unnoticed, so ui.log says what was kept and where."""
        self.log(f"restore: the tmux server changed; [restore] enabled = false; {len(stored.sessions)} session(s) "
                 f"kept in {self.previous_path} (kalmux restore)")

    def _restore_lost(self, stored: Snapshot) -> None:
        """A snapshot round found the saved list's server replaced: bring back what the new one lacks.

        On a thread of its own, because a round is ticked from the keeper's loop AND from inside an HTTP
        request that holds the server's lock (Backend.do -> _kept -> run_once): recreating sessions and
        opening a tab each is seconds of it2 calls, and neither that request nor the actions queued
        behind that lock may wait for them. The new thread holds no caller lock, so it can take the
        server's own around the tmux calls, exactly like the startup path. Guarded: a tmux hiccup there
        must take down neither the keeper nor the ui server."""
        self.restorer = threading.Thread(target=self._guarded, args=(lambda: self._bring_back(stored),),
                                         name="kalmux-restore", daemon=True)
        self.restorer.start()

    def _bring_back(self, stored: Snapshot) -> None:
        """The in-flight restore itself, on the thread _restore_lost started: never call this inline from
        a thread holding the server's lock — `_busy` can be held by a startup that is waiting for it."""
        with self._busy:
            if self._stop.is_set():
                return
            self.log(f"restore: the tmux server changed; {len(stored.sessions)} session(s) saved under the old one")
            created = restore(self.tmux, stored.sessions, lock=self.lock, log=self.log, stop=self._stop.is_set)
            tabs = open_tabs(self.it2, created, socket=getattr(self.tmux, "socket", ""), log=self.log,
                             stop=self._stop.is_set)
            self.log(f"restore: {len(created)} session(s) recreated, {tabs} tab(s) opened")

    def run_once(self) -> bool:
        """One snapshot round, from the thread or straight after an action that changed the list."""
        return self.snapshotter.tick()

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self._loop, name="kalmux-sessions", daemon=True)
        self.thread.start()
        return self.thread

    def stop(self) -> None:
        """Wake the thread and take the final snapshot (SIGTERM, i.e. `kalmux ui stop` or a shutdown).

        Never while a restore is running: the restoring thread is a daemon and dies with the interpreter,
        and a final snapshot taken half-way through would record the handful of sessions recreated so far
        as this server's own list. The next start would then find nothing to restore and the rest would
        be gone. Leaving the file as it is costs the last few seconds of session changes and keeps the
        restore pending, which is the one of the two the next start can still fix."""
        self._stop.set()
        if not self._busy.acquire(blocking=False):
            self.log("restore: a restore is in flight; the final snapshot is skipped so the next start finishes it")
            return
        try:
            self._guarded(self.snapshotter.final)
        finally:
            self._busy.release()

    def _loop(self) -> None:
        with self._busy:                           # a stop during the startup restore skips final()
            if not self._stop.is_set():
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
def snapshot_health(path: Path, boot: int, live: LiveState | None, now: int) -> tuple[bool, str]:
    """(ok, info) for `kalmux doctor`: does the saved list still describe the live tmux server?

    A snapshot that quietly stopped being written is exactly how the Claude trail broke for three weeks
    without anyone noticing, so the freshness of this file is a check of its own. A DRIFT_NOTE verdict
    is the one the caller re-reads after a keeper round: only the UI asks for an immediate snapshot, so
    `kalmux new` on the command line leaves the file behind for up to SNAPSHOT_EVERY seconds. A list
    whose server is gone is a restore that has not happened — the names can agree and still be wrong,
    and so can a tmux that is not running at all, which is exactly when this check is consulted."""
    snap = read_snapshot(path)
    if snap is None:
        return False, f"{path} missing or unreadable (the ui server writes it every {int(SNAPSHOT_EVERY)}s)"
    info = f"{len(snap.sessions)} session(s), {fmt_age(max(0, now - snap.saved_at))} old, {path}"
    if boot and snap.boot != boot:
        return False, f"{info} — saved under boot {snap.boot}, this machine booted at {boot}"
    if restore_due(snap, live, boot):
        return False, f"{info} — restore pending: {_why(snap, live, boot)} (kalmux restore)"
    if live is None:
        return True, info                        # nothing saved and no server: nothing is waiting to happen
    names = {s.name for s in live.sessions}
    if names != set(snap.names):
        return False, f"{info} — {DRIFT_NOTE}: {', '.join(sorted(names ^ set(snap.names)))}"
    return True, info
