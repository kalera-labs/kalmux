"""Tests for lib/tmstatusline.py: the `kalmux statusline` tap, and install/uninstall/status of the wrapper."""
import io
import json
import os
import time
from pathlib import Path

import pytest

from kalmux import tmstatusline

PAYLOAD = {
    "session_id": "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d",
    "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
    "workspace": {"current_dir": "/Volumes/Dev/api", "git_worktree": "/Volumes/Dev/api"},
    "cost": {"total_cost_usd": 1.2345678, "total_duration_ms": 1000},
    "context_window": {"used_percentage": 42.4, "context_window_size": 200000, "current_usage": 84000,
                       "total_input_tokens": 70000},
    "effort": {"level": "high"},
    "transcript_path": "/Users/x/.claude/projects/p/s.jsonl",
    "version": "2.1.270",
}


def read_status(state_dir: Path, session_id: str) -> dict:
    return json.loads((state_dir / "status" / f"{session_id}.json").read_text())


def save_original(state_dir: Path, command: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "statusline.json").write_text(json.dumps({"command": command, "type": "command", "saved_at": 1}))


# ---------- normalize ----------
def test_normalize_maps_the_documented_payload():
    rec = tmstatusline.normalize(PAYLOAD, now=1789330000)
    assert rec == {"ts": 1789330000, "session_id": PAYLOAD["session_id"], "model": "Opus 5",
                   "model_id": "claude-opus-5", "effort": "high", "context_pct": 42, "context_tokens": 84000,
                   "context_size": 200000, "cost_usd": 1.2346, "cwd": "/Volumes/Dev/api",
                   "transcript_path": "/Users/x/.claude/projects/p/s.jsonl", "version": "2.1.270",
                   "rate_limits": None}


def test_normalize_needs_a_uuid_session_id():
    for bad in ({}, {"session_id": ""}, {"session_id": "nope"}, {"session_id": 7}, [], "x", None):
        assert tmstatusline.normalize(bad) is None


def test_normalize_turns_every_missing_or_wrong_typed_field_into_null():
    rec = tmstatusline.normalize({"session_id": PAYLOAD["session_id"], "model": "Opus 5", "cost": 3,
                                  "context_window": {"used_percentage": None}, "effort": "medium"}, now=5)
    assert rec["model"] is None and rec["model_id"] is None and rec["cost_usd"] is None
    assert rec["context_pct"] is None and rec["context_tokens"] is None and rec["context_size"] is None
    assert rec["cwd"] is None and rec["transcript_path"] is None and rec["version"] is None
    assert rec["rate_limits"] is None and rec["effort"] == "medium" and rec["ts"] == 5
    # booleans are not numbers, control characters never reach the file
    weird = tmstatusline.normalize({"session_id": PAYLOAD["session_id"], "context_window": {"used_percentage": True},
                                    "version": "2.1\n270\x07"}, now=5)
    assert weird["context_pct"] is None and weird["version"] == "2.1 270"


def test_normalize_reads_rate_limits_in_both_time_shapes():
    raw = {"five_hour": {"used_percentage": 35, "resets_at": 1789340000},
           "seven_day": {"used_percentage": 12.6, "resets_at": "2026-09-14T10:00:00+00:00"},
           "spend_limit": {"used_percentage": None, "resets_at": None},
           "junk": {"used_percentage": 1}}
    rec = tmstatusline.normalize({**PAYLOAD, "rate_limits": raw})
    assert rec["rate_limits"]["five_hour"] == {"used_pct": 35, "resets_at": 1789340000}
    assert rec["rate_limits"]["seven_day"] == {"used_pct": 13, "resets_at": 1789380000}
    assert "spend_limit" not in rec["rate_limits"] and "junk" not in rec["rate_limits"]
    assert tmstatusline.normalize({**PAYLOAD, "rate_limits": {"five_hour": "n/a"}})["rate_limits"] is None


