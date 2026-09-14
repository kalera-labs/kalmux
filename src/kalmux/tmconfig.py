"""Kalmux user configuration: ~/.config/kalmux/config.toml (read-only for kalmux; created once by `kalmux setup`).

The file is the user's: kalmux never rewrites it. A bad file never raises here; wrong values fall back to the
defaults and the problems are reported under `_errors` so `kalmux config` / `kalmux doctor` can show them.
"""
from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "claude": {
        "new": "claude",
        "resume": "claude --resume {session_id}",
        "resume_mode": "type",
    },
    "tombstones": {
        "keep_days": 30,
    },
    "notify": {
        "iterm2": "auto",
        "bell": False,
    },
}

CONFIG_TEMPLATE = """\
# Kalmux configuration. Kalmux reads this file and never rewrites it; delete a key to get the default back.

[claude]
# Typed into a new session by `kalmux new --claude` (Enter is pressed for you).
new = "claude"
# Typed into the pane by `kalmux resume`. {session_id} is replaced with the Claude session id.
resume = "claude --resume {session_id}"
# "type" leaves the command in the prompt for you to press Enter; "run" presses Enter for you.
resume_mode = "type"

[tombstones]
# Trail files older than this are pruned; gone sessions older than this are not offered for resume.
keep_days = 30

[notify]
# Inside tmux, Claude Code posts no notification at all: it picks its channel from TERM_PROGRAM, which
# tmux sets to "tmux", and finds no method. Kalmux posts the iTerm2 alert (a macOS notification, with its
# sound) that the same Claude would post from a plain tab, on the same event.
#   "auto"   only while ~/.claude.json has no preferredNotifChannel of its own (then Claude posts, not us)
#   "always" post regardless      "never" stay quiet
# The hook reads these two keys without a TOML parser: keep each on its own line under [notify].
iterm2 = "auto"
# Also ring the terminal bell with the alert, like Claude's own "iterm2_with_bell".
bell = false
"""

RESUME_MODES = ("type", "run")
NOTIFY_MODES = ("auto", "always", "never")
MAX_COMMAND = 512
SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


APP_DIR = "kalmux"
LEGACY_APP_DIRS = ("kmux",)                 # config written before the rename, moved once by ensure_config()


def _config_base(env: dict) -> Path:
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", str(Path.home())), ".config")
    return Path(base).expanduser()


def config_path(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("KALMUX_CONFIG"):
        return Path(env["KALMUX_CONFIG"]).expanduser()
    return _config_base(env) / APP_DIR / "config.toml"


def legacy_config_paths(env: dict | None = None) -> list[Path]:
    """Where `config.toml` lived under the product's previous names."""
    env = os.environ if env is None else env
    base = _config_base(env)
    return [base / name / "config.toml" for name in LEGACY_APP_DIRS]


def _valid_command(value, errors: list[str], where: str, need_placeholder: str = "") -> str | None:
    if not isinstance(value, str):
        errors.append(f"{where}: expected a string")
        return None
    text = value.strip()
    if not text or len(text) > MAX_COMMAND or _CONTROL_RE.search(text):
        errors.append(f"{where}: must be one printable line of at most {MAX_COMMAND} characters")
        return None
    if need_placeholder and need_placeholder not in text:
        errors.append(f"{where}: must contain {need_placeholder}")
        return None
    return text


def _validate(raw: dict) -> tuple[dict, list[str]]:
    cfg = copy.deepcopy(DEFAULTS)
    errors: list[str] = []
    claude = raw.get("claude", {})
    if not isinstance(claude, dict):
        errors.append("[claude]: expected a table")
        claude = {}
    if "new" in claude:
        v = _valid_command(claude["new"], errors, "claude.new")
        if v is not None:
            cfg["claude"]["new"] = v
    if "resume" in claude:
        v = _valid_command(claude["resume"], errors, "claude.resume", "{session_id}")
        if v is not None:
            cfg["claude"]["resume"] = v
    if "resume_mode" in claude:
        if claude["resume_mode"] in RESUME_MODES:
            cfg["claude"]["resume_mode"] = claude["resume_mode"]
        else:
            errors.append(f"claude.resume_mode: expected one of {', '.join(RESUME_MODES)}")
    tomb = raw.get("tombstones", {})
    if not isinstance(tomb, dict):
        errors.append("[tombstones]: expected a table")
        tomb = {}
    if "keep_days" in tomb:
        v = tomb["keep_days"]
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3650:
            cfg["tombstones"]["keep_days"] = v
        else:
            errors.append("tombstones.keep_days: expected an integer between 1 and 3650")
    notify = raw.get("notify", {})
    if not isinstance(notify, dict):
        errors.append("[notify]: expected a table")
        notify = {}
    if "iterm2" in notify:
        if notify["iterm2"] in NOTIFY_MODES:
            cfg["notify"]["iterm2"] = notify["iterm2"]
        else:
            errors.append(f"notify.iterm2: expected one of {', '.join(NOTIFY_MODES)}")
    if "bell" in notify:
        if isinstance(notify["bell"], bool):
            cfg["notify"]["bell"] = notify["bell"]
        else:
            errors.append("notify.bell: expected true or false")
    return cfg, errors


def load_config(path: Path | None = None, env: dict | None = None) -> dict:
    """Defaults merged with the file. Never raises; problems land in cfg['_errors'] (a list, maybe empty)."""
    path = config_path(env) if path is None else Path(path)
    raw: dict = {}
    errors: list[str] = []
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"{path}: {exc}")
    if not isinstance(raw, dict):
        raw = {}
    cfg, more = _validate(raw)
    cfg["_errors"] = errors + more
    cfg["_path"] = str(path)
    return cfg


def ensure_config(path: Path | None = None, env: dict | None = None,
                  legacy: list[Path] | None = None) -> bool:
    """Make sure a config file exists. Returns True when this call created (or moved) one.

    A file written under a previous product name is MOVED to the new path rather than replaced by the
    template, so the user's own settings survive the rename. A legacy file is only ever moved when the new
    path is free; it is never merged with an existing one and never deleted. `legacy` defaults to the
    pre-rename locations ONLY when `path` is the configured one — an explicit path migrates nothing.
    """
    if path is None:
        path, legacy = config_path(env), legacy_config_paths(env) if legacy is None else legacy
    path = Path(path)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for old in legacy or []:
        if old.is_file() and old != path:
            os.replace(old, path)
            return True
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(CONFIG_TEMPLATE)
    os.replace(tmp, path)
    return True


def render_resume(cfg: dict, session_id: str) -> str:
    if not SESSION_ID_RE.match(session_id or ""):
        raise ValueError(f"not a Claude session id: {session_id!r}")
    return cfg["claude"]["resume"].replace("{session_id}", session_id)


def describe(cfg: dict) -> str:
    """Human-readable dump for `kalmux config`."""
    path = cfg.get("_path", "")
    lines = [f"config: {path}" + ("" if Path(path).exists() else " (missing; defaults in effect; `kalmux setup` creates it)")]
    for section in ("claude", "tombstones", "notify"):
        for key, value in cfg[section].items():
            lines.append(f"  {section}.{key} = {value!r}")
    for err in cfg.get("_errors", []):
        lines.append(f"  ! {err}")
    return "\n".join(lines)
