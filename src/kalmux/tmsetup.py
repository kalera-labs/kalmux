"""tmsetup — install and verify the machine-side pieces: symlinks, iTerm2 prefs, the ~/.tmux.conf
managed block, the iTerm2 AutoLaunch script that starts `kalmux ui serve`, and the toolbelt tool.

Why the server is started BY iTerm2 (AutoLaunch.scpt) and not by launchd: `it2` authenticates through an
Apple Event to iTerm2 and the repo may live on an external volume. A launchd-spawned process has neither
TCC grant (Automation, Removable Volumes) and blocks on permission prompts; a process spawned from
iTerm2's own tree inherits both, exactly like a shell in a tab.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import tmconfig, tmstatusline
from .tmcore import ASSETS, ROOT, SOURCE_CHECKOUT, VERSION, _decode, resolve_tmux

WRAPPER = ASSETS / "cc-status-tmux"
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
KEY_HOOK_EVENTS = {"UserPromptSubmit", "Stop", "Notification", "PermissionRequest", "PreToolUse", "SessionEnd"}


# ----------------------------------------------------------------------------- tmux.conf block
def managed_block(tm_path: Path = DEFAULT_TM_LINK) -> str:
    return "\n".join([
        MANAGED_BEGIN,
        "# Let iTerm2-specific escape sequences (status, notifications, tab color) reach the terminal.",
        "set -g allow-passthrough on",
        "# Show Claude Code state (written by cc-status-tmux) in the status line for normal / remote attaches.",
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
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name in tmstatusline.WRAPPER_NAMES and argv0.is_file():
        # a symlink is resolved (~/.local/bin/kalmux -> the checkout); a real script is kept as it is
        return argv0.resolve() if argv0.is_symlink() else argv0.absolute()
    if SOURCE_CHECKOUT:
        return SOURCE_CHECKOUT / "bin" / "kalmux"
    # the script installed next to THIS interpreter, before anything a stale PATH may still point at.
    # sys.executable is NOT resolved first: in a venv that would follow the python symlink out of the venv.
    here = Path(sys.executable).parent / "kalmux"
    if here.is_file():
        return here
    found = shutil.which("kalmux")
    return Path(found).resolve() if found else here


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
    """A symlink of ours: it points into this checkout (true for the dangling pre-rename bin/tm link too) or
    straight at the installed `kalmux` command."""
    return link.is_symlink() and (os.readlink(link).startswith(str(ROOT)) or os.readlink(link) == str(CLI))


def is_the_command(link: Path) -> bool:
    """True when `link` IS the kalmux executable (the console script of an installed package), rather than a
    symlink pointing at it. A symlink of ours resolves to the same file, so resolve() alone cannot tell them apart."""
    return not link.is_symlink() and link.exists() and _same_path(CLI, link)


def symlink_cli(link: Path = DEFAULT_TM_LINK) -> str:
    """Put `kalmux` on PATH. An installed package already did: when the command IS that path, leave it
    alone instead of moving the real executable aside and replacing it with a symlink to itself."""
    if is_the_command(link):
        return f"{link} is the installed kalmux command; left alone"
    return symlink(CLI, link)


def alias_symlink(target: Path, link: Path) -> bool:
    """Create a legacy-name alias (`kmux`, `tm`) only when the path is free or already ours: an unrelated
    program of the same name is never clobbered."""
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
    python = python or sys.executable
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
def setup_steps(ui: bool = True, tm_link: Path = DEFAULT_TM_LINK, hook_link: Path = DEFAULT_HOOK_LINK,
                statusline: bool = True, settings_path: Path = DEFAULT_SETTINGS) -> list[tuple[str, callable]]:
    steps = [
        # first: everything below (the config file, the saved status line, the pidfile) lives in the state dir
        (STATE_DIR_STEP, migrate_state_dir),
        ("config file", tmconfig.ensure_config),
        ("hook symlink", lambda: symlink(WRAPPER, hook_link)),
        ("kalmux symlink", lambda: symlink_cli(tm_link)),
    ]
    steps += [(f"{link.name} alias symlink (skipped if the path is not ours)",
               lambda link=link: alias_symlink(CLI, link)) for link in LEGACY_LINKS if not is_the_command(link)]
    steps += [
        ("iTerm2 prefs", write_iterm_prefs),
        ("tmux.conf block", lambda: write_tmux_conf(DEFAULT_TMUX_CONF, tm_link)),
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


def run_setup(dry_run: bool = False, ui: bool = True, statusline: bool = True, printer=print) -> int:
    failures = 0
    for name, fn in setup_steps(ui=ui, statusline=statusline):
        if dry_run:
            printer(f"would: {name}")
            continue
        try:
            note = fn()
            printer(f"✓ {name}" + (f" ({note})" if isinstance(note, str) and note else ""))
        except (OSError, ValueError) as exc:
            failures += 1
            printer(f"✗ {name}: {exc}")
    return failures


def _hook_commands(settings_path: Path) -> dict[str, list[str]]:
    hooks = json.loads(settings_path.read_text()).get("hooks", {})
    return {ev: [h.get("command", "") for g in groups for h in g.get("hooks", [])] for ev, groups in hooks.items()}


def doctor_checks(settings_path: Path = DEFAULT_SETTINGS, hook_link: Path = DEFAULT_HOOK_LINK, wrapper: Path = WRAPPER,
                  tmux_conf: Path = DEFAULT_TMUX_CONF, defaults_reader=None, ui_health=None, autolaunch=None,
                  ui: bool = True, cli_link: Path = DEFAULT_TM_LINK, alias_links: tuple[Path, ...] = LEGACY_LINKS,
                  autolaunch_paths=None, state_dir: Path = STATE_DIR, statusline_status=None,
                  statusline: bool = True, config_loader=None) -> list[dict]:
    """ui=False (a machine set up with `kalmux setup --no-ui`) skips the toolbelt / AutoLaunch / server checks;
    statusline=False (`--no-statusline`) skips the two status-line checks the same way."""
    checks: list[dict] = []

    def add(name: str, ok: bool, info: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "info": info})

    def link_info(link: Path) -> str:
        return f"{link} -> {os.readlink(link)}" if link.is_symlink() else (f"{link} is not a symlink (left alone)" if link.exists() else f"{link} missing")

    config = (config_loader() if config_loader else tmconfig.load_config()) or {}
    config_errors = config.get("_errors") or []
    add("kalmux config file", not config_errors, "; ".join(config_errors) or str(tmconfig.config_path()))

    # the rename moved bin/kmux to bin/kalmux: a pre-rename ~/.local/bin/kmux symlink dangles until setup runs
    add("kalmux on PATH", is_the_command(cli_link) or (cli_link.is_symlink() and cli_link.resolve() == CLI.resolve()),
        (f"{cli_link} is the installed command" if is_the_command(cli_link) else link_info(cli_link)) + " (kalmux setup)")
    for alias_link in alias_links:
        add(f"{alias_link.name} alias", is_the_command(alias_link) or
            (alias_link.is_symlink() and alias_link.resolve() == CLI.resolve()), link_info(alias_link))

    try:
        link_ok = hook_link.is_symlink() and hook_link.resolve() == wrapper.resolve()
        add("hook symlink -> wrapper", link_ok, f"{hook_link} -> {os.readlink(hook_link) if hook_link.is_symlink() else 'missing'}")
    except OSError as exc:
        add("hook symlink -> wrapper", False, str(exc))
    add("wrapper executable", os.access(wrapper, os.X_OK), str(wrapper))
    try:
        by_event = _hook_commands(settings_path)
        uses = [c for cmds in by_event.values() for c in cmds if str(hook_link) in c or "cc-status" in c]
        add("settings.json hooks use cc-status", bool(uses), f"{len(uses)} hook command(s) reference cc-status")
        wired = {ev for ev, cmds in by_event.items() if any(str(hook_link) in c or "cc-status" in c for c in cmds)}
        missing = KEY_HOOK_EVENTS - wired
        add("key hook events wired", not missing, "missing: " + ", ".join(sorted(missing)) if missing else "all key events wired")
    except (OSError, ValueError) as exc:
        add("settings.json hooks use cc-status", False, str(exc))
        add("key hook events wired", False, str(exc))
    add("jq installed", bool(shutil.which("jq")), shutil.which("jq") or "install: brew install jq")
    add("tmux installed", bool(resolve_tmux()), resolve_tmux() or "")
    try:
        conf = tmux_conf.read_text()
    except OSError:
        conf = ""
    add("tmux.conf allow-passthrough", "allow-passthrough on" in conf, str(tmux_conf))
    add("tmux.conf client-attached hook (status replay)", "client-attached" in conf, str(tmux_conf))
    if statusline:
        line = statusline_status() if statusline_status else tmstatusline.status(settings_path, state_dir)
        saved_there = line.get("saved_present", bool(line.get("saved_command")))
        if not line["routed"]:
            add("statusLine routed through kalmux", False, "kalmux statusline install")
        elif not saved_there:
            # routed with no saved original: uninstall cannot put anything back and the tap prints our default
            add("statusLine routed through kalmux", False,
                f"routed, but the saved original is gone ({tmstatusline.saved_path(state_dir)}); recover it from "
                f"{settings_path}{tmstatusline.BACKUP_SUFFIX}, then run `kalmux statusline install` again")
        else:
            add("statusLine routed through kalmux", True, f"original saved: {line['saved_command'] or 'none'}")
        # informational: a machine simply nobody has used in the last 10 minutes is healthy
        add("status files fresh", True,
            f"{line['fresh']} file(s) newer than {tmstatusline.FRESH_SECONDS // 60} min in {state_dir / 'status'}"
            " (0 is normal until a Claude session refreshes its status line)")
    reader = defaults_reader or defaults_read
    if sys.platform == "darwin":
        add("iTerm2 OpenTmuxWindowsIn=2 (tabs in attaching window)", reader("OpenTmuxWindowsIn") == "2", "Settings > General > tmux")
        add("iTerm2 AutoHideTmuxClientSession=1", reader("AutoHideTmuxClientSession") == "1", "Settings > General > tmux")
    if sys.platform == "darwin" and ui:
        add("iTerm2 toolbelt tool registered", toolbelt_registered(reader), f"{TOOLBELT_ID} in NoSyncDynamicTools (kalmux ui show)")
        state = autolaunch() if autolaunch else autolaunch_state()
        add("iTerm2 AutoLaunch script starts the ui server", state == "ours", f"{AUTOLAUNCH}: {state} (kalmux ui install)")
        if state == "ours":
            targets = autolaunch_paths() if autolaunch_paths else autolaunch_targets()
            missing = [t for t in targets if not os.path.exists(t)]
            add("AutoLaunch script targets exist", bool(targets) and not missing,
                ("missing: " + ", ".join(missing) + " (kalmux ui install)") if missing else ", ".join(targets) or "could not read the script")
        if ui_health is not None:
            h = ui_health()
            add("ui server reachable", bool(h), f"{UI_URL} pid {h.get('pid')}" if h else f"{UI_URL} not answering (kalmux ui start)")
            if h:
                add("ui server can run tmux", bool(h.get("tmux")),
                    h.get("tmux") or "the server's PATH has no tmux: `kalmux ui restart` from an iTerm2 shell, then `kalmux ui install`")
                running = h.get("version") or "unknown"
                add("ui server runs this code", running == VERSION,
                    f"server {running}, checkout {VERSION}"
                    + ("" if running == VERSION else " — kalmux ui restart (from an iTerm2 shell)"))
    return checks