# ---------- the tap ----------
def test_run_tap_writes_the_status_file_and_feeds_the_saved_command(tmp_path):
    state = tmp_path / "state"
    sink = tmp_path / "sink"
    save_original(state, f"cat > {sink}; exit 3")
    data = json.dumps(PAYLOAD).encode()
    rc = tmstatusline.run_tap(state, stdin=io.BytesIO(data), now=1789330000)
    assert rc == 3                                         # the original's exit status is ours
    assert sink.read_bytes() == data                       # the original sees the same bytes
    assert read_status(state, PAYLOAD["session_id"])["context_pct"] == 42
    assert (state / "status").stat().st_mode & 0o777 == 0o700
    assert list((state / "status").glob("*.tmp")) == []


def test_run_tap_prints_the_default_line_when_nothing_is_saved(tmp_path, capsys):
    rc = tmstatusline.run_tap(tmp_path, stdin=io.BytesIO(json.dumps(PAYLOAD).encode()))
    assert rc == 0 and capsys.readouterr().out.strip() == "Opus 5 · ctx 42%"
    payload = {"session_id": PAYLOAD["session_id"]}
    tmstatusline.run_tap(tmp_path, stdin=io.BytesIO(json.dumps(payload).encode()))
    assert capsys.readouterr().out.strip() == "claude"


def test_run_tap_survives_garbage_and_still_passes_through(tmp_path, capsys):
    state = tmp_path / "state"
    sink = tmp_path / "sink"
    save_original(state, f"cat > {sink}")
    for junk in (b"", b"   ", b"{not json", b"\xff\xfe\x00", json.dumps([1, 2]).encode(),
                 json.dumps({"session_id": "nope"}).encode()):
        assert tmstatusline.run_tap(state, stdin=io.BytesIO(junk)) == 0
        assert sink.read_bytes() == junk
    assert not (state / "status").exists()
    # a saved command that cannot run must not take the status line down with it
    save_original(state / "broken", "/nonexistent/binary --flag")
    assert tmstatusline.run_tap(state / "broken", stdin=io.BytesIO(json.dumps(PAYLOAD).encode())) != 0


def test_run_tap_caps_stdin_and_an_unwritable_state_dir_is_not_fatal(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory")
    big = json.dumps(PAYLOAD).encode() + b" " * (2 * 1024 * 1024)
    assert tmstatusline.run_tap(blocked, stdin=io.BytesIO(big)) == 0
    assert capsys.readouterr().out.strip() == "Opus 5 · ctx 42%"
    stream = io.BytesIO(big)
    tmstatusline.run_tap(blocked, stdin=stream)
    assert stream.tell() == tmstatusline.MAX_STDIN            # never reads more than 1 MiB


# ---------- install / uninstall / status ----------
def settings_with(tmp_path: Path, obj) -> Path:
    path = tmp_path / "settings.json"
    body = {"env": {"A": "b"}, "hooks": {"Stop": []}}
    if obj is not None:
        body["statusLine"] = obj
    path.write_text(json.dumps(body, indent=2) + "\n")
    return path


def test_install_saves_the_original_backs_up_and_rewrites_only_status_line(tmp_path):
    state = tmp_path / "state"
    original = {"type": "command", "command": "bun run /Users/x/hud/index.ts", "padding": 0}
    settings = settings_with(tmp_path, original)
    assert tmstatusline.install(settings, Path("/Users/x/.local/bin/kalmux"), state) is True
    written = json.loads(settings.read_text())
    assert written["statusLine"] == {"type": "command", "command": "/Users/x/.local/bin/kalmux statusline", "padding": 0}
    assert written["env"] == {"A": "b"} and written["hooks"] == {"Stop": []}
    assert settings.read_text().endswith("}\n") and '\n  "env"' in settings.read_text()
    saved = json.loads((state / "statusline.json").read_text())
    assert saved["command"] == original["command"] and saved["padding"] == 0 and isinstance(saved["saved_at"], int)
    backup = tmp_path / "settings.json.bak-kalmux"
    assert json.loads(backup.read_text())["statusLine"] == original
    # idempotent, and a second install never overwrites the saved original with our own wrapper
    assert tmstatusline.install(settings, Path("/Users/x/.local/bin/kalmux"), state) is False
    assert json.loads((state / "statusline.json").read_text())["command"] == original["command"]
    assert json.loads(settings.read_text())["statusLine"]["command"] == "/Users/x/.local/bin/kalmux statusline"
    # the user swapped in another status line by hand: routing it again keeps the FIRST saved original
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "starship prompt"}}))
    assert tmstatusline.install(settings, Path("/Users/x/.local/bin/kalmux"), state) is True
    assert json.loads((state / "statusline.json").read_text())["command"] == original["command"]


