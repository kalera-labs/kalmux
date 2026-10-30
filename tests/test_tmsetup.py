"""Tests for lib/tmsetup.py (managed block, AutoLaunch script, server start, toolbelt registration, doctor, setup)."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from fakes import FakeTmux, pane
from kalmux import tmhooks, tmrestore, tmserver, tmsetup, tmsnapshot


def test_managed_block_has_passthrough_and_replay_hook():
    block = tmsetup.managed_block(Path("/Users/x/.local/bin/tm"))
    assert "set -g allow-passthrough on" in block
    # without it, killing the LAST session stops the server, which is exactly what a crash looks like
    assert "set -s exit-empty off" in block
    assert "set-hook -g client-attached 'run-shell -b \"sleep 2; /Users/x/.local/bin/tm reapply >/dev/null 2>&1 || true\"'" in block
    assert block.startswith(tmsetup.MANAGED_BEGIN) and block.rstrip().endswith(tmsetup.MANAGED_END)


def test_upsert_managed_block_is_idempotent_and_replaces_old_headers():
    block = tmsetup.MANAGED_BLOCK
    once = tmsetup.upsert_managed_block("set -g mouse on\n", block)
    twice = tmsetup.upsert_managed_block(once, block)
    assert once == twice and once.count("# >>> kalmux") == 1 and once.startswith("set -g mouse on\n")
    assert tmsetup.upsert_managed_block("", block).startswith("# >>> kalmux")
    old = "set -g mouse on\n\n# >>> kalmux (managed block; edit via /some/path) >>>\nset -g allow-passthrough on\n# <<< kalmux <<<\n"
    out = tmsetup.upsert_managed_block(old, block)
    assert out.count("# >>> kalmux") == 1 and "/some/path" not in out and "client-attached" in out
    # blocks written under an older name must be replaced, not left sitting next to the new one - and a
    # machine that ran BOTH older releases has both of them to clear out
    def named(name: str) -> str:
        return (f"# >>> {name} (managed block; edit via bin/{name} setup) >>>\nset -g allow-passthrough on\n"
                f"set-hook -g client-attached 'run-shell -b \"{name} old\"'\n# <<< {name} <<<\n")
    for legacy in (f"set -g mouse on\n\n{named('tmux-manager')}",
                   f"set -g mouse on\n\n{named('kmux')}",
                   f"set -g mouse on\n\n{named('tmux-manager')}\n{named('kmux')}"):
        out = tmsetup.upsert_managed_block(legacy, block)
        assert "tmux-manager" not in out and "# >>> kmux" not in out and "kmux old" not in out
        assert out.count("# >>> kalmux") == 1 and out.startswith("set -g mouse on\n")


def test_write_tmux_conf_upserts_and_sources(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **_k: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(tmsetup, "resolve_tmux", lambda: "/opt/homebrew/bin/tmux")
    conf = tmp_path / "tmux.conf"
    conf.write_text("set -g mouse on\n")
    tmsetup.write_tmux_conf(conf, Path("/opt/tm"))
    text = conf.read_text()
    assert text.startswith("set -g mouse on\n") and "/opt/tm reapply" in text
    assert calls == [["/opt/homebrew/bin/tmux", "source-file", str(conf)]]
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **_k: (1, "no server running on /tmp/tmux-501/default"))
    tmsetup.write_tmux_conf(conf, Path("/opt/tm"))                       # no server yet: fine
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **_k: (1, "unknown option: allow-passthrough"))
    with pytest.raises(OSError, match="tmux refused"):                   # tmux rejected the conf: say so
        tmsetup.write_tmux_conf(conf, Path("/opt/tm"))


# ---------- AutoLaunch ----------
def test_autolaunch_source_bakes_path_and_quotes():
    src = tmsetup.autolaunch_source(Path("/Users/k a/.local/bin/tm"), python="/opt/homebrew/bin/python3.13", extra_path="/opt/homebrew/bin")
    # PATH prefix: AutoLaunch runs under the login PATH, where Homebrew's tmux is invisible to the server
    assert src == ("do shell script \"PATH='/opt/homebrew/bin':$PATH '/opt/homebrew/bin/python3.13' '/Users/k a/.local/bin/tm'"
                   " ui start >/dev/null 2>&1 &\"")
    for bad_python, bad_tm in [('/tmp/py"3', Path("/x/tm")), ("/usr/bin/python3", Path('/tmp/we"ird')), ("/tmp/it's/python", Path("/x/tm"))]:
        with pytest.raises(ValueError, match="quoted"):
            tmsetup.autolaunch_source(bad_tm, python=bad_python, extra_path="/opt/homebrew/bin")
    with pytest.raises(ValueError, match="quoted"):
        tmsetup.autolaunch_source(Path("/x/tm"), python="/usr/bin/python3", extra_path="/bad'dir")


def test_autolaunch_source_defaults_to_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(tmsetup, "resolve_tmux", lambda: "/opt/homebrew/bin/tmux")
    src = tmsetup.autolaunch_source(Path("/x/tm"))
    py_dir = str(Path(tmsetup.sys.executable).parent)
    assert src.startswith("do shell script \"PATH='/opt/homebrew/bin:") and py_dir in src.split("':$PATH")[0]
    assert f"'{tmsetup.sys.executable}' '/x/tm' ui start >/dev/null 2>&1 &" in src
    monkeypatch.setattr(tmsetup, "resolve_tmux", lambda: "")
    assert tmsetup.autolaunch_source(Path("/x/tm"), python="/py/bin/python3").startswith("do shell script \"PATH='/py/bin:/opt/homebrew/bin:/usr/local/bin':$PATH")


def test_autolaunch_install_state_and_uninstall(tmp_path, monkeypatch):
    script, stamp = tmp_path / "Scripts" / "AutoLaunch.scpt", tmp_path / "state" / "autolaunch.sha256"
    compiled = []

    def fake_run(cmd, **_k):
        if cmd[0] == "osadecompile":                       # "decompile" = strip our fake compile prefix
            raw = Path(cmd[1]).read_bytes()
            return (0, raw[len(b"compiled:"):].decode()) if raw.startswith(b"compiled:") else (0, "")
        compiled.append(cmd)
        Path(cmd[2]).write_bytes(b"compiled:" + cmd[4].encode())
        return 0, ""
    monkeypatch.setattr(tmsetup, "_run", fake_run)
    monkeypatch.setattr(tmsetup, "resolve_tmux", lambda: "/opt/homebrew/bin/tmux")
    assert tmsetup.autolaunch_state(script, stamp) == "missing"
    tmsetup.install_autolaunch(Path("/x/tm"), script, stamp, python="/opt/homebrew/bin/python3.13")
    assert compiled[0][:3] == ["osacompile", "-o", str(script)]
    assert "'/x/tm' ui start" in compiled[0][4] and "'/opt/homebrew/bin/python3.13'" in compiled[0][4]
    assert tmsetup.autolaunch_state(script, stamp) == "ours"
    tmsetup.install_autolaunch(Path("/x/tm"), script, stamp, python="/opt/homebrew/bin/python3.13")   # idempotent: ours -> overwrite
    assert len(compiled) == 2
    stamp.unlink()                                          # state dir wiped: our one-liner is still recognised by content
    assert tmsetup.autolaunch_state(script, stamp) == "ours"
    tmsetup.install_autolaunch(Path("/x/tm"), script, stamp, python="/opt/homebrew/bin/python3.13")
    assert len(compiled) == 3 and stamp.exists()
    script.write_bytes(b"someone else's script")
    assert tmsetup.autolaunch_state(script, stamp) == "foreign"
    with pytest.raises(OSError, match="not ours") as exc:
        tmsetup.install_autolaunch(Path("/x/tm"), script, stamp)
    assert "do shell script" in str(exc.value)
    assert tmsetup.uninstall_autolaunch(script, stamp) is False and script.exists()
    stamp.write_text(tmsetup._sha256(script))
    assert tmsetup.uninstall_autolaunch(script, stamp) is True and not script.exists() and not stamp.exists()


def test_install_autolaunch_reports_osacompile_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **_k: (1, "syntax error"))
    try:
        tmsetup.install_autolaunch(Path("/x/tm"), tmp_path / "a.scpt", tmp_path / "s")
    except OSError as exc:
        assert "osacompile failed: syntax error" in str(exc)
    else:
        raise AssertionError("expected OSError")


def test_install_autolaunch_refuses_to_bake_in_an_old_python(tmp_path, monkeypatch):
    """Caught for real on 2026-09-13: a machine crash forced a reboot, AutoLaunch cold-started the
    server through the minimal PATH `do shell script` gets, and that resolved Python 3.9 (Xcode's
    bundled stub) instead of the Homebrew 3.13 the user normally has — 3.9 doesn't support
    `zip(strict=True)`, which the rest of tmux-manager relies on. install_autolaunch must refuse to
    bake in an interpreter older than the project's floor rather than silently shipping this again."""
    calls = []
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **_k: calls.append(cmd) or (0, ""))
    monkeypatch.setattr(tmsetup.sys, "version_info", (3, 9, 6, "final", 0))
    try:
        tmsetup.install_autolaunch(Path("/x/tm"), tmp_path / "a.scpt", tmp_path / "s")
    except OSError as exc:
        assert "3.11" in str(exc) and "modern" in str(exc)
    else:
        raise AssertionError("expected OSError")
    assert calls == []                                        # never even tried osacompile


