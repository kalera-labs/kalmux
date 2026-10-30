"""tmhooks: Kalmux's own Claude Code hook entries in ~/.claude/settings.json, and whether they still arrive.

Why Kalmux has entries of its own: until 0.5.1 it rode on the hooks iTerm2 installs (command =
~/.config/iterm2/cc-status) by replacing that path with a symlink to its wrapper. iTerm2 re-links the
path to its own bundle every time a newer version launches, so the wrapper silently stopped running
after the first iTerm2 update. iTerm2 recognises its entries by the word "cc-status" in the command; an
entry whose command does not contain it is invisible to iTerm2, so it is never rewritten, deduplicated or
removed by it. Kalmux therefore adds one entry per event next to iTerm2's, leaves iTerm2's alone, and
gives the cc-status path back to iTerm2.

Both hooks run in parallel on every event. iTerm2 >= 3.7.5 routes the Claude status of a tmux -CC pane to
the right tab by itself, so then the wrapper must not write the status escapes too: the mode is baked into
the command (`--status=iterm2` or `--status=kalmux`) when the entries are written.

Health: the wrapper truncates ${STATE}/hook.heartbeat on every event (its mtime is the last time a hook
reached Kalmux), and ${STATE}/hooks/<session_id> for the session that sent it. A registry transition is
not proof of a hook (an interrupt fires no Stop, an approved permission prompt fires nothing until the
tool ends), so the alarm is narrower: a live Claude session that has been changing state for a while and
has not sent a single hook event since the entries were written. That is the real failure: entries
missing, or a session that never loaded them.
"""
from __future__ import annotations

import copy
import os
import plistlib
import re
import shlex
import time
from pathlib import Path

from . import tmstatusline
from .tmconfig import SESSION_ID_RE

HOOK_NAME = "kalmux-hook"
# every event the wrapper's case statement acts on (tests derive the same list from the script)
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
               "Notification", "Stop", "StopFailure", "SessionEnd")
HOOK_TIMEOUT = 5                       # seconds; the wrapper takes milliseconds and must never hold Claude up
STATUS_ITERM2 = "iterm2"               # iTerm2's own hook writes the Claude status; the wrapper does not
STATUS_KALMUX = "kalmux"               # the wrapper writes the status escapes into the pane itself
STATUS_FLAG = "--status="
ITERM2_MARKERS = ("cc-status", "handle-agent-hook")    # how an iTerm2 hook entry reads, before and after 3.7.7
ITERM2_NATIVE_TMUX = (3, 7, 5)         # first iTerm2 that reports Claude Code status for tmux -CC panes itself
ITERM2_INFO_PLIST = Path("/Applications/iTerm.app/Contents/Info.plist")
ITERM2_BUNDLE_CC_STATUS = Path("/Applications/iTerm.app/Contents/Resources/utilities/cc-status")
WRAPPER_NAMES = ("cc-status-tmux", HOOK_NAME)          # every name the wrapper ever had
HEARTBEAT_NAME = "hook.heartbeat"
WIRED_NAME = "hooks.wired"             # touched whenever the entries are (re)written
SESSION_BEATS = "hooks"                # ${STATE}/hooks/<session_id>: the last hook event of that Claude session
SILENCE_LIMIT = 120                    # seconds of Claude activity with no hook event before it counts as lost
BEAT_KEEP = 86400                      # a gone session's heartbeat file is dropped after a day
PRUNE_BEATS_EVERY = 3600               # seconds between two prunes by a long-lived server
MAX_NAMED = 3                          # sessions named in the health reason; the rest are counted
HOOKS_BACKUP_SUFFIX = ".hooks" + tmstatusline.BACKUP_SUFFIX
LINK_BACKUP_SUFFIX = tmstatusline.BACKUP_SUFFIX
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


# ----------------------------------------------------------------------------- the command
def hook_command(wrapper: Path, mode: str) -> str:
    """What one Kalmux entry runs. Never contains "cc-status": iTerm2 would take the entry for its own."""
    path = str(wrapper)
    if "cc-status" in path:
        raise ValueError(f"{path} contains 'cc-status', so iTerm2 would manage it as its own hook; "
                         "install Kalmux under another path")
    return f"{shlex.quote(path)} {STATUS_FLAG}{mode}"


def _split(command) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return []


def is_kalmux_command(command) -> bool:
    parts = _split(command)
    return bool(parts) and os.path.basename(parts[0]) == HOOK_NAME


def command_parts(command) -> tuple[str, str] | None:
    """(wrapper path, status mode) of a Kalmux entry, None for anything else."""
    if not is_kalmux_command(command):
        return None
    parts = _split(command)
    mode = next((p[len(STATUS_FLAG):] for p in parts[1:] if p.startswith(STATUS_FLAG)), STATUS_KALMUX)
    return parts[0], mode


