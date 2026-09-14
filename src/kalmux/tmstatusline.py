"""tmstatusline — the `kalmux statusline` tap that Claude Code runs as `statusLine.command`.

Claude Code feeds one JSON object on stdin every time the status line refreshes. The tap keeps a
normalized copy under `${STATE}/status/<session_id>.json` (that is where the context gauge in `kalmux ls`
and in the UI reads from) and then hands the very same bytes to the command the user had configured
before kalmux took the key over, exiting with its status.

Two rules drive the whole module: the user's status line must never disappear because of kalmux (any
internal error still passes through), and the saved original is never overwritten with our own wrapper —
including the wrapper this tool installed under one of its previous names (`kmux`, `tm`).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .tmconfig import SESSION_ID_RE, load_config

MAX_STDIN = 1 << 20                 # 1 MiB: the payload is a few KiB; anything bigger is not ours to relay
FRESH_SECONDS = 600                 # a status file older than this belongs to a session that stopped refreshing
SAVED_NAME = "statusline.json"      # ${STATE}/statusline.json: the user's original statusLine object
RESTORED_SUFFIX = ".restored"       # uninstall renames the saved file instead of deleting it
STATUS_SUBDIR = "status"
BACKUP_SUFFIX = ".bak-kalmux"
SETTINGS_MODE = 0o600               # what a settings.json we create ourselves gets (it can hold API keys)
DEPTH_ENV = "KALMUX_STATUSLINE_DEPTH"  # set on the child, so a saved command calling us back cannot loop
PRUNE_STAMP = ".prune-stamp"
PRUNE_EVERY = 600                   # at most one sweep of the status dir every 10 minutes
WRAPPER_ARG = "statusline"
WRAPPER_NAMES = ("kalmux", "kmux", "tm")   # every name this wrapper was ever installed under
RATE_KEYS = ("five_hour", "seven_day", "spend_limit")
MAX_TEXT = 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ----------------------------------------------------------------------------- paths
def status_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / STATUS_SUBDIR


def saved_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / SAVED_NAME


def wrapper_command(tm_link: Path | str) -> str:
    """The command we write into settings.json (quoted only when the path needs it)."""
    return f"{shlex.quote(str(tm_link))} {WRAPPER_ARG}"


def is_ours(command: str | None, tm_link: Path | str | None = None) -> bool:
    """True when this statusLine command is our tap (quoted or not, under any name in WRAPPER_NAMES).

    Recognising the OLD names matters more than the new one: a machine routed through `kmux statusline`
    must be re-routed to kalmux without the kmux wrapper ever being mistaken for the user's own command.
    """
    text = (command or "").strip()
    if not text:
        return False
    if tm_link is not None and text == f"{tm_link} {WRAPPER_ARG}":
        return True
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if len(parts) != 2 or parts[-1] != WRAPPER_ARG:
        return False
    return os.path.basename(parts[0]) in WRAPPER_NAMES


# ----------------------------------------------------------------------------- payload -> record
def _text(value, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    clean = _CONTROL_RE.sub(" ", value).strip()[:limit]
    return clean or None


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int(value) -> int | None:
    num = _num(value)
    return None if num is None else round(num)


def _epoch(value) -> int | None:
    """Seconds since the epoch from a number or an ISO-8601 timestamp (both shapes seen in the wild)."""
    num = _num(value)
    if num is not None:
        return int(num)
    text = _text(value, 64)
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _rate_limits(raw) -> dict | None:
    """The quota clocks, when the account has them (absent on Vertex / API-key setups)."""
    out: dict[str, dict] = {}
    for key in RATE_KEYS:
        item = _dict(_dict(raw).get(key))
        used, resets = _int(item.get("used_percentage")), _epoch(item.get("resets_at"))
        if used is not None or resets is not None:
            out[key] = {"used_pct": used, "resets_at": resets}
    return out or None


def normalize(payload, now: int | None = None) -> dict | None:
    """The status record for one session, or None when the payload carries no usable session id."""
    session_id = _dict(payload).get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    model, ctx = _dict(payload.get("model")), _dict(payload.get("context_window"))
    effort = payload.get("effort")
    cost = _num(_dict(payload.get("cost")).get("total_cost_usd"))
    tokens = ctx.get("current_usage") if _int(ctx.get("current_usage")) is not None else ctx.get("total_input_tokens")
    return {
        "ts": int(time.time()) if now is None else int(now),
        "session_id": session_id,
        "model": _text(model.get("display_name")),
        "model_id": _text(model.get("id")),
        "effort": _text(_dict(effort).get("level") if isinstance(effort, dict) else effort),
        "context_pct": _int(ctx.get("used_percentage")),
        "context_tokens": _int(tokens),
        "context_size": _int(ctx.get("context_window_size")),
        "cost_usd": None if cost is None else round(cost, 4),
        "cwd": _text(_dict(payload.get("workspace")).get("current_dir")) or _text(payload.get("cwd")),
        "transcript_path": _text(payload.get("transcript_path"), 4096),
        "version": _text(payload.get("version"), 64),
        "rate_limits": _rate_limits(payload.get("rate_limits")),
    }


def write_status(record: dict, state_dir: Path | str) -> bool:
    """Atomically replace this session's status file. Best effort: a read-only state dir is not an error."""
    directory = status_dir(state_dir)
    tmp = directory / f".{record['session_id']}.{os.getpid()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp.write_text(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(tmp, directory / f"{record['session_id']}.json")
        return True
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