def test_install_without_a_status_line_or_without_a_settings_file(tmp_path):
    state = tmp_path / "state"
    settings = settings_with(tmp_path, None)
    assert tmstatusline.install(settings, Path("/opt/kalmux"), state) is True
    assert json.loads((state / "statusline.json").read_text())["command"] == ""
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": "/opt/kalmux statusline"}
    fresh = tmp_path / "none" / "settings.json"
    assert tmstatusline.install(fresh, Path("/opt/kalmux"), tmp_path / "state2") is True
    assert json.loads(fresh.read_text()) == {"statusLine": {"type": "command", "command": "/opt/kalmux statusline"}}
    assert not (tmp_path / "none" / "settings.json.bak-kalmux").exists()
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(ValueError):
        tmstatusline.install(bad, Path("/opt/kalmux"), tmp_path / "state3")


def test_is_ours_recognises_the_wrapper_in_its_usual_shapes():
    link = Path("/Users/x/.local/bin/kalmux")
    for good in ("/Users/x/.local/bin/kalmux statusline", "'/Users/x/my kalmux/kalmux' statusline",
                 "/usr/local/bin/tm statusline", "  /opt/kalmux statusline  "):
        assert tmstatusline.is_ours(good, link) is True
    for bad in ("", "bun run hud.ts", "/opt/kalmux ls", "echo kalmux statusline install", None):
        assert tmstatusline.is_ours(bad, link) is False


def test_uninstall_restores_the_original_or_drops_the_key(tmp_path):
    state = tmp_path / "state"
    link = Path("/opt/kalmux")
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts", "padding": 0})
    tmstatusline.install(settings, link, state)
    assert tmstatusline.uninstall(settings, state)[0] is True
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": "bun hud.ts", "padding": 0}
    assert (state / "statusline.json.restored").exists()   # the saved file is kept, only moved aside
    # nothing of ours in there any more: uninstall leaves the user's own status line alone
    assert tmstatusline.uninstall(settings, state)[0] is False
    # a machine that had no status line before kalmux: the key goes away again
    plain = settings_with(tmp_path, None)
    state2 = tmp_path / "state2"
    tmstatusline.install(plain, link, state2)
    assert tmstatusline.uninstall(plain, state2)[0] is True
    assert "statusLine" not in json.loads(plain.read_text())


def test_the_odd_corners_never_raise(tmp_path):
    assert tmstatusline.is_ours("'unbalanced kalmux statusline") is False       # shlex refuses, the fallback split wins
    assert tmstatusline._epoch("not a date") is None and tmstatusline._epoch({}) is None
    assert tmstatusline.default_line(None) == "kalmux"
    assert tmstatusline.passthrough(b"x", "", None, stdout=io.StringIO()) == 0
    assert tmstatusline.saved_object(tmp_path / "nowhere") == {}
    (tmp_path / "statusline.json").write_text("[1]")                          # saved file of the wrong shape
    assert tmstatusline.saved_object(tmp_path) == {} and tmstatusline.saved_command(tmp_path) == ""
    assert tmstatusline.fresh_count(tmp_path / "nowhere") == 0
    broken = io.BytesIO(b"")
    broken.read = lambda _n=None: (_ for _ in ()).throw(OSError("stdin is gone"))
    assert tmstatusline.run_tap(tmp_path / "nowhere", stdin=broken, stdout=io.StringIO()) == 0
    listy = tmp_path / "list.json"
    listy.write_text("[]")
    with pytest.raises(ValueError):
        tmstatusline.load_settings(listy)
    assert tmstatusline.status(listy, tmp_path) == {"routed": False, "command": "", "saved_command": "",
                                                    "saved_present": True, "fresh": 0}
    # uninstall on a machine that never had a status line and never installed one: nothing to do
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    assert tmstatusline.uninstall(empty, tmp_path / "nowhere")[0] is False


