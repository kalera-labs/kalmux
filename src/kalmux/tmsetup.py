"""tmsetup: install and verify the machine-side pieces: Kalmux's Claude Code hook entries (tmhooks), symlinks,
iTerm2 prefs, the ~/.tmux.conf managed block, the iTerm2 AutoLaunch script that starts `kalmux ui serve`, and
the toolbelt tool.

Why the server is started BY iTerm2 (AutoLaunch.scpt) and not by launchd: `it2` authenticates through an
Apple Event to iTerm2 and the repo may live on an external volume. A launchd-spawned process has neither
TCC grant (Automation, Removable Volumes) and blocks on permission prompts; a process spawned from
iTerm2's own tree inherits both, exactly like a shell in a tab.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import tmconfig, tmhooks, tmstatusline
from .tmcore import (
    ASSETS,
    DEFAULT_REGISTRY,
    ROOT,
    SOURCE_CHECKOUT,
    VERSION,
    _decode,
    fmt_age,
    homebrew_keg,
    load_registry,
    resolve_tmux,
    stable_path,
)

WRAPPER = ASSETS / "kalmux-hook"
TOOLBELT_SCRIPT = ASSETS / "it2_toolbelt.py"
DEFAULT_HOOK_LINK = Path.home() / ".config/iterm2/cc-status"
DEFAULT_TM_LINK = Path.home() / ".local/bin/kalmux"
# kept so anything that already types `kmux` or `tm` keeps working (both are plain aliases of bin/kalmux)
LEGACY_KMUX_LINK = Path.home() / ".local/bin/kmux"
LEGACY_TM_LINK = Path.home() / ".local/bin/tm"
LEGACY_LINKS = (LEGACY_KMUX_LINK, LEGACY_TM_LINK)
DEFAULT_SETTINGS = Path.home() / ".claude/settings.json"
DEFAULT_TMUX_CONF = Path.home() / ".tmux.conf"


def state_dir(env: dict | None = None) -> Path:
    """kalmux's own state: `$KALMUX_STATE_DIR` (the hook honours it too) or ~/.local/state/kalmux."""
    env = os.environ if env is None else env
    override = env.get("KALMUX_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path(env.get("HOME", str(Path.home()))).expanduser() / ".local/state/kalmux"


STATE_DIR = state_dir()
STATUS_DIR = STATE_DIR / "status"                     # one <session_id>.json per Claude session (statusLine tap)
TRACE_DIR = STATE_DIR / "trace"                       # one <session_id>.jsonl trail per Claude session (hook)
SESSIONS_FILE = STATE_DIR / "sessions.json"           # the live tmux sessions, rewritten by the ui server
PREVIOUS_SESSIONS_FILE = STATE_DIR / "sessions.previous.json"   # the list of the boot before this one
# state written before a rename: migrated whole by `kalmux setup`, and still searched for a stale pidfile
LEGACY_STATE_DIRS = (Path.home() / ".local/state/kmux", Path.home() / ".local/state/tm")
ITERM_SCRIPTS = Path.home() / "Library/Application Support/iTerm2/Scripts"
AUTOLAUNCH = ITERM_SCRIPTS / "AutoLaunch.scpt"          # iTerm2 runs this AppleScript at startup
AUTOLAUNCH_STAMP = STATE_DIR / "autolaunch.sha256"       # so we only ever overwrite our own script
AUTOLAUNCH_MARKER = " ui start >/dev/null 2>&1 &"         # recognises our one-liner when the stamp is lost
UI_PORT = 47321
UI_URL = f"http://127.0.0.1:{UI_PORT}/"
UI_LOG = STATE_DIR / "ui.log"
BACKUP_SUFFIX = ".bak-kalmux"                           # what a file we had to move out of the way becomes
TOOLBELT_ID = "vn.kal.kalmux.toolbelt"
TOOLBELT_NAME = "Kalmux"
LEGACY_TOOLBELTS = (("vn.kal.kmux.toolbelt", "Kmux"), ("vn.kal.tm.toolbelt", "tmux manager"))
ITERM_DOMAIN = "com.googlecode.iterm2"
STATE_DIR_STEP = f"state directory (migrates {LEGACY_STATE_DIRS[0]})"
STATUSLINE_STEP = "statusLine wrapper (context gauge)"
MANAGED_BEGIN = "# >>> kalmux (managed block; edit via `kalmux setup`) >>>"
MANAGED_END = "# <<< kalmux <<<"
# every marker pair this block was ever written with; the backreference keeps begin and end paired up
LEGACY_MANAGED_RE = re.compile(r"# >>> (kmux|tmux-manager)[^\n]*\n.*?# <<< \1 <<<\n?", re.DOTALL)
HOOKS_STEP = "Claude Code hooks"
HOOKS_CHECK = "Claude Code hooks call Kalmux"
CLI_STEP = "kalmux symlink"
TMUX_CONF_STEP = "tmux.conf block"
EXIT_EMPTY_CHECK = "tmux.conf exit-empty off (a killed last session stays killed)"
# what `kalmux doctor --fix` may run on its own: idempotent, and nothing that writes iTerm2's preferences,
# its AutoLaunch folder or its toolbelt, or starts a server (those print the command to run instead)
SAFE_FIX_STEPS = frozenset({HOOKS_STEP, CLI_STEP, TMUX_CONF_STEP, STATUSLINE_STEP,
                            *(f"{link.name} alias symlink (skipped if the path is not ours)" for link in LEGACY_LINKS)})


# ----------------------------------------------------------------------------- tmux.conf block
def managed_block(tm_path: Path = DEFAULT_TM_LINK) -> str:
    return "\n".join([
        MANAGED_BEGIN,
        "# Let iTerm2-specific escape sequences (status, notifications, tab color) reach the terminal.",
        "set -g allow-passthrough on",
        "# Keep the server up with no session left, so Kalmux can tell \"the last session was killed\" from \"tmux died\".",
        "set -s exit-empty off",
        "# Show Claude Code state (written by kalmux-hook) in the status line for normal / remote attaches.",
        "set -g status-interval 5",
        "set -g window-status-format ' #I:#W#{?#{@cc_state}, [#{@cc_state}],} '",
        "set -g window-status-current-format ' #I:#W#{?#{@cc_state}, [#{@cc_state}],} '",
        "set -g status-right '#{?#{@cc_detail},#{=40:@cc_detail} | ,}#S | %H:%M'",
        "set -g status-right-length 80",
        "# A tab that just attached starts blank: replay the last known status + identity color into it.",
        f"set-hook -g client-attached 'run-shell -b \"sleep 2; {tm_path} reapply >/dev/null 2>&1 || true\"'",
        MANAGED_END,
        "",
    ])


MANAGED_BLOCK = managed_block()


def upsert_managed_block(text: str, block: str) -> str:
    text = LEGACY_MANAGED_RE.sub("", text)               # the same block written under any pre-kalmux name
    pattern = re.compile(r"# >>> kalmux[^\n]*\n.*?" + re.escape(MANAGED_END) + r"\n?", re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + block


# ----------------------------------------------------------------------------- small helpers
def _run(cmd: list[str], timeout: float = 30, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return p.returncode, (_decode(p.stdout) + _decode(p.stderr)).strip()


def defaults_read(key: str) -> str:
    return _run(["defaults", "read", ITERM_DOMAIN, key], timeout=10)[1] if sys.platform == "darwin" else ""


def cli_path() -> Path:
    """The file the `kalmux` command is: a Python script something else can run as `python3 <path> ...`.

    The command we were actually invoked as comes first, because it is the path that stays valid. A
    Homebrew keg, for instance, lives under a versioned Cellar directory that `brew upgrade` replaces,
    while the name on PATH survives; baking the versioned one into ~/.tmux.conf and the AutoLaunch script
    would leave both dangling after the next upgrade.
    """
    argv0 = Path(sys.argv[0]).absolute() if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name in tmstatusline.WRAPPER_NAMES and argv0.is_file():
        return stable_path(argv0)                    # kept unresolved on purpose: see the docstring
    if SOURCE_CHECKOUT:
        return SOURCE_CHECKOUT / "bin" / "kalmux"
    # the script installed next to THIS interpreter, before anything a stale PATH may still point at.
    # sys.executable is NOT resolved first: in a venv that would follow the python symlink out of the venv.
    here = Path(sys.executable).parent / "kalmux"
    if here.is_file():
        return here
    found = shutil.which("kalmux")
    return stable_path(Path(found).resolve()) if found else here


CLI = cli_path()


def symlink(target: Path, link: Path) -> str:
    """Point `link` at `target`. A REAL file already there is the user's: move it aside, never delete it."""
    link.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        backup = link.with_name(link.name + BACKUP_SUFFIX)
        os.replace(link, backup)
        note = f"moved your own {link} to {backup}"
    link.symlink_to(target)
    return note


def link_is_ours(link: Path) -> bool:
    """A symlink of ours: it points into this checkout (true for the dangling pre-rename bin/tm link too),
    straight at the installed `kalmux` command, or into any version of the Homebrew kalmux keg (a link baked
    by an older version names its versioned directory, which dangles once `brew cleanup` removes it)."""
    if not link.is_symlink():
        return False
    target = os.readlink(link)
    return target.startswith(str(ROOT)) or target == str(CLI) or _in_kalmux_keg(link, target)


def _in_kalmux_keg(link: Path, target: str) -> bool:
    candidates = [Path(target)]
    with contextlib.suppress(OSError):
        candidates.append(link.resolve())
    return any((keg := homebrew_keg(c)) is not None and keg[1] == "kalmux" for c in candidates)


def is_the_command(link: Path) -> bool:
    """True when `link` already gets you the kalmux we are running: the installed console script itself, or
    a symlink of ours that resolves to the same file. Linking such a path to itself would destroy it."""
    return (link.exists() or link.is_symlink()) and _same_path(CLI, link)


def symlink_cli(link: Path = DEFAULT_TM_LINK) -> str:
    """Put `kalmux` on PATH. An installed package already did: when the command IS that path, leave it
    alone instead of moving the real executable aside and replacing it with a symlink to itself."""
    if is_the_command(link):
        return f"{link} already is the kalmux command; left alone"
    return symlink(CLI, link)


def alias_symlink(target: Path, link: Path) -> bool:
    """Create a legacy-name alias (`kmux`, `tm`) only when the path is free or already ours: an unrelated
    program of the same name is never clobbered."""
    if _same_path(target, link):
        return True                                  # already gets you this kalmux; relinking it to itself would not
    if (link.exists() or link.is_symlink()) and not link_is_ours(link):
        return False
    symlink(target, link)
    return True


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:                                      # a broken symlink cannot be resolved; it is not `new`
        return False


def migrate_state_dir(new: Path = STATE_DIR, legacy: tuple[Path, ...] = LEGACY_STATE_DIRS) -> str:
    """Move the state written under a previous name (~/.local/state/kmux) to ~/.local/state/kalmux.

    Files move one by one rather than by renaming the directory, because any `kalmux` command run before
    setup (`kalmux ui stop`, the statusLine tap) creates the new directory as a side effect, and a
    directory rename would then refuse and silently leave the saved status line behind under the old name.
    A file that already exists at the destination is never overwritten and never deleted: it stays where it
    is for the user to look at. Returns a note for `kalmux setup`, "" when nothing moved.
    """
    moved: list[str] = []
    for old in legacy:
        if not old.is_dir() or old.is_symlink() or _same_path(old, new):
            continue
        new.mkdir(parents=True, exist_ok=True)
        for src in sorted(old.iterdir()):
            dst = new / src.name
            if src.is_dir() and dst.is_dir():            # status/ and trace/: merge file by file
                for item in sorted(src.iterdir()):
                    if not (dst / item.name).exists():
                        os.rename(item, dst / item.name)
            elif not dst.exists():
                os.rename(src, dst)                      # an OSError here is real: let setup show it
        with contextlib.suppress(OSError):               # only succeeds once everything has moved out
            _rmtree_empty(old)
        moved.append(str(old))
    return f"moved {', '.join(moved)} to {new}" if moved else ""


def _rmtree_empty(path: Path) -> None:
    """Remove a directory tree, but only the parts that are empty. Anything left behind is the user's."""
    for child in sorted(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            _rmtree_empty(child)
    path.rmdir()


def write_iterm_prefs() -> None:
    for key, kind, val in (("OpenTmuxWindowsIn", "-int", "2"), ("AutoHideTmuxClientSession", "-bool", "true")):
        _run(["defaults", "write", ITERM_DOMAIN, key, kind, val])


def write_tmux_conf(conf: Path = DEFAULT_TMUX_CONF, tm_path: Path = DEFAULT_TM_LINK) -> None:
    text = conf.read_text() if conf.exists() else ""
    conf.write_text(upsert_managed_block(text, managed_block(tm_path)))
    rc, out = _run([resolve_tmux() or "tmux", "source-file", str(conf)])
    if rc != 0 and "no server running" not in out:      # no server yet is fine: the conf loads on first start
        raise OSError(f"tmux refused {conf}: {out or 'no output'}")


# ----------------------------------------------------------------------------- ui server process
def start_server(tm_path: Path = DEFAULT_TM_LINK, port: int = UI_PORT, wait: float = 5.0) -> bool:
    """Start `tm ui serve` detached from this process (it must be a descendant of iTerm2, see module doc)."""
    # imported here, not at module level: `kalmux statusline` imports tmsetup for state_dir() on every status-line
    # refresh, and tmserver drags in http.client/ssl/email — ~17 ms this path must not pay for.
    from .tmserver import health
    if health(port):
        return True
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(UI_LOG, "ab") as log:
        # sys.executable, not tm_path's own shebang: whoever calls start_server() already resolved a
        # good python3 to get here, and the child should inherit that choice rather than re-resolve one.
        subprocess.Popen([sys.executable, str(tm_path), "ui", "serve", "--port", str(port)], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if health(port):
            return True
        time.sleep(0.1)
    return False


# ----------------------------------------------------------------------------- iTerm2 AutoLaunch
def autolaunch_source(tm_path: Path = DEFAULT_TM_LINK, python: str = "", extra_path: str | None = None) -> str:
    """Build the `do shell script` AppleScript source that starts the ui server.

    AppleScript's `do shell script` runs under a MINIMAL PATH (/etc/paths only — no ~/.zshrc, no
    ~/.local/bin, no Homebrew) because iTerm2 itself is launched with that environment at login /
    cold app-launch, which is exactly when AutoLaunch fires. `tm`'s shebang (`#!/usr/bin/env python3`)
    then resolves to whatever `python3` is first on THAT PATH — on this machine that turned out to be
    the ancient Python 3.9 bundled with Xcode's command line tools, which crashes on `zip(strict=True)`
    (a 3.10+ feature). Fix: bake the interpreter that ran `tm setup`/`tm ui install` (a real PATH,
    verified >=3.10) as an ABSOLUTE path into the script, bypassing PATH resolution entirely.
    Caught for real on 2026-09-13 when a macOS crash forced a reboot and AutoLaunch cold-started the
    server under Python 3.9.
    """
    python = python or sys.executable
    tm = str(tm_path)
    path = _autolaunch_path(python) if extra_path is None else extra_path
    for value in (python, tm, path):
        if any(c in value for c in "\"\\'"):
            raise ValueError(f"path {value!r} cannot be quoted for AppleScript")
    # PATH prefix: the same minimal PATH hides Homebrew's tmux from the server (empty toolbelt after every reboot)
    return f"do shell script \"PATH='{path}':$PATH '{python}' '{tm}'{AUTOLAUNCH_MARKER}\""


def _autolaunch_path(python: str) -> str:
    """Directories the server must see even under the login PATH: tmux's, the interpreter's, Homebrew's."""
    dirs = [os.path.dirname(p) for p in (resolve_tmux(), python) if p] + ["/opt/homebrew/bin", "/usr/local/bin"]
    return ":".join(dict.fromkeys(d for d in dirs if d))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def autolaunch_targets(script: Path = AUTOLAUNCH) -> list[str]:
    """The absolute paths baked into our AutoLaunch one-liner (interpreter, tm), read back from the script."""
    rc, out = _run(["osadecompile", str(script)], timeout=30)
    if rc != 0:
        return []
    return re.findall(r"(?<!PATH=)'(/[^']+)'", out)[:2]      # skip the PATH='…' prefix, keep interpreter + tm


def autolaunch_state(script: Path = AUTOLAUNCH, stamp: Path = AUTOLAUNCH_STAMP) -> str:
    """'missing' | 'ours' | 'foreign' — a user's own AutoLaunch.scpt is never overwritten."""
    if not script.exists():
        return "missing"
    try:
        if stamp.read_text().strip() == _sha256(script):
            return "ours"
    except OSError:
        pass
    # stamp lost (state dir wiped) or stale: our script is a single recognisable line, so ask the source
    rc, out = _run(["osadecompile", str(script)], timeout=30)
    return "ours" if rc == 0 and AUTOLAUNCH_MARKER in out else "foreign"


def install_autolaunch(tm_path: Path = DEFAULT_TM_LINK, script: Path = AUTOLAUNCH, stamp: Path = AUTOLAUNCH_STAMP,
                       python: str = "") -> None:
    python = str(stable_path(Path(python or sys.executable)))     # a versioned python keg dies on `brew upgrade`
    if sys.version_info < (3, 11):  # noqa: UP036 - a git clone can still be run by an old `python3` (see the docstring)
        raise OSError(f"refusing to bake {python} (Python {sys.version.split()[0]}) into AutoLaunch: "
                      "kalmux needs >=3.11; run `kalmux setup` from a shell whose `python3` is modern (e.g. Homebrew's)")
    source = autolaunch_source(tm_path, python)
    if autolaunch_state(script, stamp) == "foreign":
        raise OSError(f"{script} already exists and is not ours; add this line to it yourself: {source}")
    script.parent.mkdir(parents=True, exist_ok=True)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run(["osacompile", "-o", str(script), "-e", source], timeout=60)
    if rc != 0 or not script.exists():
        raise OSError(f"osacompile failed: {out or 'no output'}")
    stamp.write_text(_sha256(script))


def uninstall_autolaunch(script: Path = AUTOLAUNCH, stamp: Path = AUTOLAUNCH_STAMP) -> bool:
    if autolaunch_state(script, stamp) != "ours":
        return False
    script.unlink()
    stamp.unlink(missing_ok=True)
    return True


# ----------------------------------------------------------------------------- toolbelt tool
def request_cookie() -> str:
    """An iTerm2 API cookie: lets the registration script connect without a permission dialog."""
    rc, out = _run(["osascript", "-e", 'tell application "iTerm2" to request cookie'], timeout=15)
    return out.strip() if rc == 0 else ""


def export_prefs(domain: str = ITERM_DOMAIN) -> dict:
    """The whole preference domain as a dict (goes through cfprefsd, unlike reading the plist file)."""
    try:
        p = subprocess.run(["defaults", "export", domain, "-"], capture_output=True, check=False, timeout=10)
        return plistlib.loads(p.stdout) if p.returncode == 0 and p.stdout else {}
    except (OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException, ValueError):
        return {}


def prune_legacy_toolbelt(legacy: tuple[tuple[str, str], ...] = LEGACY_TOOLBELTS) -> bool:
    """Drop every pre-rename tool so the toolbelt does not keep an empty box named after one.

    iTerm2 keeps dynamic tools in two prefs: NoSyncDynamicTools (id -> {name, URL}) and ToolbeltTools (the
    names currently shown). `defaults delete` cannot remove a key INSIDE a dict, so the dict is rewritten
    whole (`defaults write <domain> <key> <plist-xml>`), still through cfprefsd. iTerm2 rewrites both prefs
    when it quits, so a running instance may resurrect an entry until it is restarted.
    """
    if sys.platform != "darwin":
        return False
    old_ids = {tool_id for tool_id, _ in legacy}
    old_names = {name for _, name in legacy}
    prefs = export_prefs()
    changed = False
    tools = prefs.get("NoSyncDynamicTools")
    if isinstance(tools, dict) and old_ids & set(tools):
        kept = {k: v for k, v in tools.items() if k not in old_ids}
        xml = plistlib.dumps(kept, fmt=plistlib.FMT_XML).decode("utf-8")
        changed |= _run(["defaults", "write", ITERM_DOMAIN, "NoSyncDynamicTools", xml], timeout=10)[0] == 0
    shown = prefs.get("ToolbeltTools")
    if isinstance(shown, list) and old_names & {str(name) for name in shown}:
        kept_names = [str(name) for name in shown if str(name) not in old_names]
        changed |= _run(["defaults", "write", ITERM_DOMAIN, "ToolbeltTools", "-array", *kept_names], timeout=10)[0] == 0
    return changed


def register_toolbelt(url: str = UI_URL, show: bool = True) -> tuple[bool, str]:
    uv = shutil.which("uv")
    if not uv:
        return False, "uv not found (brew install uv); it fetches the `iterm2` Python package on demand"
    cookie = request_cookie()
    if not cookie:
        return False, "iTerm2 gave no API cookie (is iTerm2 running, with Settings > General > Magic > Enable Python API?)"
    cmd = [uv, "run", "--no-project", "--with", "iterm2", "python", str(TOOLBELT_SCRIPT), "--url", url]
    if show:
        cmd.append("--show")
    rc, out = _run(cmd, timeout=240, env={**os.environ, "ITERM2_COOKIE": cookie})
    if rc == 0:
        prune_legacy_toolbelt()
    return rc == 0, out


def toolbelt_registered(reader=defaults_read) -> bool:
    return TOOLBELT_ID in reader("NoSyncDynamicTools")


# ----------------------------------------------------------------------------- setup / doctor
def alias_step(link: Path) -> str:
    return f"{link.name} alias symlink (skipped if the path is not ours)"


def setup_steps(ui: bool = True, tm_link: Path = DEFAULT_TM_LINK, hook_link: Path = DEFAULT_HOOK_LINK,
                statusline: bool = True, settings_path: Path = DEFAULT_SETTINGS) -> list[tuple[str, callable]]:
    steps = [
        # first: everything below (the config file, the saved status line, the pidfile) lives in the state dir
        (STATE_DIR_STEP, migrate_state_dir),
        ("config file", tmconfig.ensure_config),
        (HOOKS_STEP, lambda: tmhooks.wire_claude_hooks(settings_path, WRAPPER, STATE_DIR, hook_link=hook_link)),
        (CLI_STEP, lambda: symlink_cli(tm_link)),
    ]
    steps += [(alias_step(link), lambda link=link: alias_symlink(CLI, link)) for link in LEGACY_LINKS]
    steps += [
        ("iTerm2 prefs", write_iterm_prefs),
        (TMUX_CONF_STEP, lambda: write_tmux_conf(DEFAULT_TMUX_CONF, tm_link)),
    ]
    if ui and sys.platform == "darwin":
        steps += [
            ("iTerm2 AutoLaunch script (starts the ui server with iTerm2)", lambda: install_autolaunch(tm_link)),
            (f"ui server on {UI_URL}", lambda: _start_or_raise(tm_link)),
        ]
    if statusline:
        steps.append((STATUSLINE_STEP, lambda: _install_statusline(settings_path, tm_link)))
    if ui and sys.platform == "darwin":
        steps.append((f"iTerm2 toolbelt tool {TOOLBELT_NAME!r}", _register_or_raise))
    return steps


def _install_statusline(settings_path: Path, tm_link: Path) -> None:
    """Route statusLine through kalmux, but never at a link that cannot run: that renders a BLANK status line."""
    if not (tm_link.is_file() and os.access(tm_link, os.X_OK)):
        raise OSError(f"{tm_link} is not an executable file: the `kalmux symlink` step has to succeed first, "
                      "otherwise Claude Code's status line would run a missing command and render blank")
    tmstatusline.install(settings_path, tm_link, STATE_DIR)


def _start_or_raise(tm_link: Path) -> None:
    if not start_server(tm_link):
        raise OSError(f"server did not answer on {UI_URL}; see {UI_LOG}")


def _register_or_raise() -> None:
    ok, out = register_toolbelt(UI_URL, show=True)
    if not ok:
        raise OSError(out or "registration failed")


def _run_steps(steps: list[tuple[str, callable]], printer) -> int:
    failures = 0
    for name, fn in steps:
        try:
            note = fn()
            printer(f"✓ {name}" + (f" ({note})" if isinstance(note, str) and note else ""))
        except (OSError, ValueError) as exc:
            failures += 1
            printer(f"✗ {name}: {exc}")
    return failures


def run_setup(dry_run: bool = False, ui: bool = True, statusline: bool = True, printer=print) -> int:
    steps = setup_steps(ui=ui, statusline=statusline)
    if dry_run:
        for name, _ in steps:
            printer(f"would: {name}")
        return 0
    return _run_steps(steps, printer)


def fix_plan(checks: list[dict]) -> list[str]:
    """The setup steps `doctor --fix` would run for the red checks: safe, idempotent ones only, in setup order."""
    wanted = {c["fix"] for c in checks if not c["ok"] and c.get("fix") in SAFE_FIX_STEPS}
    order = [name for name, _ in setup_steps(ui=False)]
    return [name for name in order if name in wanted]


def run_fix(checks: list[dict], ui: bool = True, statusline: bool = True, printer=print) -> int:
    """`kalmux doctor --fix`: say what will run, run only that, then list what needs a human. Returns failures.

    The iTerm2 preferences, the AutoLaunch script, the ui server and the toolbelt are never touched here: each
    red check of theirs carries the exact command to run by hand instead."""
    plan = fix_plan(checks)
    printer(("kalmux doctor --fix will run: " + ", ".join(plan)) if plan else "nothing kalmux doctor --fix can repair on its own")
    steps = [(name, fn) for name, fn in setup_steps(ui=ui, statusline=statusline) if name in plan]
    failures = _run_steps(steps, printer)
    for c in checks:
        if not c["ok"] and c.get("fix") not in SAFE_FIX_STEPS:
            printer(f"by hand: {c['name']}: {c['remedy']}")
    return failures


def check_session_snapshot(sessions_file: Path = SESSIONS_FILE, tmux=None, now=None, sleep=time.sleep) -> tuple[bool, str]:
    """(ok, info) for the doctor's session-snapshot check: is the saved list this boot's live one?

    Only the UI server snapshots right after an action; `kalmux new` / `kill` / `rename` (and a plain
    `tmux new-session`) talk to tmux directly, so the file trails them by up to one keeper round. A
    disagreement is therefore looked at twice before it is reported, or `kalmux new x && kalmux doctor`
    would exit 1 on a perfectly healthy machine."""
    # lazy, like the tmserver import above: `kalmux statusline` imports this module on every status-line
    # refresh and must not pay for dataclasses/threading it never uses.
    from .tmcore import Tmux
    from .tmrestore import DRIFT_NOTE, SNAPSHOT_EVERY, boot_time, live_state, snapshot_health
    server = Tmux() if tmux is None else tmux
    clock, boot = now or time.time, boot_time()

    def verdict() -> tuple[bool, str]:
        return snapshot_health(sessions_file, boot, live_state(server), int(clock()))

    ok, info = verdict()
    if ok or DRIFT_NOTE not in info:
        return ok, info
    sleep(SNAPSHOT_EVERY + 1)
    return verdict()


class Checks(list):
    """The doctor's report: one dict per line, each red one with a one-line remedy (and a setup step to fix it)."""

    def add(self, name: str, ok: bool, info: str = "", remedy: str = "", fix: str = "") -> None:
        self.append({"name": name, "ok": bool(ok), "info": info, "remedy": remedy if not ok else "", "fix": fix})


def _link_info(link: Path) -> str:
    if link.is_symlink():
        return f"{link} -> {os.readlink(link)}"
    return f"{link} is not a symlink (left alone)" if link.exists() else f"{link} missing"


def _check_paths(checks: Checks, config: dict, cli_link: Path, alias_links: tuple[Path, ...]) -> None:
    errors = config.get("_errors") or []
    checks.add("kalmux config file", not errors, "; ".join(errors) or str(tmconfig.config_path()),
               f"fix the listed keys in {tmconfig.config_path()} (`kalmux config` shows the result)")
    # the rename moved bin/kmux to bin/kalmux: a pre-rename ~/.local/bin/kmux symlink dangles until setup runs
    checks.add("kalmux on PATH", is_the_command(cli_link), str(cli_link) if not cli_link.is_symlink() else _link_info(cli_link),
               "kalmux doctor --fix (links the kalmux command there)", CLI_STEP)
    for link in alias_links:
        checks.add(f"{link.name} alias", is_the_command(link), _link_info(link),
                   f"kalmux doctor --fix; if {link} is another program, it stays: remove it yourself to get the alias",
                   alias_step(link))


def _check_hooks(checks: Checks, settings_path: Path, hook_link: Path, wrapper: Path, state_dir: Path,
                 registry: dict, version: tuple[int, ...] | None) -> None:
    settings: dict = {}
    try:
        settings = tmstatusline.load_settings(settings_path)
        problems = tmhooks.wiring_problems(settings, wrapper, tmhooks.status_mode(settings, version))
    except (OSError, ValueError) as exc:
        problems = [tmhooks.read_problem(settings_path, exc)]
    checks.add(HOOKS_CHECK, not problems, "; ".join(problems) or f"{len(tmhooks.HOOK_EVENTS)} events -> {wrapper}",
               "kalmux doctor --fix (adds Kalmux's own entries; iTerm2's stay as they are)", HOOKS_STEP)
    _check_iterm_native(checks, settings, version)
    checks.add("iTerm2 owns ~/.config/iterm2/cc-status", not tmhooks.points_at_kalmux(hook_link), _link_info(hook_link),
               "kalmux doctor --fix (gives the link back to iTerm2)", HOOKS_STEP)
    checks.add("wrapper executable", os.access(wrapper, os.X_OK), str(wrapper),
               f"reinstall Kalmux, or chmod +x {wrapper}")
    seen, quiet = tmhooks.silence(state_dir, registry)
    age = tmhooks.heartbeat_age(state_dir)
    info = "no hook event yet" if seen is None else f"last event {fmt_age(age)} ago"
    if quiet:
        info += "; not reporting: " + ", ".join(tmhooks.unreported(state_dir, registry))
    if problems:        # nothing can arrive through entries that are not there: fixing them comes first
        checks.add("hooks reach Kalmux", not quiet, info, "kalmux doctor --fix (Kalmux's entries are missing, see above)", HOOKS_STEP)
    else:
        checks.add("hooks reach Kalmux", not quiet, info,
                   "restart a Claude session listed here if it still does not report (it may predate the wiring); "
                   "if none report, kalmux doctor --fix")


def _check_iterm_native(checks: Checks, settings: dict, version: tuple[int, ...] | None) -> None:
    """iTerm2's own hooks run next to Kalmux's; before 3.7.5 they report every tmux pane to one hidden tab."""
    try:
        hooked = tmhooks.iterm2_hooked(settings)
    except ValueError:
        return
    if version is None or not hooked:
        return
    shown = ".".join(map(str, version))
    native = version >= tmhooks.ITERM2_NATIVE_TMUX
    checks.add("iTerm2 3.7.5+ (reports tmux panes itself)", native,
               f"iTerm2 {shown}" + ("" if native else ": its own Claude Code hooks report every tmux pane to one hidden tab"),
               "upgrade iTerm2 to 3.7.5 or newer (Settings > General > Check for Updates)")


def _check_tmux(checks: Checks, tmux_conf: Path) -> None:
    checks.add("jq installed", bool(shutil.which("jq")), shutil.which("jq") or "", "brew install jq")
    checks.add("tmux installed", bool(resolve_tmux()), resolve_tmux() or "", "brew install tmux")
    try:
        conf = tmux_conf.read_text()
    except OSError:
        conf = ""
    remedy = f"kalmux doctor --fix (rewrites the managed block in {tmux_conf} and reloads it)"
    checks.add("tmux.conf allow-passthrough", "allow-passthrough on" in conf, str(tmux_conf), remedy, TMUX_CONF_STEP)
    # without it, killing the last session stops the server, and that is indistinguishable from a crash:
    # the restore would bring the session the user just killed straight back
    checks.add(EXIT_EMPTY_CHECK, "exit-empty off" in conf, str(tmux_conf), remedy, TMUX_CONF_STEP)
    checks.add("tmux.conf client-attached hook (status replay)", "client-attached" in conf, str(tmux_conf), remedy, TMUX_CONF_STEP)


def _check_statusline(checks: Checks, line: dict, settings_path: Path, state_dir: Path) -> None:
    name = "statusLine routed through kalmux"
    if not line["routed"]:
        # no fix step: `setup --no-statusline` is recorded nowhere, and --fix must not route what the user kept
        checks.add(name, False, "not routed; kalmux statusline install routes it (skip this check with --no-statusline)",
                   "kalmux statusline install")
    elif not line.get("saved_present", bool(line.get("saved_command"))):
        # routed with no saved original: uninstall cannot put anything back and the tap prints our default
        checks.add(name, False,
                   f"routed, but the saved original is gone ({tmstatusline.saved_path(state_dir)}); recover it from "
                   f"{settings_path}{tmstatusline.BACKUP_SUFFIX}, then run `kalmux statusline install` again",
                   f"copy your statusLine back from {settings_path}{tmstatusline.BACKUP_SUFFIX}, then kalmux statusline install")
    else:
        checks.add(name, True, f"original saved: {line['saved_command'] or 'none'}")
    python = line.get("interpreter") if line["routed"] else ""
    if python:
        # the command carries an absolute interpreter so it never depends on PATH; a removed one blanks the line
        there = tmstatusline.usable_python(python)
        checks.add("statusLine interpreter exists", there,
                   python if there else f"{python} is gone or older than 3.11: run `kalmux setup` to bake the current one",
                   "kalmux doctor --fix (bakes the interpreter running it into the statusLine command)", STATUSLINE_STEP)
    # informational: a machine simply nobody has used in the last 10 minutes is healthy
    checks.add("status files fresh", True,
               f"{line['fresh']} file(s) newer than {tmstatusline.FRESH_SECONDS // 60} min in {state_dir / 'status'}"
               " (0 is normal until a Claude session refreshes its status line)")


def _check_iterm(checks: Checks, reader, ui: bool, autolaunch, autolaunch_paths) -> None:
    prefs = "kalmux setup (writes this iTerm2 preference), or Settings > General > tmux"
    checks.add("iTerm2 OpenTmuxWindowsIn=2 (tabs in attaching window)", reader("OpenTmuxWindowsIn") == "2",
               "Settings > General > tmux", prefs)
    checks.add("iTerm2 AutoHideTmuxClientSession=1", reader("AutoHideTmuxClientSession") == "1",
               "Settings > General > tmux", prefs)
    if not ui:
        return
    checks.add("iTerm2 toolbelt tool registered", toolbelt_registered(reader),
               f"{TOOLBELT_ID} in NoSyncDynamicTools (kalmux ui show)", "kalmux ui show (registers the toolbelt tool)")
    state = autolaunch() if autolaunch else autolaunch_state()
    checks.add("iTerm2 AutoLaunch script starts the ui server", state == "ours", f"{AUTOLAUNCH}: {state} (kalmux ui install)",
               "kalmux ui install" if state != "foreign" else f"add the line `kalmux ui install` prints to your own {AUTOLAUNCH}")
    if state == "ours":
        targets = autolaunch_paths() if autolaunch_paths else autolaunch_targets()
        missing = [t for t in targets if not os.path.exists(t)]
        checks.add("AutoLaunch script targets exist", bool(targets) and not missing,
                   ("missing: " + ", ".join(missing) + " (kalmux ui install)") if missing else ", ".join(targets) or "could not read the script",
                   "kalmux ui install (bakes the current interpreter and command)")


def _check_ui(checks: Checks, h: dict | None, state_dir: Path, snapshot_status) -> None:
    checks.add("ui server reachable", bool(h), f"{UI_URL} pid {h.get('pid')}" if h else f"{UI_URL} not answering (kalmux ui start)",
               "kalmux ui start (from a shell inside iTerm2)")
    if not h:
        return
    checks.add("ui server can run tmux", bool(h.get("tmux")),
               h.get("tmux") or "the server's PATH has no tmux: `kalmux ui restart` from an iTerm2 shell, then `kalmux ui install`",
               "kalmux ui restart from an iTerm2 shell, then kalmux ui install")
    running = h.get("version") or "unknown"
    checks.add("ui server runs this code", running == VERSION,
               f"server {running}, checkout {VERSION}" + ("" if running == VERSION else ", kalmux ui restart (from an iTerm2 shell)"),
               "kalmux ui restart (from an iTerm2 shell)")
    # only while the server answers: it is the process that writes the file every 5 s; state_dir, not the
    # module default: a doctor pointed at another state dir reads that one
    status = snapshot_status or (lambda: check_session_snapshot(state_dir / "sessions.json"))
    checks.add("session snapshot (restore after a crash or reboot)", *status(),
               remedy="wait 5 s and run kalmux doctor again; if it stays red, kalmux ui restart (from an iTerm2 shell)")


def doctor_checks(settings_path: Path = DEFAULT_SETTINGS, hook_link: Path = DEFAULT_HOOK_LINK, wrapper: Path = WRAPPER,
                  tmux_conf: Path = DEFAULT_TMUX_CONF, defaults_reader=None, ui_health=None, autolaunch=None,
                  ui: bool = True, cli_link: Path = DEFAULT_TM_LINK, alias_links: tuple[Path, ...] = LEGACY_LINKS,
                  autolaunch_paths=None, state_dir: Path = STATE_DIR, statusline_status=None,
                  statusline: bool = True, config_loader=None, snapshot_status=None, registry_loader=None,
                  iterm_version=None) -> list[dict]:
    """ui=False (a machine set up with `kalmux setup --no-ui`) skips the toolbelt / AutoLaunch / server checks;
    statusline=False (`--no-statusline`) skips the two status-line checks the same way. Every red check
    carries a one-line `remedy`; the ones `kalmux doctor --fix` can repair name their setup step in `fix`."""
    checks = Checks()
    _check_paths(checks, (config_loader() if config_loader else tmconfig.load_config()) or {}, cli_link, alias_links)
    registry = registry_loader() if registry_loader else load_registry(DEFAULT_REGISTRY)
    version = iterm_version() if iterm_version else tmhooks.iterm2_version()
    _check_hooks(checks, settings_path, hook_link, wrapper, state_dir, registry, version)
    _check_tmux(checks, tmux_conf)
    if statusline:
        line = statusline_status() if statusline_status else tmstatusline.status(settings_path, state_dir)
        _check_statusline(checks, line, settings_path, state_dir)
    if sys.platform == "darwin":
        _check_iterm(checks, defaults_reader or defaults_read, ui, autolaunch, autolaunch_paths)
        if ui and ui_health is not None:
            _check_ui(checks, ui_health(), state_dir, snapshot_status)
    return list(checks)
