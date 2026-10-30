"""The statusLine command carries an absolute interpreter, and the tap never lets the status line go blank."""
import io
import json
import sys
from pathlib import Path

import pytest

from kalmux import tmstatusline

PY = "/opt/homebrew/opt/python@3.13/bin/python3.13"
PAYLOAD = {"session_id": "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d", "model": {"display_name": "Opus 5"},
           "context_window": {"used_percentage": 42.4}}


def settings_with(tmp_path: Path, statusline) -> Path:
    path = tmp_path / "settings.json"
    body = {"env": {"A": "b"}}
    if statusline is not None:
        body["statusLine"] = statusline
    path.write_text(json.dumps(body))
    return path


def command_in(settings: Path) -> str:
    return json.loads(settings.read_text())["statusLine"]["command"]


def save_original(state_dir: Path, command: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "statusline.json").write_text(json.dumps({"command": command, "type": "command", "saved_at": 1}))


# ---------- the command ----------
def test_wrapper_command_bakes_the_interpreter_when_given_one():
    link = Path("/Users/x/.local/bin/kalmux")
    assert tmstatusline.wrapper_command(link) == f"{link} statusline"
    assert tmstatusline.wrapper_command(link, PY) == f"{PY} {link} statusline"
    assert tmstatusline.wrapper_command(Path("/a b/kalmux"), "/c d/python3") == "'/c d/python3' '/a b/kalmux' statusline"


def test_is_ours_recognises_the_interpreter_form_but_not_look_alikes():
    for good in (f"{PY} /Users/x/.local/bin/kalmux statusline", "'/c d/python3' '/a b/kmux' statusline",
                 "/usr/bin/python3 /opt/tm statusline"):
        assert tmstatusline.is_ours(good) is True
    for bad in ("bun /Users/x/.local/bin/kalmux statusline", f"{PY} /Users/x/hud.py statusline",
                f"{PY} /Users/x/.local/bin/kalmux ls", f"{PY} -u /opt/kalmux statusline"):
        assert tmstatusline.is_ours(bad) is False
    assert tmstatusline.baked_interpreter(f"{PY} /opt/kalmux statusline") == PY
    assert tmstatusline.baked_interpreter("/opt/kalmux statusline") == ""
    assert tmstatusline.baked_interpreter(None) == ""


def test_interpreter_for_only_applies_to_python_scripts(tmp_path):
    py_script = tmp_path / "kalmux"
    py_script.write_text("#!/usr/bin/env python3\n")
    sh_script = tmp_path / "wrapper"
    sh_script.write_text("#!/bin/bash\nexec python x\n")
    binary = tmp_path / "blob"
    binary.write_bytes(b"\xcf\xfa\xed\xfe\x00")
    assert tmstatusline.interpreter_for(py_script) == sys.executable
    for other in (sh_script, binary, tmp_path / "missing"):
        assert tmstatusline.interpreter_for(other) == ""


# ---------- install / upgrade ----------
def test_install_bakes_the_interpreter_and_is_idempotent(tmp_path):
    state, link = tmp_path / "state", Path("/Users/x/.local/bin/kalmux")
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts", "padding": 0})
    assert tmstatusline.install(settings, link, state, python=PY) is True
    assert json.loads(settings.read_text())["statusLine"] == {
        "type": "command", "command": f"{PY} {link} statusline", "padding": 0}
    assert tmstatusline.install(settings, link, state, python=PY) is False
    assert tmstatusline.saved_command(state) == "bun hud.ts"
    assert tmstatusline.uninstall(settings, state)[0] is True
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": "bun hud.ts", "padding": 0}


def test_install_upgrades_an_older_routed_command_without_touching_the_saved_original(tmp_path):
    state, link = tmp_path / "state", Path("/Users/x/.local/bin/kalmux")
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts"})
    tmstatusline.install(settings, link, state, python="")                    # the 0.5.1 form: bare link
    assert command_in(settings) == f"{link} statusline"
    assert tmstatusline.install(settings, link, state, python=PY) is True     # re-running setup upgrades it
    assert command_in(settings) == f"{PY} {link} statusline"
    assert tmstatusline.saved_command(state) == "bun hud.ts"                  # never the wrapper itself
    assert tmstatusline.install(settings, link, state, python="/opt/homebrew/bin/python3.14") is True
    assert tmstatusline.saved_command(state) == "bun hud.ts"
    assert tmstatusline.install(settings, link, state, python="") is True     # and the bare form can come back
    assert command_in(settings) == f"{link} statusline"