# ---------- server process ----------
def test_start_server_spawns_detached_and_waits(tmp_path, monkeypatch):
    spawned = []
    answers = iter([None, None, {"pid": 1}])
    monkeypatch.setattr(tmsetup, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tmsetup, "UI_LOG", tmp_path / "ui.log")
    monkeypatch.setattr(tmserver, "health", lambda port: next(answers, {"pid": 1}))   # start_server imports it lazily
    monkeypatch.setattr(tmsetup.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
    monkeypatch.setattr(tmsetup.time, "sleep", lambda _s: None)
    assert tmsetup.start_server(Path("/x/tm"), port=4711) is True
    cmd, kw = spawned[0]
    assert cmd == [tmsetup.sys.executable, "/x/tm", "ui", "serve", "--port", "4711"] and kw["start_new_session"] is True
    assert tmsetup.start_server(Path("/x/tm")) is True and len(spawned) == 1      # already running: no spawn


def test_start_server_gives_up(tmp_path, monkeypatch):
    monkeypatch.setattr(tmsetup, "STATE_DIR", tmp_path)
    monkeypatch.setattr(tmsetup, "UI_LOG", tmp_path / "ui.log")
    monkeypatch.setattr(tmserver, "health", lambda port: None)
    monkeypatch.setattr(tmsetup.subprocess, "Popen", lambda cmd, **kw: None)
    assert tmsetup.start_server(Path("/x/tm"), wait=0.05) is False


# ---------- toolbelt ----------
def test_register_toolbelt_preconditions(monkeypatch):
    monkeypatch.setattr(tmsetup.shutil, "which", lambda name: None)
    ok, msg = tmsetup.register_toolbelt("http://127.0.0.1:1/")
    assert ok is False and "uv" in msg
    monkeypatch.setattr(tmsetup.shutil, "which", lambda name: "/usr/local/bin/uv")
    monkeypatch.setattr(tmsetup, "request_cookie", lambda: "")
    ok, msg = tmsetup.register_toolbelt("http://127.0.0.1:1/")
    assert ok is False and "cookie" in msg


def test_register_toolbelt_runs_uv_with_cookie(monkeypatch):
    seen = {}

    def fake_run(cmd, timeout=0, env=None):
        seen["cmd"], seen["env"] = cmd, env
        return 0, "registered"
    monkeypatch.setattr(tmsetup.shutil, "which", lambda name: "/usr/local/bin/uv")
    monkeypatch.setattr(tmsetup, "request_cookie", lambda: "COOKIE123")
    monkeypatch.setattr(tmsetup, "_run", fake_run)
    monkeypatch.setattr(tmsetup, "prune_legacy_toolbelt", lambda: False)
    ok, out = tmsetup.register_toolbelt("http://127.0.0.1:47321/", show=True)
    assert ok and out == "registered" and seen["env"]["ITERM2_COOKIE"] == "COOKIE123"
    assert seen["cmd"][:5] == ["/usr/local/bin/uv", "run", "--no-project", "--with", "iterm2"]
    assert seen["cmd"][-3:] == ["--url", "http://127.0.0.1:47321/", "--show"]


def test_prune_legacy_toolbelt_drops_the_pre_rename_entries(monkeypatch):
    """Every pre-rename id (kmux, tm) must go, or its box stays in the toolbelt next to the Kalmux one.
    `defaults delete` cannot remove a key inside a dict, so the dict is rewritten whole."""
    import plistlib
    calls = []
    kept = {"vn.kal.kalmux.toolbelt": {"URL": "z", "name": "Kalmux"}}
    prefs = {"NoSyncDynamicTools": {"vn.kal.tm.toolbelt": {"URL": "x", "name": "tmux manager"},
                                    "vn.kal.kmux.toolbelt": {"URL": "y", "name": "Kmux"}, **kept},
             "ToolbeltTools": ["Actions", "tmux manager", "Kmux", "Kalmux"]}
    assert tmsetup.LEGACY_TOOLBELTS == (("vn.kal.kmux.toolbelt", "Kmux"), ("vn.kal.tm.toolbelt", "tmux manager"))
    monkeypatch.setattr(tmsetup.sys, "platform", "darwin")
    monkeypatch.setattr(tmsetup, "export_prefs", lambda domain=None: prefs)
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **kw: (calls.append(cmd), (0, ""))[1])
    assert tmsetup.prune_legacy_toolbelt() is True
    assert calls[0][:4] == ["defaults", "write", tmsetup.ITERM_DOMAIN, "NoSyncDynamicTools"]
    assert plistlib.loads(calls[0][4].encode()) == kept          # both legacy ids gone in ONE rewrite
    assert calls[1] == ["defaults", "write", tmsetup.ITERM_DOMAIN, "ToolbeltTools", "-array", "Actions", "Kalmux"]
    prefs.update({"NoSyncDynamicTools": kept, "ToolbeltTools": ["Actions", "Kalmux"]})
    calls.clear()
    assert tmsetup.prune_legacy_toolbelt() is False and calls == []      # nothing stale left: no writes