def _entry(command: str) -> dict:
    return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}


# ----------------------------------------------------------------------------- reading settings.json
def _hooks(settings: dict) -> dict:
    hooks = settings.get("hooks", {})
    if isinstance(hooks, dict) and all(isinstance(groups, list) for groups in hooks.values()):
        return hooks
    raise ValueError("settings.json: 'hooks' must be an object whose values are lists; left alone")


def _commands(groups: list) -> list:
    return [e.get("command") for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)
            for e in g["hooks"] if isinstance(e, dict)]


def kalmux_commands(settings: dict) -> dict[str, list[str]]:
    """event -> the commands of Kalmux's entries for it (only events that have at least one)."""
    found = {ev: [c for c in _commands(groups) if is_kalmux_command(c)] for ev, groups in _hooks(settings).items()}
    return {ev: cmds for ev, cmds in found.items() if cmds}


def iterm2_hooked(settings: dict) -> bool:
    """True when iTerm2's own Claude Code integration has entries in settings.json."""
    return any(isinstance(c, str) and any(m in c for m in ITERM2_MARKERS)
               for groups in _hooks(settings).values() for c in _commands(groups))


def iterm2_version(info_plist: Path = ITERM2_INFO_PLIST) -> tuple[int, ...] | None:
    try:
        raw = plistlib.loads(Path(info_plist).read_bytes()).get("CFBundleShortVersionString", "")
    except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
        return None
    m = _VERSION_RE.match(str(raw))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def status_mode(settings: dict, version: tuple[int, ...] | None) -> str:
    """iterm2 when iTerm2's hook already reports the status of tmux panes, else kalmux."""
    native = version is not None and version >= ITERM2_NATIVE_TMUX and iterm2_hooked(settings)
    return STATUS_ITERM2 if native else STATUS_KALMUX


# ----------------------------------------------------------------------------- editing (pure: new dicts)
def _strip_group(group, keep_one: dict | None, placed: bool) -> tuple[object, bool, bool]:
    """(group, placed, emptied_by_us) with Kalmux's entries removed, or the first replaced by `keep_one`."""
    if not (isinstance(group, dict) and isinstance(group.get("hooks"), list)):
        return group, placed, False
    entries, touched = [], False
    for e in group["hooks"]:
        if isinstance(e, dict) and is_kalmux_command(e.get("command")):
            touched = True
            if keep_one is not None and not placed:
                entries.append(dict(keep_one))
                placed = True
            continue
        entries.append(e)
    return {**group, "hooks": entries}, placed, touched and not entries


def _event_groups(groups: list, keep_one: dict | None) -> tuple[list, bool]:
    out, placed = [], False
    for group in groups:
        new, placed, emptied = _strip_group(group, keep_one, placed)
        if not emptied:
            out.append(new)
    return out, placed


def merged(settings: dict, command: str) -> dict:
    """`settings` with exactly one Kalmux entry per HOOK_EVENTS event running `command`. Every other entry,
    key and key order stays as it was; an entry of ours is rewritten in place rather than moved."""
    out = copy.deepcopy(settings)
    hooks = _hooks(out)
    for event in list(hooks):
        groups, placed = _event_groups(hooks[event], _entry(command) if event in HOOK_EVENTS else None)
        hooks[event] = groups if placed or event not in HOOK_EVENTS else [*groups, {"hooks": [_entry(command)]}]
    for event in HOOK_EVENTS:
        hooks.setdefault(event, [{"hooks": [_entry(command)]}])
    out["hooks"] = hooks
    return out


def without_kalmux(settings: dict) -> dict:
    """`settings` with every Kalmux entry gone, and the groups and events that only held ours."""
    out = copy.deepcopy(settings)
    if "hooks" not in out:
        return out
    hooks = {}
    for event, groups in _hooks(out).items():
        kept, _ = _event_groups(groups, None)
        if kept or not groups:
            hooks[event] = kept
    out["hooks"] = hooks
    return out


# ----------------------------------------------------------------------------- writing settings.json
def _backup_once(settings_path: Path) -> None:
    backup = Path(str(settings_path) + HOOKS_BACKUP_SUFFIX)
    if settings_path.exists() and not backup.exists():
        tmstatusline._write_private(backup, settings_path.read_text())


def install_hooks(settings_path: Path, command: str, state_dir: Path | None = None) -> bool:
    """Write Kalmux's entries. Idempotent: returns False (and leaves the file untouched) when already there."""
    settings_path = Path(settings_path)
    current = tmstatusline.load_settings(settings_path)
    wanted = merged(current, command)
    if wanted == current and settings_path.exists():
        return False
    _backup_once(settings_path)
    tmstatusline.write_settings(settings_path, wanted)
    if state_dir is not None:
        _touch(Path(state_dir) / WIRED_NAME)
    return True