def test_install_picks_the_interpreter_from_the_link_when_none_is_given(tmp_path):
    link = tmp_path / "kalmux"
    link.write_text("#!/usr/bin/env python3\n")
    settings = settings_with(tmp_path, None)
    tmstatusline.install(settings, link, tmp_path / "state")
    assert command_in(settings) == f"{sys.executable} {link} statusline"
    assert tmstatusline.status(settings, tmp_path / "state")["interpreter"] == sys.executable


def python_script_link(tmp_path: Path) -> Path:
    link = tmp_path / "kalmux"
    link.write_text("#!/usr/bin/env python3\n")
    return link


def test_install_keeps_a_baked_interpreter_that_still_works(tmp_path):
    """Setup run under another python (an SSH shell, the shim's re-exec) must not flip the command back and
    forth, nor rewrite settings.json each time."""
    link = python_script_link(tmp_path)
    other = tmp_path / "bin" / "python3.99"
    other.parent.mkdir()
    other.symlink_to(sys.executable)
    settings = settings_with(tmp_path, {"type": "command", "command": f"{other} {link} statusline"})
    before = settings.read_text()
    assert tmstatusline.install(settings, link, tmp_path / "state") is False
    assert settings.read_text() == before


@pytest.mark.parametrize("baked", ["missing", "too-old"])
def test_install_rebakes_an_interpreter_that_is_gone_or_too_old(tmp_path, baked):
    link = python_script_link(tmp_path)
    old = tmp_path / "bin" / "python3.9"
    old.parent.mkdir()
    if baked == "too-old":
        old.write_text("#!/bin/sh\nexit 1\n")
        old.chmod(0o755)
    settings = settings_with(tmp_path, {"type": "command", "command": f"{old} {link} statusline"})
    assert tmstatusline.install(settings, link, tmp_path / "state") is True
    assert command_in(settings) == f"{sys.executable} {link} statusline"


def test_rebaking_never_overwrites_the_backup_of_the_users_settings(tmp_path):
    link = python_script_link(tmp_path)
    settings = settings_with(tmp_path, {"type": "command", "command": "bun /x/hud.ts"})
    backup = Path(str(settings) + tmstatusline.BACKUP_SUFFIX)
    tmstatusline.install(settings, link, tmp_path / "state", python="")         # the bare 0.5.x form
    assert "bun /x/hud.ts" in backup.read_text()
    assert tmstatusline.install(settings, link, tmp_path / "state") is True     # baked now
    assert tmstatusline.install(settings, link, tmp_path / "state", python=PY) is True
    assert "bun /x/hud.ts" in backup.read_text() and "statusline" not in backup.read_text()


def test_the_self_reference_guard_sees_the_interpreter_form(tmp_path, capsys):
    state = tmp_path / "state"
    save_original(state, f"{PY} /opt/kalmux statusline")
    assert tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode())) == 0
    assert "kalmux wrapper itself" in capsys.readouterr().err


# ---------- the status line never goes blank because of kalmux ----------
@pytest.mark.parametrize("broken", ["normalize", "write_status", "prune_status"])
def test_run_tap_passes_the_bytes_through_whatever_raises_inside(tmp_path, monkeypatch, broken):
    state, sink = tmp_path / "state", tmp_path / "sink"
    save_original(state, f"cat > {sink}; exit 4")
    monkeypatch.setattr(tmstatusline, "_prune_due", lambda *a: True)

    def boom(*args, **kwargs):
        raise RuntimeError("kalmux bug")

    monkeypatch.setattr(tmstatusline, broken, boom)
    data = json.dumps(PAYLOAD).encode()
    assert tmstatusline.run_tap(state, stdin=io.BytesIO(data)) == 4
    assert sink.read_bytes() == data


def test_run_tap_with_an_unreadable_saved_file_prints_the_default_line(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("kalmux bug")

    monkeypatch.setattr(tmstatusline, "saved_command", boom)
    assert tmstatusline.run_tap(tmp_path / "state", stdin=io.BytesIO(json.dumps(PAYLOAD).encode())) == 0
    assert capsys.readouterr().out.strip() == "Opus 5 · ctx 42%"


def test_passthrough_survives_a_command_the_os_refuses(capsys):
    assert tmstatusline.passthrough(b"{}", "echo\x00hi", None) == 0           # embedded NUL: ValueError, not OSError
    assert capsys.readouterr().out.strip() == "kalmux"