def test_alias_symlink_never_clobbers_a_foreign_program(tmp_path):
    target = tmsetup.CLI
    free = tmp_path / "bin" / "tm"
    assert tmsetup.alias_symlink(target, free) is True and free.resolve() == target.resolve()
    assert tmsetup.alias_symlink(target, free) is True                     # idempotent: ours -> refreshed
    dangling = tmp_path / "bin" / "tm-old"
    dangling.symlink_to(tmsetup.ROOT / "bin" / "tm")                      # the pre-rename link, now dangling
    assert tmsetup.link_is_ours(dangling) and tmsetup.alias_symlink(target, dangling) is True
    assert dangling.resolve() == target.resolve()
    foreign = tmp_path / "bin" / "tm-foreign"
    foreign.write_text("#!/bin/sh\necho someone else's tm\n")
    assert tmsetup.alias_symlink(target, foreign) is False and foreign.read_text().startswith("#!/bin/sh")
    elsewhere = tmp_path / "bin" / "tm-link"
    elsewhere.symlink_to("/usr/bin/true")
    assert tmsetup.alias_symlink(target, elsewhere) is False and os.readlink(elsewhere) == "/usr/bin/true"


def test_autolaunch_targets_reads_the_baked_paths(monkeypatch):
    src = ("do shell script \"PATH='/opt/homebrew/bin':$PATH '/opt/homebrew/bin/python3.13' '/Users/k/.local/bin/kalmux'"
           " ui start >/dev/null 2>&1 &\"")
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **kw: (0, src))
    assert tmsetup.autolaunch_targets(Path("/x.scpt")) == ["/opt/homebrew/bin/python3.13", "/Users/k/.local/bin/kalmux"]
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **kw: (1, "boom"))
    assert tmsetup.autolaunch_targets(Path("/x.scpt")) == []


def test_toolbelt_registered_reads_dynamic_tools():
    assert tmsetup.toolbelt_registered(lambda key: '{ "vn.kal.kalmux.toolbelt" = { URL = "http://127.0.0.1:47321/"; }; }') is True
    assert tmsetup.toolbelt_registered(lambda key: "") is False


# ---------- doctor / setup ----------
def kalmux_wired(tmp_path, mode="kalmux"):
    """settings.json with iTerm2's entries plus Kalmux's own, the wrapper, and iTerm2's link to its bundle."""
    settings = tmp_path / "settings.json"
    link = tmp_path / "cc-status"
    wrapper = tmp_path / "pkg" / "kalmux-hook"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/bash\n")
    wrapper.chmod(0o755)
    link.symlink_to("/Applications/iTerm.app/Contents/Resources/utilities/cc-status")
    hooks = {ev: [{"hooks": [{"type": "command", "command": str(link)}]}] for ev in tmhooks.HOOK_EVENTS}
    settings.write_text(json.dumps({"hooks": hooks}))
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, mode))
    return settings, link, wrapper


HOOKS_OK = "Claude Code hooks call Kalmux"
REACH = "hooks reach Kalmux"


def test_doctor_checks_full_report(tmp_path):
    settings, link, wrapper = kalmux_wired(tmp_path)
    conf = tmp_path / "tmux.conf"
    conf.write_text(tmsetup.MANAGED_BLOCK)
    reader = lambda key: {"OpenTmuxWindowsIn": "2", "AutoHideTmuxClientSession": "1", "NoSyncDynamicTools": "vn.kal.kalmux.toolbelt"}.get(key, "")
    cli_link = tmp_path / "kalmux"
    alias_links = (tmp_path / "kmux", tmp_path / "tm")
    for each in (cli_link, *alias_links):
        each.symlink_to(tmsetup.CLI)
    common = {"settings_path": settings, "hook_link": link, "wrapper": wrapper, "tmux_conf": conf, "defaults_reader": reader,
              "cli_link": cli_link, "alias_links": alias_links, "autolaunch_paths": lambda: ["/bin/sh", str(tmsetup.CLI)],
              "state_dir": tmp_path / "state",
              "statusline_status": lambda: {"routed": True, "command": "kalmux statusline", "saved_command": "bun hud.ts",
                                            "saved_present": True, "fresh": 1},
              "config_loader": lambda: {"_errors": []}, "registry_loader": lambda: {}, "iterm_version": lambda: None,
              "snapshot_status": lambda: (True, "3 session(s), 2s old")}
    healthy = {"pid": 42, "tmux": "/opt/homebrew/bin/tmux", "version": tmsetup.VERSION}
    report = tmsetup.doctor_checks(ui_health=lambda: healthy, autolaunch=lambda: "ours", **common)
    names = {c["name"]: c for c in report}
    assert names[HOOKS_OK]["ok"] and names[REACH]["ok"] and names["tmux.conf client-attached hook (status replay)"]["ok"]
    assert names["iTerm2 owns ~/.config/iterm2/cc-status"]["ok"]
    assert all(set(c) >= {"name", "ok", "info", "remedy"} for c in report)
    assert names[tmsetup.EXIT_EMPTY_CHECK]["ok"]
    assert names["kalmux on PATH"]["ok"]
    assert names["kmux alias"]["ok"] and names["tm alias"]["ok"]
    if os.uname().sysname == "Darwin":
        assert names["iTerm2 toolbelt tool registered"]["ok"] and names["iTerm2 AutoLaunch script starts the ui server"]["ok"]
        assert names["AutoLaunch script targets exist"]["ok"]
        assert names["ui server reachable"]["ok"] and "pid 42" in names["ui server reachable"]["info"]
        assert names["ui server can run tmux"]["ok"] and names["ui server can run tmux"]["info"] == "/opt/homebrew/bin/tmux"
    expected_ok = bool(shutil.which("jq")) and bool(tmsetup.resolve_tmux())
    assert all(c["ok"] for c in report) == expected_ok
    # the server answers but cannot see tmux (started under the login PATH): a red check with the remedy
    blind = {c["name"]: c for c in tmsetup.doctor_checks(ui_health=lambda: {**healthy, "tmux": ""}, autolaunch=lambda: "ours", **common)}
    if os.uname().sysname == "Darwin":
        assert blind["ui server can run tmux"]["ok"] is False and "kalmux ui restart" in blind["ui server can run tmux"]["info"]
    # a dangling pre-rename link and an AutoLaunch script that points at a python that is gone
    alias_links[1].unlink()
    alias_links[1].symlink_to(tmsetup.ROOT / "bin" / "tm")
    broken = {c["name"]: c for c in tmsetup.doctor_checks(ui_health=lambda: None, autolaunch=lambda: "ours", **{**common, "autolaunch_paths": lambda: ["/nonexistent/python3"]})}
    assert broken["tm alias"]["ok"] is False and "bin/tm" in broken["tm alias"]["info"]
    assert broken["kmux alias"]["ok"] is True
    if os.uname().sysname == "Darwin":
        assert broken["AutoLaunch script targets exist"]["ok"] is False and "/nonexistent/python3" in broken["AutoLaunch script targets exist"]["info"]
    # --no-ui machines: no toolbelt / AutoLaunch / server checks at all
    no_ui = {c["name"] for c in tmsetup.doctor_checks(ui_health=lambda: None, autolaunch=lambda: "missing", ui=False, **common)}
    assert not {n for n in no_ui if "ui server" in n or "AutoLaunch" in n or "toolbelt" in n}