def test_status_reports_routing_saved_command_and_fresh_files(tmp_path):
    state = tmp_path / "state"
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts"})
    report = tmstatusline.status(settings, state)
    assert report == {"routed": False, "command": "bun hud.ts", "saved_command": "", "saved_present": False,
                      "fresh": 0}
    tmstatusline.install(settings, Path("/opt/kalmux"), state)
    now = int(time.time())
    (state / "status").mkdir(parents=True, exist_ok=True)
    for name, age in (("a", 0), ("b", 60), ("c", 4000)):
        f = state / "status" / f"{name}.json"
        f.write_text("{}")
        os.utime(f, (now - age, now - age))
    report = tmstatusline.status(settings, state)
    assert report["routed"] is True and report["saved_command"] == "bun hud.ts" and report["fresh"] == 2
    # an unreadable settings file never raises here, it just reads as not routed
    (tmp_path / "junk.json").write_text("{oops")
    assert tmstatusline.status(tmp_path / "junk.json", state)["routed"] is False


# ---------- saved-original lifecycle ----------
def test_uninstall_renames_the_saved_file_so_a_later_install_records_the_new_original(tmp_path):
    state = tmp_path / "state"
    link = Path("/opt/kalmux")
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts"})
    tmstatusline.install(settings, link, state)
    ok, message = tmstatusline.uninstall(settings, state)
    assert ok is True and "bun hud.ts" in message
    assert not (state / "statusline.json").exists()
    restored = json.loads((state / "statusline.json.restored").read_text())
    assert restored["statusLine"] == {"type": "command", "command": "bun hud.ts"}
    # the user moved on to another status line: installing again records THAT one, not the stale first
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "starship prompt"}}))
    assert tmstatusline.install(settings, link, state) is True
    assert tmstatusline.saved_command(state) == "starship prompt"
    ok, _ = tmstatusline.uninstall(settings, state)
    assert ok and json.loads(settings.read_text())["statusLine"]["command"] == "starship prompt"


def test_uninstall_refuses_when_the_saved_original_is_gone(tmp_path):
    state = tmp_path / "state"
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts"})
    tmstatusline.install(settings, Path("/opt/kalmux"), state)
    (state / "statusline.json").unlink()
    ok, message = tmstatusline.uninstall(settings, state)
    assert ok is False and "settings.json.bak-kalmux" in message
    # the key is NOT popped: the user keeps a working (kalmux) status line instead of none at all
    assert tmstatusline.is_ours(json.loads(settings.read_text())["statusLine"]["command"])


def test_status_reports_whether_the_saved_original_is_still_there(tmp_path):
    state = tmp_path / "state"
    settings = settings_with(tmp_path, {"type": "command", "command": "bun hud.ts"})
    assert tmstatusline.status(settings, state)["saved_present"] is False
    tmstatusline.install(settings, Path("/opt/kalmux"), state)
    assert tmstatusline.status(settings, state)["saved_present"] is True
    (state / "statusline.json").unlink()
    report = tmstatusline.status(settings, state)
    assert report["routed"] is True and report["saved_present"] is False