def uninstall_hooks(settings_path: Path) -> bool:
    """Remove Kalmux's entries (the inverse of install_hooks, for an uninstall). False when there were none."""
    settings_path = Path(settings_path)
    current = tmstatusline.load_settings(settings_path)
    wanted = without_kalmux(current)
    if wanted == current:
        return False
    _backup_once(settings_path)
    tmstatusline.write_settings(settings_path, wanted)
    return True


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch()
    os.utime(path)


# ----------------------------------------------------------------------------- what is wrong
def wiring_problems(settings: dict, wrapper: Path, mode: str | None = None) -> list[str]:
    """Plain-English problems with Kalmux's entries ([] = every event runs `wrapper`, in `mode` when given)."""
    found = kalmux_commands(settings)
    missing = [ev for ev in HOOK_EVENTS if ev not in found]
    if len(missing) == len(HOOK_EVENTS):
        return ["no Kalmux entry in settings.json for any hook event"]
    problems = [f"no Kalmux entry for {', '.join(missing)}"] if missing else []
    parts = {command_parts(c) for ev in HOOK_EVENTS for c in found.get(ev, [])}
    for path in sorted({p for p, _ in parts if p != str(wrapper)}):
        problems.append(f"an entry runs {path} (expected {wrapper})")
    if not os.access(wrapper, os.X_OK):
        problems.append(f"{wrapper} is missing or not executable")
    if mode is not None and any(m != mode for _, m in parts):
        problems.append(f"the entries do not match iTerm2 (want {STATUS_FLAG}{mode}: "
                        + ("iTerm2 reports the Claude status itself)" if mode == STATUS_ITERM2 else "the hook reports it)"))
    return problems