def test_doctor_checks_reports_missing_pieces(tmp_path):
    report = tmsetup.doctor_checks(settings_path=tmp_path / "x.json", hook_link=tmp_path / "l", wrapper=tmp_path / "w",
                                   tmux_conf=tmp_path / "c", defaults_reader=lambda key: "", ui_health=lambda: None,
                                   autolaunch=lambda: "foreign")
    names = {c["name"]: c for c in report}
    assert names[HOOKS_OK]["ok"] is False and "no Kalmux entry" in names[HOOKS_OK]["info"]
    assert names[HOOKS_OK]["fix"] == tmsetup.HOOKS_STEP and "doctor --fix" in names[HOOKS_OK]["remedy"]
    assert all(c["remedy"] for c in report if not c["ok"])           # every red line says what to do
    assert names["tmux.conf allow-passthrough"]["ok"] is False
    assert names[tmsetup.EXIT_EMPTY_CHECK]["ok"] is False
    if os.uname().sysname == "Darwin":
        assert names["ui server reachable"]["ok"] is False and "kalmux ui start" in names["ui server reachable"]["info"]
        assert names["iTerm2 AutoLaunch script starts the ui server"]["ok"] is False and "foreign" in names["iTerm2 AutoLaunch script starts the ui server"]["info"]


def test_run_setup_dry_run_and_failure_count(monkeypatch, capsys):
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **_kw: [("good", lambda: None), ("bad", lambda: (_ for _ in ()).throw(OSError("nope")))])
    assert tmsetup.run_setup(dry_run=True) == 0
    assert "would: good" in capsys.readouterr().out
    assert tmsetup.run_setup() == 1
    out = capsys.readouterr().out
    assert "✓ good" in out and "✗ bad: nope" in out


def test_setup_steps_include_ui_only_on_request(monkeypatch):
    names = [n for n, _ in tmsetup.setup_steps(ui=False)]
    assert names == [tmsetup.STATE_DIR_STEP, "config file", tmsetup.HOOKS_STEP, "kalmux symlink",
                     "kmux alias symlink (skipped if the path is not ours)",
                     "tm alias symlink (skipped if the path is not ours)",
                     "iTerm2 prefs", "tmux.conf block", tmsetup.STATUSLINE_STEP]
    assert f"state directory (migrates {Path.home() / '.local/state/kmux'})" == tmsetup.STATE_DIR_STEP
    if os.uname().sysname == "Darwin":
        steps = dict(tmsetup.setup_steps(ui=True))
        assert len(steps) == 12
        monkeypatch.setattr(tmsetup, "start_server", lambda tm: False)
        try:
            steps[f"ui server on {tmsetup.UI_URL}"]()
        except OSError as exc:
            assert "did not answer" in str(exc)
        else:
            raise AssertionError("expected OSError")
        monkeypatch.setattr(tmsetup, "register_toolbelt", lambda url, show=True: (False, "no cookie"))
        try:
            steps[f"iTerm2 toolbelt tool {tmsetup.TOOLBELT_NAME!r}"]()
        except OSError as exc:
            assert "no cookie" in str(exc)
        else:
            raise AssertionError("expected OSError")


# ---------- config file + status-line wrapper ----------
def test_setup_steps_place_the_statusline_wrapper_after_the_ui_server(tmp_path, monkeypatch):
    done = []
    monkeypatch.setattr(tmsetup.tmstatusline, "install", lambda settings, tm_link, state: done.append((settings, tm_link, state)))
    monkeypatch.setattr(tmsetup.tmconfig, "ensure_config", lambda: done.append("config"))
    link = tmp_path / "kalmux"
    link.write_text("#!/bin/sh\n")
    link.chmod(0o755)
    steps = tmsetup.setup_steps(ui=True, tm_link=link, settings_path=tmp_path / "settings.json")
    names = [n for n, _ in steps]
    # the state dir moves FIRST: the config file, the saved status line and the pidfile all live inside it
    assert names[:2] == [tmsetup.STATE_DIR_STEP, "config file"]
    assert names.index(tmsetup.STATUSLINE_STEP) == len(names) - (2 if os.uname().sysname == "Darwin" else 1)
    if os.uname().sysname == "Darwin":
        assert names[names.index(tmsetup.STATUSLINE_STEP) - 1] == f"ui server on {tmsetup.UI_URL}"
    dict(steps)[tmsetup.STATUSLINE_STEP]()
    dict(steps)["config file"]()
    assert done == [(tmp_path / "settings.json", link, tmsetup.STATE_DIR), "config"]
    # --no-statusline leaves the user's status line alone
    assert tmsetup.STATUSLINE_STEP not in [n for n, _ in tmsetup.setup_steps(ui=False, statusline=False)]


def test_state_dir_derives_the_status_and_trace_dirs():
    assert tmsetup.STATUS_DIR == tmsetup.STATE_DIR / "status" and tmsetup.TRACE_DIR == tmsetup.STATE_DIR / "trace"
    assert tmsetup.state_dir({"KALMUX_STATE_DIR": "/tmp/elsewhere"}) == Path("/tmp/elsewhere")
    assert tmsetup.state_dir({"HOME": "/Users/x"}) == Path("/Users/x/.local/state/kalmux")
    assert (Path.home() / ".local/state/kmux", Path.home() / ".local/state/tm") == tmsetup.LEGACY_STATE_DIRS


def test_doctor_checks_report_the_status_line_tap(tmp_path):
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state"}
    good = {c["name"]: c for c in tmsetup.doctor_checks(statusline_status=lambda: {
        "routed": True, "command": "/opt/kalmux statusline", "saved_command": "bun hud.ts", "fresh": 3}, **common)}
    assert good["statusLine routed through kalmux"]["ok"] and "bun hud.ts" in good["statusLine routed through kalmux"]["info"]
    assert good["status files fresh"]["ok"] and "3" in good["status files fresh"]["info"]
    bad = {c["name"]: c for c in tmsetup.doctor_checks(statusline_status=lambda: {
        "routed": False, "command": "bun hud.ts", "saved_command": "", "saved_present": False, "fresh": 0}, **common)}
    assert bad["statusLine routed through kalmux"]["ok"] is False
    assert "kalmux statusline install" in bad["statusLine routed through kalmux"]["info"]
    # "status files fresh" is a gauge, not a gate: a machine no Claude session has refreshed yet is healthy
    assert bad["status files fresh"]["ok"] is True and "0 file(s)" in bad["status files fresh"]["info"]
    assert "normal" in bad["status files fresh"]["info"]
    # without injection it reads the real module: a missing settings file is simply "not routed"
    live = {c["name"]: c for c in tmsetup.doctor_checks(**common)}
    assert live["statusLine routed through kalmux"]["ok"] is False