# ----------------------------------------------------------------------------- the tap
def saved_object(state_dir: Path | str) -> dict:
    """The whole saved file ({} when it is missing or malformed)."""
    try:
        data = json.loads(saved_path(state_dir).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def saved_present(state_dir: Path | str) -> bool:
    """True while the saved original is on disk (a saved EMPTY object counts: it means the user had none)."""
    try:
        return saved_path(state_dir).exists()
    except OSError:
        return False


def saved_statusline(state_dir: Path | str):
    """The statusLine value kalmux replaced, verbatim ({} when the user had none).

    Files written before 0.4.1 only kept command/type/padding, so rebuild the object from those.
    """
    saved = saved_object(state_dir)
    if "statusLine" in saved:
        return saved["statusLine"]
    command = saved.get("command")
    if not isinstance(command, str) or not command:
        return {}
    legacy = {"type": saved.get("type") or "command", "command": command}
    if saved.get("padding") is not None:
        legacy["padding"] = saved["padding"]
    return legacy


def saved_command(state_dir: Path | str) -> str:
    saved = saved_object(state_dir)
    command = saved.get("command")
    if not isinstance(command, str):
        command = _dict(saved.get("statusLine")).get("command")
    return command if isinstance(command, str) else ""


def default_line(record: dict | None) -> str:
    """What we print when the user had no status line of their own."""
    if not record:
        return "kalmux"
    bits = [record.get("model") or "claude"]
    if isinstance(record.get("context_pct"), int):
        bits.append(f"ctx {record['context_pct']}%")
    return " · ".join(bits)


def self_reference(command: str) -> str:
    """Why this saved command must not be run, or "" when it is safe to hand the payload over."""
    if is_ours(command) or any(f"{name} {WRAPPER_ARG}" in command for name in WRAPPER_NAMES):
        return "the saved original is the kalmux wrapper itself"
    if os.environ.get(DEPTH_ENV):
        return f"{DEPTH_ENV} is set, so kalmux is already running inside its own status line"
    return ""


def passthrough(data: bytes, command: str, record: dict | None, stdout=None) -> int:
    """Feed the original command the same bytes and return its status; fall back to our own one-liner."""
    if command:
        loop = self_reference(command)
        if loop:
            print(f"kalmux statusline: {loop}; printing the default line instead "
                  "(fix: `kalmux statusline uninstall`, then install again)", file=sys.stderr)
        else:
            try:
                return subprocess.run(["/bin/sh", "-c", command], input=data, check=False,
                                      env={**os.environ, DEPTH_ENV: "1"}).returncode
            except (OSError, subprocess.SubprocessError):
                pass
    print(default_line(record), file=sys.stdout if stdout is None else stdout)
    return 0


def run_tap(state_dir: Path | str, stdin=None, stdout=None, now: int | None = None) -> int:
    """`kalmux statusline`: record the payload, then hand it to whatever ran here before kalmux."""
    stream = sys.stdin.buffer if stdin is None else stdin
    try:
        data = stream.read(MAX_STDIN) or b""
    except OSError:
        data = b""
    record = None
    try:
        record = normalize(json.loads(data.decode("utf-8", "replace")), now) if data.strip() else None
    except ValueError:
        record = None
    if record and write_status(record, state_dir) and _prune_due(state_dir, time.time()):
        prune_status(state_dir)
    return passthrough(data, saved_command(state_dir), record, stdout)


# ----------------------------------------------------------------------------- pruning the status dir
def _keep_days() -> int:
    """`tombstones.keep_days` from the user config - status files follow the same retention as the trails."""
    try:
        return max(int(load_config()["tombstones"]["keep_days"]), 1)
    except (KeyError, TypeError, ValueError):
        return 30


def _prune_due(state_dir: Path | str, now: float) -> bool:
    """The sweep costs a directory walk, so it runs at most once every PRUNE_EVERY seconds."""
    try:
        return now - (status_dir(state_dir) / PRUNE_STAMP).stat().st_mtime >= PRUNE_EVERY
    except OSError:
        return True


def prune_status(state_dir: Path | str, keep_days: int | None = None, now: float | None = None) -> int:
    """Drop status files of sessions nothing will ask about again. Best effort; returns how many went away."""
    days = _keep_days() if keep_days is None else keep_days
    cutoff = (time.time() if now is None else float(now)) - days * 86400
    directory = status_dir(state_dir)
    removed = 0
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        entries = []
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    try:
        (directory / PRUNE_STAMP).touch()
    except OSError:
        pass
    return removed


# ----------------------------------------------------------------------------- install / uninstall
def load_settings(settings_path: Path | str) -> dict:
    """Claude Code's settings.json as a dict ({} when it does not exist). Raises ValueError on a broken file."""
    try:
        data = json.loads(Path(settings_path).read_text())
    except FileNotFoundError:
        return {}
    if isinstance(data, dict):
        return data
    raise ValueError(f"{settings_path}: expected a JSON object, found {type(data).__name__}")


def write_settings(settings_path: Path | str, settings: dict) -> None:
    """Replace settings.json atomically, keeping its mode and writing THROUGH a symlink rather than over it."""
    target = Path(os.path.realpath(Path(settings_path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode = SETTINGS_MODE                     # a file we create ourselves stays private: it can hold API keys
    tmp = target.with_name(target.name + f".kalmux-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, target)


def _write_private(path: Path, text: str) -> None:
    """Write a file only the user can read, whether or not it already existed."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SETTINGS_MODE)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(path, SETTINGS_MODE)


def save_original(state_dir: Path | str, statusline) -> bool:
    """Remember the user's statusLine value verbatim, once. Never overwritten, so a second install cannot eat it.

    `command` / `type` / `padding` stay at the top level for compatibility (an older kalmux, and the tap, read
    them); the lossless copy lives under `statusLine`.
    """
    path = saved_path(state_dir)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = _dict(statusline)
    record = {"statusLine": {} if statusline is None else statusline,
              "command": current.get("command") if isinstance(current.get("command"), str) else "",
              "type": current.get("type") or "command",
              "padding": current.get("padding"),
              "saved_at": int(time.time())}
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return True


def install(settings_path: Path | str, tm_link: Path | str, state_dir: Path | str) -> bool:
    """Route statusLine through kalmux. Returns False when it was already routed here. Undone by uninstall().

    A command that is one of OUR older wrappers (`kmux statusline`, `tm statusline`) is re-pointed at
    `tm_link` but never saved as the original: the user's real status line was recorded the first time
    round, and overwriting that record with our own wrapper would lose it for good.
    """
    settings = load_settings(settings_path)
    raw = settings.get("statusLine")
    current = _dict(raw)
    command = current.get("command")
    if isinstance(command, str) and command.strip() in (wrapper_command(tm_link), f"{tm_link} {WRAPPER_ARG}"):
        return False
    if not is_ours(command):
        save_original(state_dir, raw)
    path = Path(settings_path)
    if path.exists():
        _write_private(Path(str(path) + BACKUP_SUFFIX), path.read_text())
    # copy the original object, so keys we know nothing about (padding, refreshMs, ...) survive the round trip
    settings["statusLine"] = {**current, "type": "command", "command": wrapper_command(tm_link)}
    write_settings(path, settings)
    return True


def uninstall(settings_path: Path | str, state_dir: Path | str) -> tuple[bool, str]:
    """Put the saved statusLine back. Returns (changed, message) and never guesses when the saved file is gone."""
    settings = load_settings(settings_path)
    current = settings.get("statusLine")
    if current is None:
        return False, "no statusLine in settings.json; nothing to restore"
    if not is_ours(_dict(current).get("command")):
        return False, "not routed through kalmux; settings.json left alone"
    if not saved_present(state_dir):
        # popping the key here would leave the user with NO status line and nothing saying what was there
        return False, (f"the saved original is gone ({saved_path(state_dir)}); settings.json left alone - "
                       f"restore your statusLine by hand from {settings_path}{BACKUP_SUFFIX}")
    original = saved_statusline(state_dir)
    if original in ({}, None, ""):
        settings.pop("statusLine", None)
        message = "statusLine key removed (there was none before kalmux)"
    else:
        settings["statusLine"] = original
        message = f"restored {_dict(original).get('command') or json.dumps(original, ensure_ascii=False)}"
    write_settings(settings_path, settings)
    # never delete the saved original: move it aside, so the next install records what is configured NOW
    try:
        os.replace(saved_path(state_dir), Path(str(saved_path(state_dir)) + RESTORED_SUFFIX))
    except OSError:
        pass
    return True, message


def fresh_count(state_dir: Path | str, now: int | None = None) -> int:
    """Status files touched in the last FRESH_SECONDS — i.e. sessions whose gauge is live."""
    now = int(time.time()) if now is None else int(now)
    try:
        entries = list(status_dir(state_dir).glob("*.json"))
    except OSError:
        return 0
    fresh = 0
    for entry in entries:
        try:
            fresh += int(entry.stat().st_mtime >= now - FRESH_SECONDS)
        except OSError:
            continue
    return fresh


def status(settings_path: Path | str, state_dir: Path | str, now: int | None = None) -> dict:
    """What `kalmux statusline status` and `kalmux doctor` report. Never raises."""
    try:
        settings = load_settings(settings_path)
    except (OSError, ValueError):
        settings = {}
    command = _dict(settings.get("statusLine")).get("command")
    return {"routed": is_ours(command), "command": command if isinstance(command, str) else "",
            "saved_command": saved_command(state_dir), "saved_present": saved_present(state_dir),
            "fresh": fresh_count(state_dir, now)}