def _mtime(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None


def _seconds(value) -> float | None:
    """A registry timestamp (ms) in epoch seconds, or None."""
    return value / 1000 if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _label(key: str, entry: dict) -> str:
    return key.split(":", 1)[0] or entry.get("name") or entry.get("sessionId", "")[:8]


def unreported(state_dir: Path, registry: dict) -> list[str]:
    """The live Claude sessions (tmux session names) that keep changing state but have sent no hook event
    since the entries were written.

    A session counts once its last registry transition is more than SILENCE_LIMIT past the wiring and past
    its own start: one that started after the wiring sends SessionStart at once, so by then a working hook
    has written its file. A session without a usable id is never guessed about."""
    state_dir = Path(state_dir)
    wired = _mtime(state_dir / WIRED_NAME) or 0
    out = []
    for key, entry in registry.items():
        sid, changed = entry.get("sessionId", ""), _seconds(entry.get("statusUpdatedAt"))
        if not entry.get("alive") or changed is None or not SESSION_ID_RE.match(sid or ""):
            continue
        if changed - max(wired, _seconds(entry.get("startedAt")) or 0) <= SILENCE_LIMIT:
            continue
        beat = _mtime(state_dir / SESSION_BEATS / sid)
        if beat is None or beat < wired:
            out.append(_label(key, entry))
    return out


def silence(state_dir: Path, registry: dict) -> tuple[int | None, str]:
    """(last heartbeat, "" or which live Claude sessions say the hooks are not reaching Kalmux)."""
    seen = _mtime(Path(state_dir) / HEARTBEAT_NAME)
    names = unreported(state_dir, registry)
    if not names:
        return seen, ""
    shown = ", ".join(names[:MAX_NAMED]) + (f" +{len(names) - MAX_NAMED} more" if len(names) > MAX_NAMED else "")
    return seen, (f"No hook event from Claude in {shown} since the hooks were wired. Restart a session that "
                  "still does not report (it may predate the wiring); if none report, run Repair (or "
                  "`kalmux doctor --fix`).")


def prune_beats(state_dir: Path, registry: dict, now: float | None = None, keep: int = BEAT_KEEP) -> int:
    """Drop the heartbeat files of Claude sessions that are gone and quiet for `keep` seconds. Best effort."""
    directory = Path(state_dir) / SESSION_BEATS
    alive = {e.get("sessionId") for e in registry.values() if e.get("alive")}
    cutoff = (time.time() if now is None else now) - keep
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.name in alive or not SESSION_ID_RE.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def hook_health(settings_path: Path, wrapper: Path, state_dir: Path, registry: dict) -> dict:
    """{ok, last_seen, reason} with no caching (the CLI asks once)."""
    return _health(_wiring(settings_path, wrapper), state_dir, registry)


def read_problem(settings_path: Path, exc: Exception) -> str:
    """Why settings.json could not be read, naming the path once (load_settings' errors already carry it)."""
    text = str(exc)
    return f"cannot read {text}" if str(settings_path) in text else f"cannot read {settings_path}: {text}"


def _wiring(settings_path: Path, wrapper: Path) -> list[str]:
    try:
        return wiring_problems(tmstatusline.load_settings(settings_path), wrapper)
    except (OSError, ValueError) as exc:
        return [read_problem(settings_path, exc)]


def _health(problems: list[str], state_dir: Path, registry: dict) -> dict:
    seen, quiet = silence(state_dir, registry)
    if problems:
        return {"ok": False, "last_seen": seen,
                "reason": "Claude Code does not call Kalmux's hook: " + "; ".join(problems) + " (settings.json)."}
    return {"ok": not quiet, "last_seen": seen, "reason": quiet}


class HookHealth:
    """hook_health for a long-lived server: settings.json is parsed again only when its mtime or size changes."""

    def __init__(self, settings_path: Path, wrapper: Path, state_dir: Path, clock=time.time) -> None:
        self.settings_path, self.wrapper, self.state_dir = Path(settings_path), Path(wrapper), Path(state_dir)
        self.clock = clock
        self._key: tuple | None = None
        self._problems: list[str] = []
        self._pruned = 0.0

    def _settings_key(self) -> tuple:
        try:
            st = Path(os.path.realpath(self.settings_path)).stat()
            return st.st_mtime_ns, st.st_size, os.access(self.wrapper, os.X_OK)
        except OSError:
            return None, None, None

    def check(self, registry: dict) -> dict:
        key = self._settings_key()
        if key != self._key:
            self._problems, self._key = _wiring(self.settings_path, self.wrapper), key
        now = self.clock()
        if now - self._pruned >= PRUNE_BEATS_EVERY:
            self._pruned = now
            prune_beats(self.state_dir, registry, now)
        return _health(self._problems, self.state_dir, registry)


# ----------------------------------------------------------------------------- migration + the setup step
def points_at_kalmux(link: Path) -> bool:
    return link.is_symlink() and os.path.basename(os.readlink(link)) in WRAPPER_NAMES


def reclaim_iterm2_link(link: Path, bundle: Path = ITERM2_BUNDLE_CC_STATUS) -> str:
    """Give ~/.config/iterm2/cc-status back to iTerm2 when a Kalmux up to 0.5.1 had pointed it at its wrapper.

    Back at the bundle's cc-status when iTerm2 is installed; otherwise the user's own file that setup had
    moved aside comes back, or the link goes. Anything that is not ours is never touched. Returns a note."""
    if not points_at_kalmux(link):
        return ""
    link.unlink()
    if Path(bundle).exists():
        link.symlink_to(bundle)
        return f"gave {link} back to iTerm2 ({bundle})"
    backup = link.with_name(link.name + LINK_BACKUP_SUFFIX)
    if backup.exists():
        os.replace(backup, link)
        return f"put your own {link} back from {backup}"
    return f"removed {link} (it pointed at the Kalmux hook)"


def wire_claude_hooks(settings_path: Path, wrapper: Path, state_dir: Path, hook_link: Path,
                      bundle: Path = ITERM2_BUNDLE_CC_STATUS, info_plist: Path = ITERM2_INFO_PLIST) -> str:
    """The "Claude Code hooks" setup step (also run by `doctor --fix` and the UI's Repair). Idempotent."""
    if not os.access(wrapper, os.X_OK):
        raise OSError(f"{wrapper} is missing or not executable; reinstall Kalmux")
    notes = [n for n in (reclaim_iterm2_link(Path(hook_link), bundle),) if n]
    mode = status_mode(tmstatusline.load_settings(settings_path), iterm2_version(info_plist))
    changed = install_hooks(settings_path, hook_command(wrapper, mode), state_dir)
    who = "status by iTerm2" if mode == STATUS_ITERM2 else "status by Kalmux"
    notes.append(f"{'wrote' if changed else 'already had'} {len(HOOK_EVENTS)} event entries, {who}")
    if changed:
        notes.append(f"backup in {settings_path}{HOOKS_BACKUP_SUFFIX}; running Claude sessions pick the entries "
                     "up when Claude Code reloads its settings: restart one only if Kalmux still says it does not report")
    return "; ".join(notes)


def heartbeat_age(state_dir: Path, now: float | None = None) -> int | None:
    seen = _mtime(Path(state_dir) / HEARTBEAT_NAME)
    return None if seen is None else max(0, int((time.time() if now is None else now) - seen))