def test_doctor_flags_a_routed_status_line_whose_saved_original_is_gone(tmp_path):
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state"}
    lost = {c["name"]: c for c in tmsetup.doctor_checks(statusline_status=lambda: {
        "routed": True, "command": "/opt/kalmux statusline", "saved_command": "", "saved_present": False,
        "fresh": 0}, **common)}
    check = lost["statusLine routed through kalmux"]
    assert check["ok"] is False and "statusline.json" in check["info"]


def test_doctor_checks_the_interpreter_baked_into_the_status_line(tmp_path):
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state"}
    name = "statusLine interpreter exists"
    base = {"routed": True, "saved_command": "bun hud.ts", "saved_present": True, "fresh": 1}

    def check(interpreter, routed=True):
        status = {**base, "routed": routed, "command": f"{interpreter} /opt/kalmux statusline", "interpreter": interpreter}
        return {c["name"]: c for c in tmsetup.doctor_checks(statusline_status=lambda: status, **common)}.get(name)

    present = check(sys.executable)
    assert present["ok"] is True and sys.executable in present["info"]
    gone = check(str(tmp_path / "python3.13-uninstalled"))
    assert gone["ok"] is False and "kalmux setup" in gone["info"] and "python3.13-uninstalled" in gone["info"]
    assert check("") is None and check(sys.executable, routed=False) is None     # bare form / not routed: nothing to check


def test_doctor_skips_both_status_line_checks_when_asked(tmp_path):
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state"}
    called = []
    names = [c["name"] for c in tmsetup.doctor_checks(statusline=False,
                                                      statusline_status=lambda: called.append(1) or {}, **common)]
    assert called == []
    assert "statusLine routed through kalmux" not in names and "status files fresh" not in names


def test_doctor_reports_a_broken_config_file(tmp_path):
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state",
              "statusline": False}
    good = {c["name"]: c for c in tmsetup.doctor_checks(config_loader=lambda: {"_errors": []}, **common)}
    assert good["kalmux config file"]["ok"] is True
    bad = {c["name"]: c for c in tmsetup.doctor_checks(
        config_loader=lambda: {"_errors": ["claude.new: expected a string"]}, **common)}
    assert bad["kalmux config file"]["ok"] is False and "claude.new" in bad["kalmux config file"]["info"]
    # no injection: it reads the real config module and must not raise
    live = {c["name"]: c for c in tmsetup.doctor_checks(**common)}
    assert "kalmux config file" in live


def test_doctor_checks_the_running_server_runs_this_code(tmp_path):
    if os.uname().sysname != "Darwin":
        return
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "autolaunch": lambda: "missing",
              "state_dir": tmp_path / "state", "statusline": False, "config_loader": lambda: {"_errors": []},
              "snapshot_status": lambda: (True, "stub")}      # never the real tmux server from a test
    fresh = {c["name"]: c for c in tmsetup.doctor_checks(
        ui_health=lambda: {"pid": 1, "tmux": "/opt/homebrew/bin/tmux", "version": tmsetup.VERSION}, **common)}
    assert fresh["ui server runs this code"]["ok"] is True and tmsetup.VERSION in fresh["ui server runs this code"]["info"]
    stale = {c["name"]: c for c in tmsetup.doctor_checks(
        ui_health=lambda: {"pid": 1, "tmux": "/opt/homebrew/bin/tmux", "version": "0.0.1"}, **common)}
    check = stale["ui server runs this code"]
    assert check["ok"] is False and "0.0.1" in check["info"] and "kalmux ui restart" in check["info"]


def test_doctor_reports_the_session_snapshot_of_the_running_server(tmp_path, monkeypatch):
    """The snapshot is what brings the sessions back after a reboot: if it quietly stops being written,
    nothing else notices until the next reboot has already lost them."""
    if os.uname().sysname != "Darwin":
        return
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "autolaunch": lambda: "missing",
              "state_dir": tmp_path / "state", "statusline": False, "config_loader": lambda: {"_errors": []},
              "ui_health": lambda: {"pid": 1, "tmux": "/opt/homebrew/bin/tmux", "version": tmsetup.VERSION}}
    name = "session snapshot (restore after a crash or reboot)"
    green = {c["name"]: c for c in tmsetup.doctor_checks(snapshot_status=lambda: (True, "2 session(s), 3s old"), **common)}
    assert green[name]["ok"] is True and "2 session(s)" in green[name]["info"]
    red = {c["name"]: c for c in tmsetup.doctor_checks(snapshot_status=lambda: (False, "sessions.json missing"), **common)}
    assert red[name]["ok"] is False and "missing" in red[name]["info"]
    # a server that does not answer is already reported on its own line: no snapshot check at all
    down = {c["name"] for c in tmsetup.doctor_checks(**{**common, "ui_health": lambda: None})}
    assert name not in down
    # without injection the default is wired, and it reads the state dir it was given — never ~/.local/state
    # and never the user's tmux server (that is why the real function is stubbed out here, not called)
    asked = []
    monkeypatch.setattr(tmsetup, "check_session_snapshot", lambda f: asked.append(f) or (True, "stub"))
    live = {c["name"]: c for c in tmsetup.doctor_checks(**common)}
    assert live[name]["info"] == "stub" and asked == [tmp_path / "state" / "sessions.json"]


def snapshot_tmux(*names):
    """A FakeTmux holding one pane per session name, the way tmrestore reads it."""
    return FakeTmux(panes=[pane(n, pane_id=f"%{i}", path=f"/Volumes/Dev/{n}") for i, n in enumerate(names)])


def write_sessions(path, boot, *names):
    sessions = tuple(tmsnapshot.SavedSession(name=n, cwd="/tmp", created=1000 + i) for i, n in enumerate(names))
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=boot, saved_at=1, sessions=sessions))
    return path


def test_check_session_snapshot_reads_the_file_against_the_live_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 7)
    path = write_sessions(tmp_path / "sessions.json", 7, "api", "web")
    slept = []
    ok, info = tmsetup.check_session_snapshot(path, tmux=snapshot_tmux("web", "api"), sleep=slept.append)
    assert ok is True and "2 session(s)" in info and slept == []


def test_check_session_snapshot_looks_again_before_calling_the_write_lag_a_drift(tmp_path, monkeypatch):
    """`kalmux new foo` talks to tmux directly, so the file lags up to one keeper round: a doctor run in
    that window must not turn red (and make `kalmux doctor` exit 1) over a snapshot that is simply next."""
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 7)
    path = write_sessions(tmp_path / "sessions.json", 7, "api")
    tmux = snapshot_tmux("api", "foo")
    slept = []

    def keeper_round(seconds):
        slept.append(seconds)
        write_sessions(path, 7, "api", "foo")                # the keeper's next tick catches up

    ok, info = tmsetup.check_session_snapshot(path, tmux=tmux, sleep=keeper_round)
    assert ok is True and slept == [tmrestore.SNAPSHOT_EVERY + 1]
    assert "out of step" not in info


def test_check_session_snapshot_still_reports_a_drift_that_does_not_heal(tmp_path, monkeypatch):
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 7)
    path = write_sessions(tmp_path / "sessions.json", 7, "api")
    slept = []
    ok, info = tmsetup.check_session_snapshot(path, tmux=snapshot_tmux("api", "foo"), sleep=slept.append)
    assert ok is False and "foo" in info and len(slept) == 1