def test_install_uninstall_round_trips_unknown_keys_and_reads_old_saved_files(tmp_path):
    state = tmp_path / "state"
    original = {"type": "static", "text": "hi", "padding": 0, "refreshMs": 500}
    settings = settings_with(tmp_path, original)
    tmstatusline.install(settings, Path("/opt/kalmux"), state)
    wrapper = json.loads(settings.read_text())["statusLine"]
    assert wrapper == {"type": "command", "command": "/opt/kalmux statusline", "padding": 0,
                       "text": "hi", "refreshMs": 500}
    assert json.loads((state / "statusline.json").read_text())["statusLine"] == original
    ok, _ = tmstatusline.uninstall(settings, state)
    assert ok and json.loads(settings.read_text())["statusLine"] == original
    # a saved file written by an older kalmux (no "statusLine" key) still restores
    old = tmp_path / "old"
    old.mkdir()
    (tmp_path / "o").mkdir()
    (old / "statusline.json").write_text(json.dumps({"command": "bun hud.ts", "type": "command", "padding": 2,
                                                     "saved_at": 1}))
    routed = settings_with(tmp_path / "o", {"type": "command", "command": "/opt/kalmux statusline"})
    assert tmstatusline.saved_command(old) == "bun hud.ts"
    ok, _ = tmstatusline.uninstall(routed, old)
    assert ok and json.loads(routed.read_text())["statusLine"] == {"type": "command", "command": "bun hud.ts",
                                                                   "padding": 2}


def test_uninstall_drops_the_key_when_the_saved_object_is_empty(tmp_path):
    state = tmp_path / "state"
    plain = settings_with(tmp_path, None)
    tmstatusline.install(plain, Path("/opt/kalmux"), state)
    ok, message = tmstatusline.uninstall(plain, state)
    assert ok and "removed" in message and "statusLine" not in json.loads(plain.read_text())
    assert (state / "statusline.json.restored").exists()


# ---------- settings.json file handling ----------
def test_write_settings_preserves_the_mode_and_follows_a_symlink(tmp_path):
    real = tmp_path / "real" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"statusLine": {"type": "command", "command": "bun hud.ts"}}))
    real.chmod(0o600)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert tmstatusline.install(link, Path("/opt/kalmux"), tmp_path / "state") is True
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert json.loads(real.read_text())["statusLine"]["command"] == "/opt/kalmux statusline"
    assert real.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "real" / "settings.json.kalmux-tmp").exists()
    backup = tmp_path / "settings.json.bak-kalmux"
    assert backup.stat().st_mode & 0o777 == 0o600
    # a settings.json that did not exist is created private
    fresh = tmp_path / "fresh" / "settings.json"
    tmstatusline.write_settings(fresh, {"a": 1})
    assert fresh.stat().st_mode & 0o777 == 0o600


def test_write_settings_keeps_a_world_readable_mode_as_it_found_it(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    path.chmod(0o644)
    tmstatusline.write_settings(path, {"b": 2})
    assert path.stat().st_mode & 0o777 == 0o644 and json.loads(path.read_text()) == {"b": 2}


# ---------- self-reference guard ----------
def test_run_tap_never_runs_itself(tmp_path, capsys):
    state = tmp_path / "state"
    save_original(state, "/opt/kalmux statusline")
    rc = tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode()))
    captured = capsys.readouterr()
    assert rc == 0 and captured.out.strip() == "Opus 5 · ctx 42%"
    assert "kalmux" in captured.err and captured.err.count("\n") == 1
    # the same when the saved command merely mentions the wrapper somewhere, under any of its names
    save_original(state / "two", "exec /usr/local/bin/kmux statusline --whatever")
    assert tmstatusline.run_tap(state / "two", stdin=io.BytesIO(b"{}")) == 0
    assert capsys.readouterr().err != ""


def test_run_tap_marks_the_child_and_refuses_a_second_level(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    sink = tmp_path / "depth"
    save_original(state, f"printenv {tmstatusline.DEPTH_ENV} > {sink}")
    assert tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode())) == 0
    assert sink.read_text().strip() == "1"
    assert tmstatusline.DEPTH_ENV not in os.environ
    monkeypatch.setenv(tmstatusline.DEPTH_ENV, "1")
    sink.unlink()
    assert tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode())) == 0
    assert not sink.exists() and "Opus 5" in capsys.readouterr().out


