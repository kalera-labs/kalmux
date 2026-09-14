"""kalmux — the command line: one `cmd_*` function per subcommand, plus the argument parser.

Works at the desk (iTerm2 control mode, toolbelt web UI) and over SSH (no iTerm2 needed for `ls`).
State comes from tmux pane options written by the cc-status-tmux hook and from Claude Code's
registry; iTerm2 is driven through its bundled `it2` CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import tmconfig, tmsetup, tmstatusline
from .tmactions import (TabMap, action_color, action_detach, action_forget, action_go, action_kill, action_new,
                        action_open, action_rename, action_resume, find_trail, reapply, resolve_session, trails_named)
from .tmconfig import load_config
from .tmcore import (DEFAULT_REGISTRY, It2, Tmux, attach_command, fmt_age, load_registry, load_status,
                     load_trails, merge, render_table, resumable_trails, sort_rows, tombstones)
from .tmserver import health, serve
from .tmsetup import (DEFAULT_HOOK_LINK, DEFAULT_SETTINGS, DEFAULT_TM_LINK, DEFAULT_TMUX_CONF, STATE_DIR,
                      STATUS_DIR, TRACE_DIR, UI_PORT, UI_URL, WRAPPER, doctor_checks)

PIDFILE = STATE_DIR / "ui.pid"
# written by a server started before a rename, in a state dir `kalmux setup` has not migrated yet
LEGACY_PIDFILES = tuple(d / "ui.pid" for d in tmsetup.LEGACY_STATE_DIRS)


def _report(result) -> int:
    print(result.message, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


# ----------------------------------------------------------------------------- session commands
def cmd_ls(tmux: Tmux, registry: dict, json_out: bool = False, now: int | None = None, status: dict | None = None) -> int:
    now = int(time.time()) if now is None else now
    rows = sort_rows(merge(tmux.list_panes(), registry, now, status))
    if json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        print(render_table(rows, color=sys.stdout.isatty()))
    return 0


def cmd_attach(tmux: Tmux, session: str, do_exec: bool = True) -> int:
    if not tmux.has_session(session):
        print(f"kalmux: no tmux session named {session!r}", file=sys.stderr)
        return 1
    cmd = attach_command(session, os.environ)
    if do_exec and sys.stdout.isatty():
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            print(f"kalmux: {exc}", file=sys.stderr)
            return 1
    print(" ".join(cmd))
    return 0


def cmd_new(tmux: Tmux, name: str, cwd: str | None, color: str | None, attach: bool, start_claude: bool = False,
            claude_cmd: str = "claude") -> int:
    r = action_new(tmux, name, cwd or "", color or "", start_claude, claude_cmd)
    if not r.ok:
        return _report(r)
    cmd = attach_command(name, os.environ)
    if attach and sys.stdout.isatty():
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            print(f"kalmux: {exc}", file=sys.stderr)
    print(f"Created tmux session {name!r}. Attach with: " + " ".join(cmd))
    return 0


def cmd_go(tmux: Tmux, it2: It2, query: str) -> int:
    """`kalmux go <anything>`: an exact session name, or part of a name, project or Claude title."""
    found = resolve_session(merge(tmux.list_panes(), {}, int(time.time())), query)
    if not found.ok:
        return _report(found)
    return _report(action_go(tmux, it2, TabMap(it2), found.data["session"]))


def cmd_open(tmux: Tmux, it2: It2, session: str, window: str = "", wait: float = 2.0) -> int:
    r = action_open(tmux, it2, session, window)
    if r.ok:
        time.sleep(wait)          # the new tab needs a moment before its pane pty reaches iTerm2
        reapply(tmux, session)
    return _report(r)


# ----------------------------------------------------------------------------- gone sessions
def _dead_line(r: dict) -> str:
    """One tombstone as a line: alpha  killed  12m  my-proj  ✳ last message  (id prefix)"""
    age = fmt_age(int(time.time()) - r["last_ts"]) if r["last_ts"] else "-"
    cells = [f"{r['tmux_session'] or '-':<16.16}", f"{r['ended']:<10}", f"{age:>6}", f"{r['project']:<16.16}",
             f"{r['session_id'][:8]}  {r['last_message'][:60]}"]
    return "  ".join(cells).rstrip()


def cmd_dead(trace_dir, registry: dict, show_all: bool = False, json_out: bool = False,
             now: int | None = None, keep_days: int = 30) -> int:
    now = int(time.time()) if now is None else now
    rows = tombstones(load_trails(Path(trace_dir), now, keep_days), registry)
    if not show_all:
        rows = [r for r in rows if r["ended"] != "superseded"]
    if json_out:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print(f"no dead Claude sessions in the last {keep_days} days")
        return 0
    print(f"{'SESSION':<16}  {'ENDED':<10}  {'AGE':>6}  {'PROJECT':<16}  ID        LAST MESSAGE")
    for r in rows:
        print(_dead_line(r))
    return 0


def cmd_resume(tmux: Tmux, it2: It2 | None, trace_dir, registry: dict, cfg: dict, query: str,
               now: int | None = None) -> int:
    """Bring a dead Claude session back: new tmux session, old cwd and color, `claude --resume <id>` typed in.

    Only tombstones are candidates (`resumable_trails`), so a live conversation can never be forked."""
    now = int(time.time()) if now is None else now
    trails = resumable_trails(Path(trace_dir), registry, now, cfg["tombstones"]["keep_days"])
    trail = find_trail(trails, query)
    if trail is None:
        print(f"kalmux: no trail matches {query!r} (see `kalmux dead`)", file=sys.stderr)
        return 1
    r = action_resume(tmux, cfg, trail, registry, matches=len(trails_named(trails, query)))
    if not r.ok:
        return _report(r)
    if it2 is not None and sys.stdout.isatty():
        action_open(tmux, it2, r.data["name"])
    return _report(r)


def cmd_forget(trace_dir, session_id: str, status_dir=None) -> int:
    return _report(action_forget(Path(trace_dir), session_id, status_dir))


# ----------------------------------------------------------------------------- ui server
def cmd_ui(args) -> int:
    sub = args.ui_cmd
    if sub == "serve":
        return serve(args.port, pidfile=PIDFILE)
    if sub == "url":
        print(UI_URL)
        return 0
    if sub == "status":
        h = health(UI_PORT)
        print(f"server:     {'running (pid ' + str(h.get('pid')) + ')' if h else 'not running'}  {UI_URL}")
        print(f"AutoLaunch: {tmsetup.autolaunch_state()}  ({tmsetup.AUTOLAUNCH})")
        print(f"toolbelt:   {'registered' if tmsetup.toolbelt_registered() else 'not registered'}  ({tmsetup.TOOLBELT_ID})")
        return 0 if h else 1
    if sub == "start":
        return _ui_start()
    if sub == "stop":
        return _ui_stop()
    if sub == "restart":                       # after a code change: the running server keeps the old code
        return _ui_start() if _ui_stop() == 0 else 1
    if sub == "show":
        rc = _ui_start()
        ok, out = tmsetup.register_toolbelt(UI_URL, show=True)
        print(out)
        return 0 if ok and rc == 0 else 1
    if sub == "install":
        try:
            tmsetup.install_autolaunch()
            print(f"✓ AutoLaunch script {tmsetup.AUTOLAUNCH} (iTerm2 starts the server on launch)")
        except (OSError, ValueError) as exc:
            print(f"✗ AutoLaunch: {exc}", file=sys.stderr)
            return 1
        if _ui_start() != 0:
            return 1
        ok, out = tmsetup.register_toolbelt(UI_URL, show=True)
        print(("✓ " if ok else "✗ ") + out)
        return 0 if ok else 1
    if sub == "uninstall":
        removed = tmsetup.uninstall_autolaunch()
        _ui_stop()
        print("AutoLaunch script removed" if removed else "no AutoLaunch script of ours to remove",
              "(the toolbelt tool stays registered; remove it in iTerm2 > View > Toolbelt)")
        return 0
    return 2


def _ui_start() -> int:
    if health(UI_PORT):
        print(f"kalmux ui: already running on {UI_URL}")
        return 0
    if tmsetup.start_server(Path(os.path.realpath(__file__))):
        print(f"kalmux ui: running on {UI_URL}")
        return 0
    print(f"kalmux ui: server did not come up; see {tmsetup.UI_LOG}", file=sys.stderr)
    return 1


def _is_tm_serve(pid: int) -> bool:
    """Does this pid still belong to a `kalmux ui serve` (or the pre-rename `tm ui serve`)? A pidfile outlives
    an unclean death (crash, SIGKILL, power loss) and the number gets recycled, so it is never trusted alone."""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "ui serve" in out.stdout.decode("utf-8", "replace")


def _server_pid() -> int | None:
    """Our server's pid: what /healthz reports, else a pidfile (current or pre-rename location) whose process
    really is a `ui serve`."""
    h = health(UI_PORT)
    if h and isinstance(h.get("pid"), int):
        return h["pid"]
    for pidfile in (PIDFILE, *LEGACY_PIDFILES):
        try:
            pid = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid > 0 and _is_tm_serve(pid):
            return pid
    return None


def _drop_pidfile() -> None:
    for pidfile in (PIDFILE, *LEGACY_PIDFILES):
        try:
            pidfile.unlink()
        except OSError:
            pass


def _ui_stop(wait: float = 5.0) -> int:
    pid = _server_pid()
    if pid is None:
        _drop_pidfile()                        # stale or absent: nothing of ours is running
        print("kalmux ui: nothing to stop")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"kalmux ui: could not signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    print(f"kalmux ui: sent SIGTERM to pid {pid}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and health(UI_PORT):
        time.sleep(0.1)                        # so that `tm ui stop && tm ui start` does not race the old server
    _drop_pidfile()
    return 0


# ----------------------------------------------------------------------------- setup / doctor
def cmd_doctor(ui: bool = True, statusline: bool = True) -> int:
    # read module-level paths at call time so tests (and callers) can override them
    checks = doctor_checks(settings_path=DEFAULT_SETTINGS, hook_link=DEFAULT_HOOK_LINK, wrapper=WRAPPER, tmux_conf=DEFAULT_TMUX_CONF,
                           ui_health=lambda: health(UI_PORT), ui=ui, state_dir=STATE_DIR, statusline=statusline)
    for c in checks:
        print(f"{'✓' if c['ok'] else '✗'} {c['name']}" + (f"  ({c['info']})" if c["info"] else ""))
    return 0 if all(c["ok"] for c in checks) else 1


def cmd_setup(dry_run: bool = False, ui: bool = True, statusline: bool = True) -> int:
    failures = tmsetup.run_setup(dry_run=dry_run, ui=ui, statusline=statusline)
    if dry_run:
        return 0
    rc = cmd_doctor(ui=ui, statusline=statusline)
    return 1 if failures else rc


# ----------------------------------------------------------------------------- config + status line
def cmd_config() -> int:
    print(tmconfig.describe(tmconfig.load_config()))
    return 0


def cmd_statusline(action: str | None = None) -> int:
    """No action: be the status line Claude Code calls. Otherwise manage the settings.json wiring."""
    if not action:
        return tmstatusline.run_tap(STATE_DIR)
    try:
        if action == "install":
            changed = tmstatusline.install(DEFAULT_SETTINGS, DEFAULT_TM_LINK, STATE_DIR)
            saved = tmstatusline.saved_command(STATE_DIR)
            print(f"✓ statusLine routed through kalmux (original: {saved or 'none'})" if changed
                  else "statusLine is already routed through kalmux")
            return 0
        if action == "uninstall":
            changed, message = tmstatusline.uninstall(DEFAULT_SETTINGS, STATE_DIR)
            if not changed:
                print(f"kalmux statusline: {message}", file=sys.stderr)
                return 1
            print(f"✓ statusLine restored: {message}")
            return 0
    except (OSError, ValueError) as exc:
        print(f"✗ statusLine: {exc}", file=sys.stderr)
        return 1
    report = tmstatusline.status(DEFAULT_SETTINGS, STATE_DIR)
    print(f"routed: {'yes' if report['routed'] else 'no'}  ({report['command'] or 'no statusLine in settings.json'})")
    saved = report["saved_command"] or ("none (you had no status line)" if report["saved_present"] else "MISSING")
    print(f"original: {saved}  ({tmstatusline.saved_path(STATE_DIR)})")
    print(f"fresh status files: {report['fresh']}  ({tmstatusline.status_dir(STATE_DIR)})")
    return 0 if report["routed"] else 1


# ----------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kalmux", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="examples:\n  kalmux ls\n  kalmux color my-proj orange\n  kalmux go my-proj\n  kalmux open my-proj\n  kalmux attach my-proj\n"
                                       "  kalmux new my-proj --cwd ~/Dev/my-proj --color teal --claude\n  kalmux dead\n  kalmux resume alpha\n  kalmux ui show\n  kalmux doctor\n  kalmux setup")
    sub = p.add_subparsers(dest="cmd")
    ls = sub.add_parser("ls", help="list tmux sessions with Claude state (works over SSH)")
    ls.add_argument("--json", action="store_true")
    col = sub.add_parser("color", help="assign an identity color to a tmux session (iTerm2 tab color, persisted in tmux)")
    col.add_argument("session")
    col.add_argument("color", help="#rrggbb, palette name, or 'none'")
    go = sub.add_parser("go", help="focus the iTerm2 tab of a session by name, or by part of its project or Claude title")
    go.add_argument("session", help="session name, or any text from its name, project or title")
    op = sub.add_parser("open", help="open a session as a new control-mode tab in the current iTerm2 window")
    op.add_argument("session")
    op.add_argument("--window", default="", help="iTerm2 window id (default: the current window)")
    at = sub.add_parser("attach", help="attach with the right verb for this terminal (-CC in iTerm2, switch-client inside tmux)")
    at.add_argument("session")
    new = sub.add_parser("new", help="create a named tmux session (and attach when run from a terminal)")
    new.add_argument("name")
    new.add_argument("--cwd")
    new.add_argument("--color")
    new.add_argument("--claude", action="store_true", help="start `claude` in the new session")
    new.add_argument("--no-attach", action="store_true")
    for verb, help_text in (("kill", "kill a tmux session"), ("detach", "detach every client of a session (it keeps running)")):
        sp = sub.add_parser(verb, help=help_text)
        sp.add_argument("session")
    rn = sub.add_parser("rename", help="rename a tmux session")
    rn.add_argument("session")
    rn.add_argument("name")
    dead = sub.add_parser("dead", help="list Claude sessions that are gone (killed, or ended cleanly)")
    dead.add_argument("--all", action="store_true", help="also show sessions superseded by /clear or a resume")
    dead.add_argument("--json", action="store_true")
    res = sub.add_parser("resume", help="resume a dead Claude session in a new tmux session")
    res.add_argument("session", help="session id, id prefix, or the tmux session it used to run in")
    fg = sub.add_parser("forget", help="drop a dead session's trail so it stops showing up in `kalmux dead`")
    fg.add_argument("session_id")
    ra = sub.add_parser("reapply", help="re-send Claude status + tab color to attached panes (used by the tmux client-attached hook)")
    ra.add_argument("session", nargs="?")
    ui = sub.add_parser("ui", help="the toolbelt web UI server: serve | start | stop | restart | status | show | install (AutoLaunch + toolbelt) | uninstall | url")
    ui.add_argument("ui_cmd", choices=["serve", "start", "stop", "restart", "status", "show", "install", "uninstall", "url"])
    ui.add_argument("--port", type=int, default=UI_PORT)
    doc = sub.add_parser("doctor", help="check hook, symlink, tmux.conf, iTerm2 prefs, AutoLaunch, toolbelt, ui server")
    doc.add_argument("--no-ui", action="store_true", help="skip the AutoLaunch / toolbelt / ui server checks")
    doc.add_argument("--no-statusline", action="store_true", help="skip the statusLine checks (nothing routed on purpose)")
    setup = sub.add_parser("setup", help="install everything idempotently (symlinks, prefs, tmux.conf block, AutoLaunch, server, toolbelt)")
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--no-ui", action="store_true", help="skip the AutoLaunch script, server start and toolbelt registration")
    setup.add_argument("--no-statusline", action="store_true", help="leave Claude Code's statusLine alone (no context gauge)")
    sub.add_parser("config", help=f"show the kalmux configuration ({tmconfig.config_path()})")
    sl = sub.add_parser("statusline", help="with no argument: the statusLine command Claude Code runs (records the context "
                                           "gauge, then calls your original one); install | uninstall | status manage the wiring")
    sl.add_argument("action", nargs="?", default=None, choices=["install", "uninstall", "status"])
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    tmux = Tmux()
    if args.cmd == "ls":
        return cmd_ls(tmux, load_registry(DEFAULT_REGISTRY), json_out=args.json, status=load_status(STATUS_DIR))
    if args.cmd == "color":
        return _report(action_color(tmux, args.session, args.color))
    if args.cmd == "go":
        return cmd_go(tmux, It2(), args.session)
    if args.cmd == "open":
        return cmd_open(tmux, It2(), args.session, args.window)
    if args.cmd == "attach":
        return cmd_attach(tmux, args.session)
    if args.cmd == "new":
        return cmd_new(tmux, args.name, cwd=args.cwd, color=args.color, attach=not args.no_attach,
                       start_claude=args.claude, claude_cmd=load_config()["claude"]["new"])
    if args.cmd == "kill":
        return _report(action_kill(tmux, args.session))
    if args.cmd == "detach":
        return _report(action_detach(tmux, args.session))
    if args.cmd == "rename":
        return _report(action_rename(tmux, args.session, args.name))
    if args.cmd == "dead":
        cfg = load_config()
        return cmd_dead(TRACE_DIR, load_registry(DEFAULT_REGISTRY), show_all=args.all, json_out=args.json,
                        keep_days=cfg["tombstones"]["keep_days"])
    if args.cmd == "resume":
        return cmd_resume(tmux, It2(), TRACE_DIR, load_registry(DEFAULT_REGISTRY), load_config(), args.session)
    if args.cmd == "forget":
        return cmd_forget(TRACE_DIR, args.session_id, STATUS_DIR)
    if args.cmd == "reapply":
        return _report(reapply(tmux, args.session))
    if args.cmd == "ui":
        return cmd_ui(args)
    if args.cmd == "doctor":
        return cmd_doctor(ui=not args.no_ui, statusline=not args.no_statusline)
    if args.cmd == "setup":
        return cmd_setup(dry_run=args.dry_run, ui=not args.no_ui, statusline=not args.no_statusline)
    if args.cmd == "config":
        return cmd_config()
    if args.cmd == "statusline":
        return cmd_statusline(args.action)
    parser.print_help()
    return 0