def test_check_session_snapshot_reports_a_list_that_belongs_to_a_server_that_is_gone(tmp_path, monkeypatch):
    """The names can agree and the file can be seconds old and the restore still be outstanding."""
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 7)
    path = tmp_path / "sessions.json"
    tmsnapshot.write_snapshot(path, tmsnapshot.Snapshot(boot=7, saved_at=1, server=tmsnapshot.ServerId(pid=11, started=22),
                                                      sessions=(tmsnapshot.SavedSession(name="api", cwd="/tmp"),)))
    slept = []
    ok, info = tmsetup.check_session_snapshot(path, tmux=snapshot_tmux("api"), sleep=slept.append)
    assert ok is False and "restore pending" in info and slept == []


def test_check_session_snapshot_does_not_wait_for_a_file_that_is_simply_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tmrestore, "boot_time", lambda: 7)
    slept = []
    ok, info = tmsetup.check_session_snapshot(tmp_path / "none.json", tmux=snapshot_tmux("api"), sleep=slept.append)
    assert ok is False and "missing" in info and slept == []


def test_the_session_files_live_in_the_state_dir():
    assert tmsetup.SESSIONS_FILE == tmsetup.STATE_DIR / "sessions.json"
    assert tmsetup.PREVIOUS_SESSIONS_FILE == tmsetup.STATE_DIR / "sessions.previous.json"


def test_the_statusline_step_bakes_an_absolute_interpreter_for_a_python_link(tmp_path, monkeypatch):
    """setup run from a clone: the routed command must not depend on the PATH Claude Code runs it under."""
    monkeypatch.setattr(tmsetup, "STATE_DIR", tmp_path / "state")
    link = tmp_path / "kalmux"
    link.write_text("#!/usr/bin/env python3\n")
    link.chmod(0o755)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": f"{link} statusline"}}))
    tmsetup._install_statusline(settings, link)
    assert json.loads(settings.read_text())["statusLine"]["command"] == f"{sys.executable} {link} statusline"
    assert not (tmp_path / "state" / "statusline.json").exists()     # the old wrapper was never saved as the original


def test_the_statusline_step_refuses_a_tm_link_that_is_not_executable(tmp_path, monkeypatch):
    installed = []
    monkeypatch.setattr(tmsetup.tmstatusline, "install", lambda *a: installed.append(a))
    link = tmp_path / "kalmux"
    step = dict(tmsetup.setup_steps(ui=False, tm_link=link, settings_path=tmp_path / "settings.json"))[tmsetup.STATUSLINE_STEP]
    for missing in (None, "dangling"):
        if missing == "dangling":
            link.symlink_to(tmp_path / "nowhere")
        try:
            step()
        except OSError as exc:
            assert "kalmux symlink" in str(exc) and str(link) in str(exc)
        else:
            raise AssertionError("expected OSError")
        assert installed == []
        if link.is_symlink():
            link.unlink()
    real = tmp_path / "real-kalmux"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    link.symlink_to(real)
    step()
    assert installed == [(tmp_path / "settings.json", link, tmsetup.STATE_DIR)]


def test_symlink_moves_a_regular_file_out_of_the_way(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    assert tmsetup.symlink(target, link) == ""
    assert link.is_symlink() and link.resolve() == target
    # a symlink of someone else's is simply replaced (unchanged behaviour)
    link.unlink()
    link.symlink_to(tmp_path / "elsewhere")
    assert tmsetup.symlink(target, link) == ""
    assert link.resolve() == target
    # a real file is never destroyed
    link.unlink()
    link.write_text("the user's own script")
    note = tmsetup.symlink(target, link)
    assert "bak-kalmux" in note
    assert link.is_symlink() and (tmp_path / "link.bak-kalmux").read_text() == "the user's own script"


def test_run_setup_prints_the_note_a_step_returns(monkeypatch, capsys):
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **_kw: [("step", lambda: "moved x to x.bak-kalmux")])
    assert tmsetup.run_setup() == 0
    assert "✓ step (moved x to x.bak-kalmux)" in capsys.readouterr().out


def test_importing_tmsetup_does_not_drag_in_the_server_stack():
    """`kalmux statusline` imports tmsetup for state_dir() on every refresh; tmserver costs ~17 ms it must not pay."""
    import subprocess
    import sys

    src = str(Path(tmsetup.__file__).resolve().parents[1])
    code = ("import sys; import kalmux.tmsetup; "
            "print(sorted(m for m in ('kalmux.tmserver', 'http.client', 'ssl') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=src, check=True)
    assert out.stdout.strip() == "[]"


# ---------- the kmux -> kalmux rename ----------
def test_migrate_state_dir_moves_the_legacy_directory(tmp_path):
    """status/, trace/, statusline.json, ui.pid and ui.log all come across, and the empty shell is removed."""
    new, legacy = tmp_path / "kalmux", tmp_path / "kmux"
    (legacy / "status").mkdir(parents=True)
    (legacy / "status" / "s.json").write_text("{}")
    (legacy / "statusline.json").write_text('{"command": "bun hud.ts"}')
    note = tmsetup.migrate_state_dir(new, (legacy,))
    assert str(legacy) in note and str(new) in note
    assert not legacy.exists()
    assert (new / "status" / "s.json").read_text() == "{}"
    assert json.loads((new / "statusline.json").read_text())["command"] == "bun hud.ts"
    assert tmsetup.migrate_state_dir(new, (legacy,)) == ""              # idempotent: nothing left to move


def test_migrate_state_dir_fills_a_directory_another_command_already_created(tmp_path):
    """`kalmux ui stop` (or the statusLine tap) creates the new dir before setup runs: migrate anyway.

    Renaming the directory would refuse here and leave statusline.json behind under the old name, which
    silently replaces the user's own status line with kalmux's default one-liner.
    """
    new, legacy = tmp_path / "kalmux", tmp_path / "kmux"
    (new / "status").mkdir(parents=True)                                # what `kalmux ui stop` leaves behind
    (new / "ui.pid").write_text("123")
    (legacy / "status").mkdir(parents=True)
    (legacy / "status" / "old.json").write_text("{}")
    (legacy / "trace").mkdir()
    (legacy / "trace" / "t.jsonl").write_text("{}\n")
    (legacy / "statusline.json").write_text('{"command": "bun hud.ts"}')
    note = tmsetup.migrate_state_dir(new, (legacy,))
    assert str(legacy) in note
    assert json.loads((new / "statusline.json").read_text())["command"] == "bun hud.ts"
    assert (new / "status" / "old.json").exists() and (new / "trace" / "t.jsonl").exists()
    assert (new / "ui.pid").read_text() == "123"                        # the newer file is left alone
    assert not legacy.exists()


def test_migrate_state_dir_never_overwrites_and_never_deletes(tmp_path):
    new, legacy = tmp_path / "kalmux", tmp_path / "kmux"
    (new / "status").mkdir(parents=True)
    (new / "statusline.json").write_text('{"command": "new"}')
    (new / "status" / "s.json").write_text("new")
    (legacy / "status").mkdir(parents=True)
    (legacy / "statusline.json").write_text('{"command": "old"}')
    (legacy / "status" / "s.json").write_text("old")
    tmsetup.migrate_state_dir(new, (legacy,))
    assert json.loads((new / "statusline.json").read_text())["command"] == "new"
    assert (new / "status" / "s.json").read_text() == "new"
    assert (legacy / "statusline.json").exists()                        # the loser stays for the user to see
    assert (legacy / "status" / "s.json").read_text() == "old"
    assert tmsetup.migrate_state_dir(tmp_path / "fresh", (tmp_path / "gone",)) == ""   # nothing to migrate
    # a machine that never saw the kmux release still has the tm one: every legacy dir is drained
    tm_dir = tmp_path / "tm"
    tm_dir.mkdir()
    (tm_dir / "ui.log").write_text("old log")
    note = tmsetup.migrate_state_dir(tmp_path / "fresh", (tmp_path / "gone", tm_dir))
    assert str(tm_dir) in note and (tmp_path / "fresh" / "ui.log").read_text() == "old log"


def test_autolaunch_state_recognises_a_script_baked_before_the_rename(tmp_path, monkeypatch):
    """`kmux setup` baked '.../kmux ui start' into AutoLaunch.scpt: kalmux must own (and rewrite) it."""
    script, stamp = tmp_path / "AutoLaunch.scpt", tmp_path / "autolaunch.sha256"
    script.write_text("compiled")
    old = ("do shell script \"PATH='/opt/homebrew/bin':$PATH '/opt/homebrew/bin/python3.13' "
           "'/Users/k/.local/bin/kmux' ui start >/dev/null 2>&1 &\"")
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **kw: (0, old))
    assert tmsetup.autolaunch_state(script, stamp) == "ours"        # stamp lost: the marker still identifies it
    stamp.write_text(tmsetup._sha256(script))
    assert tmsetup.autolaunch_state(script, stamp) == "ours"        # stamp carried over by the state-dir migration
    monkeypatch.setattr(tmsetup, "_run", lambda cmd, **kw: (0, 'display dialog "not ours"'))
    stamp.write_text("stale")
    assert tmsetup.autolaunch_state(script, stamp) == "foreign"