# ---------- pruning the status dir ----------
def test_prune_status_removes_files_older_than_keep_days(tmp_path):
    state = tmp_path / "state"
    directory = state / "status"
    directory.mkdir(parents=True)
    now = int(time.time())
    for name, age_days in (("old", 40), ("young", 2)):
        f = directory / f"{name}.json"
        f.write_text("{}")
        os.utime(f, (now - age_days * 86400, now - age_days * 86400))
    (directory / "keep.txt").write_text("not ours")
    assert tmstatusline.prune_status(state, keep_days=30, now=now) == 1
    assert {p.name for p in directory.iterdir()} == {"young.json", "keep.txt", ".prune-stamp"}
    assert tmstatusline.prune_status(tmp_path / "nowhere", keep_days=30) == 0


def test_the_tap_prunes_at_most_once_every_ten_minutes(tmp_path):
    state = tmp_path / "state"
    stale = state / "status" / "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")
    old = int(time.time()) - 99 * 86400
    os.utime(stale, (old, old))
    tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode()), stdout=io.StringIO())
    assert not stale.exists() and (state / "status" / ".prune-stamp").exists()
    # a second run right away does not walk the directory again
    stale.write_text("{}")
    os.utime(stale, (old, old))
    tmstatusline.run_tap(state, stdin=io.BytesIO(json.dumps(PAYLOAD).encode()), stdout=io.StringIO())
    assert stale.exists()


# ---------- the kmux -> kalmux rename ----------
def test_install_over_the_kmux_wrapper_reroutes_without_saving_it(tmp_path):
    """The rename's one dangerous migration: a machine routed through `kmux statusline` must be re-pointed at
    kalmux, and the kmux wrapper must NEVER be recorded as the user's original (that would lose it for good)."""
    state = tmp_path / "state"
    kmux, kalmux = Path("/Users/x/.local/bin/kmux"), Path("/Users/x/.local/bin/kalmux")
    settings = settings_with(tmp_path, {"type": "command", "command": f"{kmux} statusline", "padding": 0})
    assert tmstatusline.install(settings, kalmux, state) is True
    assert json.loads(settings.read_text())["statusLine"] == {
        "type": "command", "command": f"{kalmux} statusline", "padding": 0}
    assert tmstatusline.saved_present(state) is False          # nothing of the user's was in there to save
    assert tmstatusline.saved_command(state) == ""
    assert tmstatusline.install(settings, kalmux, state) is False                 # now idempotent
    # the original saved before the rename (carried over with the state dir) survives the re-route untouched
    other = tmp_path / "state2"
    other.mkdir()
    (other / "statusline.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "bun hud.ts"},
                                                       "command": "bun hud.ts", "saved_at": 1}))
    (tmp_path / "again").mkdir()
    again = settings_with(tmp_path / "again", {"type": "command", "command": "/usr/local/bin/tm statusline"})
    assert tmstatusline.install(again, kalmux, other) is True
    assert json.loads(again.read_text())["statusLine"]["command"] == f"{kalmux} statusline"
    assert tmstatusline.saved_command(other) == "bun hud.ts"
    assert tmstatusline.uninstall(again, other)[0] is True
    assert json.loads(again.read_text())["statusLine"] == {"type": "command", "command": "bun hud.ts"}


def test_is_ours_spans_every_name_the_wrapper_ever_had():
    for good in ("/Users/x/.local/bin/kalmux statusline", "/Users/x/.local/bin/kmux statusline",
                 "/usr/local/bin/tm statusline", "'/Users/x/my tools/kalmux' statusline"):
        assert tmstatusline.is_ours(good) is True
    for bad in ("/opt/kalmuxer statusline", "/opt/kalmux ls", ""):
        assert tmstatusline.is_ours(bad) is False