# ---------- which file the `kalmux` command is, in each way it can be installed ----------
def test_cli_path_prefers_the_name_it_was_invoked_as(tmp_path, monkeypatch):
    """A Homebrew keg lives under a versioned Cellar path that `brew upgrade` replaces; the name on PATH
    does not move. Baking the versioned one into ~/.tmux.conf and AutoLaunch would dangle after an upgrade."""
    stable = tmp_path / "bin" / "kalmux"
    stable.parent.mkdir()
    stable.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(tmsetup.sys, "argv", [str(stable), "setup"])
    assert tmsetup.cli_path() == stable

    # a symlink is NOT followed either: Homebrew links bin/kalmux into a Cellar path that an upgrade
    # replaces, and setup_steps refuses to relink a path that already resolves to the same file.
    link = tmp_path / "kalmux"
    link.symlink_to(stable)
    monkeypatch.setattr(tmsetup.sys, "argv", [str(link)])
    assert tmsetup.cli_path() == link

    # invoked as anything else (pytest, python -c), it falls back to the checkout
    monkeypatch.setattr(tmsetup.sys, "argv", ["/usr/bin/pytest"])
    assert tmsetup.cli_path() == tmsetup.SOURCE_CHECKOUT / "bin" / "kalmux"


# ---------- Claude Code hooks: the doctor's view, --fix and the setup step ----------
def test_setup_wires_claude_hooks_instead_of_hijacking_the_cc_status_link():
    names = [name for name, _ in tmsetup.setup_steps(ui=False, statusline=False)]
    assert tmsetup.HOOKS_STEP in names and "hook symlink" not in names
    assert names.index(tmsetup.HOOKS_STEP) < names.index("tmux.conf block")


def test_the_hooks_step_calls_wire_claude_hooks_with_the_stable_wrapper(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(tmsetup.tmhooks, "wire_claude_hooks", lambda *a, **kw: seen.update(args=a, kw=kw) or "done")
    step = dict(tmsetup.setup_steps(ui=False, statusline=False, settings_path=tmp_path / "s.json",
                                    hook_link=tmp_path / "cc-status"))[tmsetup.HOOKS_STEP]
    assert step() == "done"
    assert seen["args"][:2] == (tmp_path / "s.json", tmsetup.WRAPPER) and seen["kw"]["hook_link"] == tmp_path / "cc-status"
    assert "Cellar" not in str(tmsetup.WRAPPER) and tmsetup.WRAPPER.name == "kalmux-hook"


def test_doctor_flags_the_entries_in_the_wrong_mode(tmp_path):
    settings, link, wrapper = kalmux_wired(tmp_path, mode="kalmux")
    common = {"settings_path": settings, "hook_link": link, "wrapper": wrapper, "tmux_conf": tmp_path / "c",
              "defaults_reader": lambda key: "", "ui": False, "statusline": False, "state_dir": tmp_path / "state",
              "config_loader": lambda: {"_errors": []}, "registry_loader": lambda: {}}
    native = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 6), **common)}
    assert native[HOOKS_OK]["ok"] is False and "iterm2" in native[HOOKS_OK]["info"]
    old = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 4), **common)}
    assert old[HOOKS_OK]["ok"] is True


ITERM_NATIVE = "iTerm2 3.7.5+ (reports tmux panes itself)"


def test_doctor_flags_an_iterm2_whose_own_hooks_misreport_tmux_panes(tmp_path):
    """Before 3.7.5 iTerm2's cc-status, now running next to Kalmux's entry, reports every tmux pane to one
    hidden tab: nothing Kalmux writes can undo that, so the doctor asks for the upgrade."""
    settings, link, wrapper = kalmux_wired(tmp_path, mode="kalmux")
    common = {"settings_path": settings, "hook_link": link, "wrapper": wrapper, "tmux_conf": tmp_path / "c",
              "defaults_reader": lambda key: "", "ui": False, "statusline": False, "state_dir": tmp_path / "state",
              "config_loader": lambda: {"_errors": []}, "registry_loader": lambda: {}}
    old = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 3), **common)}
    assert old[ITERM_NATIVE]["ok"] is False and "3.7.3" in old[ITERM_NATIVE]["info"]
    assert "upgrade iTerm2" in old[ITERM_NATIVE]["remedy"] and not old[ITERM_NATIVE]["fix"]
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, "iterm2"))
    new = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 6), **common)}
    assert new[ITERM_NATIVE]["ok"] is True
    unknown = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: None, **common)}
    assert ITERM_NATIVE not in unknown
    settings.write_text(json.dumps({"hooks": {}}))                      # no iTerm2 hooks: nothing runs in parallel
    bare = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 3), **common)}
    assert ITERM_NATIVE not in bare
    settings.write_text(json.dumps({"hooks": []}))                      # unreadable hooks: the hooks line says so
    broken = {c["name"]: c for c in tmsetup.doctor_checks(iterm_version=lambda: (3, 7, 3), **common)}
    assert ITERM_NATIVE not in broken and broken[HOOKS_OK]["ok"] is False


def test_doctor_fix_never_routes_a_status_line_that_is_not_routed(tmp_path):
    """`setup --no-statusline` is not recorded anywhere, so --fix may only re-bake a command that is ours."""
    common = {"settings_path": tmp_path / "settings.json", "hook_link": tmp_path / "l", "wrapper": tmp_path / "w",
              "tmux_conf": tmp_path / "c", "defaults_reader": lambda key: "", "ui_health": lambda: None,
              "autolaunch": lambda: "missing", "ui": False, "state_dir": tmp_path / "state"}
    report = tmsetup.doctor_checks(statusline_status=lambda: {
        "routed": False, "command": "bun /x/hud.ts", "saved_command": "", "saved_present": False, "fresh": 0}, **common)
    check = {c["name"]: c for c in report}["statusLine routed through kalmux"]
    assert check["ok"] is False and not check.get("fix") and "kalmux statusline install" in check["remedy"]
    assert tmsetup.STATUSLINE_STEP not in tmsetup.fix_plan(report)


def test_doctor_flags_a_cc_status_link_kalmux_still_holds(tmp_path):
    settings, link, wrapper = kalmux_wired(tmp_path)
    link.unlink()
    link.symlink_to(tmp_path / "old" / "cc-status-tmux")
    report = {c["name"]: c for c in tmsetup.doctor_checks(
        settings_path=settings, hook_link=link, wrapper=wrapper, tmux_conf=tmp_path / "c", defaults_reader=lambda key: "",
        ui=False, statusline=False, state_dir=tmp_path / "state", config_loader=lambda: {"_errors": []},
        registry_loader=lambda: {}, iterm_version=lambda: None)}
    check = report["iTerm2 owns ~/.config/iterm2/cc-status"]
    assert check["ok"] is False and check["fix"] == tmsetup.HOOKS_STEP


def test_doctor_reports_when_hooks_stop_reaching_kalmux(tmp_path):
    import time as _time
    settings, link, wrapper = kalmux_wired(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    hb = state / tmhooks.HEARTBEAT_NAME
    hb.write_text("")
    now = int(_time.time())
    os.utime(hb, (now - 3600, now - 3600))
    common = {"settings_path": settings, "hook_link": link, "wrapper": wrapper, "tmux_conf": tmp_path / "c",
              "defaults_reader": lambda key: "", "ui": False, "statusline": False, "state_dir": state,
              "config_loader": lambda: {"_errors": []}, "iterm_version": lambda: None}
    alive = {"reels:@1.%1": {"alive": True, "statusUpdatedAt": (now - 10) * 1000,
                             "sessionId": "0a1b2c3d-0000-4000-8000-00000000abcd"}}
    red = {c["name"]: c for c in tmsetup.doctor_checks(registry_loader=lambda: alive, **common)}
    assert red[REACH]["ok"] is False and "last event 1h00m ago" in red[REACH]["info"]
    assert "not reporting: reels" in red[REACH]["info"]
    assert "restart" in red[REACH]["remedy"] and "still" in red[REACH]["remedy"] and not red[REACH]["fix"]
    tmhooks.uninstall_hooks(settings)
    unwired = {c["name"]: c for c in tmsetup.doctor_checks(registry_loader=lambda: alive, **common)}
    assert unwired[REACH]["fix"] == tmsetup.HOOKS_STEP and "missing" in unwired[REACH]["remedy"]
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, "kalmux"))       # no state_dir: no wired stamp
    green = {c["name"]: c for c in tmsetup.doctor_checks(registry_loader=lambda: {}, **common)}
    assert green[REACH]["ok"] is True and "last event" in green[REACH]["info"]
    hb.unlink()
    never = {c["name"]: c for c in tmsetup.doctor_checks(registry_loader=lambda: {}, **common)}
    assert never[REACH]["ok"] is True and "no hook event yet" in never[REACH]["info"]


def test_fix_plan_lists_only_safe_idempotent_steps_in_setup_order():
    checks = [{"name": "a", "ok": False, "info": "", "remedy": "x", "fix": "tmux.conf block"},
              {"name": "b", "ok": False, "info": "", "remedy": "y", "fix": tmsetup.HOOKS_STEP},
              {"name": "c", "ok": False, "info": "", "remedy": "y", "fix": tmsetup.HOOKS_STEP},
              {"name": "d", "ok": True, "info": "", "remedy": "z", "fix": "kalmux symlink"},
              {"name": "e", "ok": False, "info": "", "remedy": "kalmux ui install"}]
    assert tmsetup.fix_plan(checks) == [tmsetup.HOOKS_STEP, "tmux.conf block"]
    assert "iTerm2 prefs" not in tmsetup.SAFE_FIX_STEPS and tmsetup.STATUSLINE_STEP in tmsetup.SAFE_FIX_STEPS


def test_run_fix_runs_the_plan_and_lists_what_needs_a_human(monkeypatch):
    ran, out = [], []
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **kw: [
        (tmsetup.HOOKS_STEP, lambda: ran.append("hooks") or "wired"), ("tmux.conf block", lambda: ran.append("conf")),
        ("iTerm2 prefs", lambda: ran.append("PREFS"))])
    checks = [{"name": "hooks", "ok": False, "info": "", "remedy": "kalmux doctor --fix", "fix": tmsetup.HOOKS_STEP},
              {"name": "prefs", "ok": False, "info": "", "remedy": "kalmux setup (writes iTerm2 preferences)"}]
    assert tmsetup.run_fix(checks, printer=out.append) == 0
    assert ran == ["hooks"]
    text = "\n".join(out)
    assert "will run: " + tmsetup.HOOKS_STEP in text and "✓ " + tmsetup.HOOKS_STEP + " (wired)" in text
    assert "prefs" in text and "kalmux setup (writes iTerm2 preferences)" in text


def test_run_fix_with_nothing_to_do(monkeypatch):
    out = []
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **kw: [])
    assert tmsetup.run_fix([{"name": "x", "ok": True, "info": "", "remedy": ""}], printer=out.append) == 0
    assert "nothing" in out[0]


def test_run_fix_counts_a_failing_step(monkeypatch):
    monkeypatch.setattr(tmsetup, "setup_steps", lambda **kw: [(tmsetup.HOOKS_STEP, lambda: (_ for _ in ()).throw(OSError("no")))])
    out = []
    checks = [{"name": "h", "ok": False, "info": "", "remedy": "r", "fix": tmsetup.HOOKS_STEP}]
    assert tmsetup.run_fix(checks, printer=out.append) == 1 and any("✗" in line and "no" in line for line in out)


def test_doctor_reports_an_unreadable_settings_file_on_the_hooks_line(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    report = {c["name"]: c for c in tmsetup.doctor_checks(
        settings_path=settings, hook_link=tmp_path / "l", wrapper=tmp_path / "w", tmux_conf=tmp_path / "c",
        defaults_reader=lambda key: "", ui=False, statusline=False, state_dir=tmp_path / "state",
        config_loader=lambda: {"_errors": []}, registry_loader=lambda: {}, iterm_version=lambda: None)}
    assert report[HOOKS_OK]["ok"] is False and "cannot read" in report[HOOKS_OK]["info"]
