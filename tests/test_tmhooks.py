"""Kalmux's own Claude Code hook entries in settings.json, next to iTerm2's, and the hook health."""
import json
import os
import re
import time
from pathlib import Path

import pytest

from conftest import ASSETS
from kalmux import tmhooks

ITERM = "/Users/k/.config/iterm2/cc-status"


def wrapper_case_events() -> set[str]:
    """The events the hook's case statement acts on: the list of entries must be derived from it."""
    text = (ASSETS / "kalmux-hook").read_text()
    start = text.index('case "$event" in')
    block = text[start:text.index("  *) exit 0 ;;", start)]
    return set(re.findall(r"^  ([A-Za-z]+)\)", block, re.MULTILINE))


def iterm_settings(**extra):
    hooks = {ev: [{"hooks": [{"type": "command", "command": ITERM}]}] for ev in
             ("Notification", "PermissionRequest", "PostToolUse", "PreToolUse", "SessionEnd", "SessionStart", "Stop",
              "StopFailure", "SubagentStop", "UserPromptSubmit")}
    return {"cleanupPeriodDays": 7, "hooks": hooks, "statusLine": {"type": "command", "command": "x"}, **extra}


@pytest.fixture
def wrapper(tmp_path):
    w = tmp_path / "pkg" / "assets" / "kalmux-hook"
    w.parent.mkdir(parents=True)
    w.write_text("#!/bin/bash\nexit 0\n")
    w.chmod(0o755)
    return w


def kalmux_entries(settings):
    return {ev: [h for g in groups for h in g.get("hooks", []) if tmhooks.is_kalmux_command(h.get("command"))]
            for ev, groups in settings.get("hooks", {}).items()}


# ---------- the command ----------
def test_hook_events_are_exactly_what_the_wrapper_handles():
    assert set(tmhooks.HOOK_EVENTS) == wrapper_case_events()


def test_hook_command_never_mentions_cc_status(wrapper):
    """iTerm2 owns every hook whose command contains the word cc-status (it rewrites or dedupes them)."""
    for mode in (tmhooks.STATUS_ITERM2, tmhooks.STATUS_KALMUX):
        cmd = tmhooks.hook_command(wrapper, mode)
        assert "cc-status" not in cmd and tmhooks.is_kalmux_command(cmd)
        assert tmhooks.command_parts(cmd) == (str(wrapper), mode)


def test_hook_command_quotes_a_path_with_spaces(tmp_path):
    w = tmp_path / "Application Support" / "kalmux-hook"
    cmd = tmhooks.hook_command(w, tmhooks.STATUS_KALMUX)
    assert cmd.startswith("'") and tmhooks.command_parts(cmd) == (str(w), tmhooks.STATUS_KALMUX)


def test_a_wrapper_path_that_contains_cc_status_is_refused(tmp_path):
    with pytest.raises(ValueError, match="cc-status"):
        tmhooks.hook_command(tmp_path / "cc-status-stuff" / "kalmux-hook", tmhooks.STATUS_KALMUX)


@pytest.mark.parametrize("command,ours", [
    ("/x/kalmux-hook --status=iterm2", True),
    ("'/a b/kalmux-hook'", True),
    (ITERM, False),
    ('"/Users/k/.config/iterm2/it2" handle-agent-hook', False),
    ("echo kalmux-hook", False),
    ("", False),
    (None, False),
    ("'unbalanced", False),
])
def test_is_kalmux_command(command, ours):
    assert tmhooks.is_kalmux_command(command) is ours
    assert (tmhooks.command_parts(command) is not None) is ours


# ---------- install ----------
def test_install_adds_one_entry_per_event_and_leaves_iterm2_alone(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    original = iterm_settings()
    settings.write_text(json.dumps(original, indent=2))
    cmd = tmhooks.hook_command(wrapper, tmhooks.STATUS_ITERM2)
    assert tmhooks.install_hooks(settings, cmd) is True
    data = json.loads(settings.read_text())
    assert list(data) == list(original)                                  # key order kept
    assert data["cleanupPeriodDays"] == 7 and data["statusLine"] == original["statusLine"]
    for ev in tmhooks.HOOK_EVENTS:
        groups = data["hooks"][ev]
        assert groups[0] == original["hooks"][ev][0]                     # iTerm2's entry untouched, still first
        assert groups[-1] == {"hooks": [{"type": "command", "command": cmd, "timeout": tmhooks.HOOK_TIMEOUT}]}
    assert data["hooks"]["SubagentStop"] == original["hooks"]["SubagentStop"]
    assert "cc-status" not in json.dumps(kalmux_entries(data))


def test_install_is_idempotent_and_does_not_rewrite_the_file(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    cmd = tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX)
    assert tmhooks.install_hooks(settings, cmd) is True
    before = settings.read_text()
    os.utime(settings, (1_000_000, 1_000_000))
    assert tmhooks.install_hooks(settings, cmd) is False
    assert settings.read_text() == before and settings.stat().st_mtime == 1_000_000


def test_install_rewrites_our_entry_in_place_and_drops_duplicates(tmp_path, wrapper):
    stale = "/old/place/kalmux-hook --status=kalmux"
    data = iterm_settings()
    data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": stale}, {"type": "command", "command": "say hi"}]},
                             {"hooks": [{"type": "command", "command": ITERM}]},
                             {"hooks": [{"type": "command", "command": stale}]}]
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(data))
    cmd = tmhooks.hook_command(wrapper, tmhooks.STATUS_ITERM2)
    tmhooks.install_hooks(settings, cmd)
    stop = json.loads(settings.read_text())["hooks"]["Stop"]
    assert stop == [{"hooks": [{"type": "command", "command": cmd, "timeout": tmhooks.HOOK_TIMEOUT},
                               {"type": "command", "command": "say hi"}]},
                    {"hooks": [{"type": "command", "command": ITERM}]}]   # the emptied duplicate group is gone


def test_install_creates_settings_and_hooks_when_missing(tmp_path, wrapper):
    settings = tmp_path / "new" / "settings.json"
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == set(tmhooks.HOOK_EVENTS)
    assert oct(settings.stat().st_mode)[-3:] == "600"


def test_install_drops_our_entry_from_an_event_we_no_longer_use(tmp_path, wrapper):
    data = {"hooks": {"SubagentStop": [{"hooks": [{"type": "command", "command": "/x/kalmux-hook"}]},
                                       {"hooks": [{"type": "command", "command": ITERM}]}]}}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(data))
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    assert json.loads(settings.read_text())["hooks"]["SubagentStop"] == [{"hooks": [{"type": "command", "command": ITERM}]}]


def test_install_backs_up_once(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    first = settings.read_text()
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    backup = tmp_path / ("settings.json" + tmhooks.HOOKS_BACKUP_SUFFIX)
    assert backup.read_text() == first and oct(backup.stat().st_mode)[-3:] == "600"
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_ITERM2))   # a real change again
    assert backup.read_text() == first                                   # never overwritten


def test_install_writes_through_a_symlinked_settings_file(tmp_path, wrapper):
    real = tmp_path / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps(iterm_settings()))
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    tmhooks.install_hooks(link, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    assert link.is_symlink() and "kalmux-hook" in real.read_text()


def test_install_refuses_a_hooks_value_it_does_not_understand(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    for bad in ({"hooks": []}, {"hooks": {"Stop": "nope"}}):
        settings.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
        assert json.loads(settings.read_text()) == bad


def test_install_keeps_malformed_foreign_groups(tmp_path, wrapper):
    data = {"hooks": {"Stop": [{"matcher": "x"}, "weird", {"hooks": [{"command": 3}]}]}}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(data))
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    assert json.loads(settings.read_text())["hooks"]["Stop"][:3] == data["hooks"]["Stop"]


# ---------- uninstall ----------
def test_uninstall_removes_only_kalmux_entries(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    original = iterm_settings()
    original["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "/x/kalmux-hook"})
    settings.write_text(json.dumps(original))
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    assert tmhooks.uninstall_hooks(settings) is True
    assert json.loads(settings.read_text()) == iterm_settings()
    assert tmhooks.uninstall_hooks(settings) is False


def test_uninstall_drops_events_it_emptied(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": []}}))
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    tmhooks.uninstall_hooks(settings)
    assert json.loads(settings.read_text()) == {"model": "x", "hooks": {}}


def test_uninstall_without_a_settings_file(tmp_path):
    assert tmhooks.uninstall_hooks(tmp_path / "none.json") is False
    assert not (tmp_path / "none.json").exists()


# ---------- which mode ----------
def test_status_mode_follows_iterm2s_own_integration():
    assert tmhooks.status_mode(iterm_settings(), (3, 7, 6)) == tmhooks.STATUS_ITERM2
    assert tmhooks.status_mode(iterm_settings(), (3, 7, 5)) == tmhooks.STATUS_ITERM2
    assert tmhooks.status_mode(iterm_settings(), (3, 7, 4)) == tmhooks.STATUS_KALMUX     # still mis-routes in tmux
    assert tmhooks.status_mode(iterm_settings(), None) == tmhooks.STATUS_KALMUX
    assert tmhooks.status_mode({}, (3, 8, 0)) == tmhooks.STATUS_KALMUX                  # no iTerm2 hooks at all
    new_style = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '"/u/.config/iterm2/it2" handle-agent-hook'}]}]}}
    assert tmhooks.status_mode(new_style, (3, 7, 7)) == tmhooks.STATUS_ITERM2


def test_iterm2_version_reads_the_bundle_plist(tmp_path):
    import plistlib
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "3.7.6"}))
    assert tmhooks.iterm2_version(plist) == (3, 7, 6)
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "3.7.7beta2"}))
    assert tmhooks.iterm2_version(plist) == (3, 7, 7)
    plist.write_text("garbage")
    assert tmhooks.iterm2_version(plist) is None
    assert tmhooks.iterm2_version(tmp_path / "missing.plist") is None


# ---------- wiring problems (doctor + health) ----------
def test_wiring_problems(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    assert "no Kalmux entry" in tmhooks.wiring_problems(json.loads(settings.read_text()), wrapper)[0]
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX))
    data = json.loads(settings.read_text())
    assert tmhooks.wiring_problems(data, wrapper) == []
    assert tmhooks.wiring_problems(data, wrapper, tmhooks.STATUS_KALMUX) == []
    assert "iTerm2" in tmhooks.wiring_problems(data, wrapper, tmhooks.STATUS_ITERM2)[0]
    moved = tmp_path / "elsewhere" / "kalmux-hook"
    assert "expected" in tmhooks.wiring_problems(data, moved)[0]
    del data["hooks"]["Stop"]
    assert tmhooks.wiring_problems(data, wrapper) == ["no Kalmux entry for Stop"]
    wrapper.unlink()
    assert any("missing" in p for p in tmhooks.wiring_problems(data, wrapper))


# ---------- heartbeat + health ----------
def stamp(path: Path, ts: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    os.utime(path, (ts, ts))


def sid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-000000000000"


def registry(*entries, started=None):
    """(alive, statusUpdatedAt s) per session; `started` = startedAt in seconds for every one of them."""
    return {f"s{i}:@{i}.%{i}": {"alive": alive, "statusUpdatedAt": ts * 1000 if ts else None, "sessionId": sid(i),
                                "startedAt": started * 1000 if started else None}
            for i, (alive, ts) in enumerate(entries)}


def beat(state: Path, i: int, ts: float):
    """Session i's own heartbeat (the wrapper truncates ${STATE}/hooks/<session_id> on every event)."""
    stamp(state / tmhooks.SESSION_BEATS / sid(i), ts)


@pytest.fixture
def wired(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    state = tmp_path / "state"
    state.mkdir()
    tmhooks.install_hooks(settings, tmhooks.hook_command(wrapper, tmhooks.STATUS_ITERM2), state_dir=state)
    stamp(state / tmhooks.WIRED_NAME, 1000)       # wired long ago
    return {"settings": settings, "state": state, "wrapper": wrapper}


def health(wired, reg):
    return tmhooks.hook_health(wired["settings"], wired["wrapper"], wired["state"], reg)


def test_install_stamps_when_the_hooks_were_wired(tmp_path, wrapper):
    state = tmp_path / "state"
    tmhooks.install_hooks(tmp_path / "s.json", tmhooks.hook_command(wrapper, tmhooks.STATUS_KALMUX), state_dir=state)
    assert abs((state / tmhooks.WIRED_NAME).stat().st_mtime - time.time()) < 60


def test_health_ok_without_claude_sessions(wired):
    assert health(wired, {}) == {"ok": True, "last_seen": None, "reason": ""}


def test_health_ok_when_the_session_has_reported_since_the_wiring(wired):
    stamp(wired["state"] / tmhooks.HEARTBEAT_NAME, 9_000)
    beat(wired["state"], 0, 9_000)
    assert health(wired, registry((True, 9_050))) == {"ok": True, "last_seen": 9_000, "reason": ""}


def test_health_ok_after_an_interrupt_that_fired_no_hook(wired):
    """Esc on a 5 min tool call: no Stop, no PostToolUse, and the registry flips to idle now."""
    stamp(wired["state"] / tmhooks.HEARTBEAT_NAME, 9_000)
    beat(wired["state"], 0, 9_000)                            # PreToolUse, 5 min ago
    assert health(wired, registry((True, 9_300)))["ok"] is True


def test_health_ok_after_a_late_approval_while_the_tool_runs(wired):
    """A permission prompt answered after 10 min: waiting -> busy, and no hook until PostToolUse."""
    beat(wired["state"], 0, 8_000)                            # PermissionRequest
    assert health(wired, registry((True, 8_600)))["ok"] is True


def test_health_not_ok_for_a_live_session_that_never_reported_since_the_wiring(wired):
    stamp(wired["state"] / tmhooks.HEARTBEAT_NAME, 9_000)
    beat(wired["state"], 0, 9_000)                            # this one is fine
    beat(wired["state"], 1, 900)                              # only from before the entries were (re)written
    h = health(wired, registry((True, 9_050), (True, 9_000), (False, 9_900)))
    assert h["ok"] is False and h["last_seen"] == 9_000
    assert "s1" in h["reason"] and "s0" not in h["reason"] and "s2" not in h["reason"]
    assert "restart" in h["reason"].lower()


def test_health_counts_activity_only_after_the_wiring_or_the_session_start(wired):
    stamp(wired["state"] / tmhooks.WIRED_NAME, 8_000)
    assert health(wired, registry((False, 9_900), (True, 8_100)))["ok"] is True     # within SILENCE_LIMIT
    assert health(wired, registry((True, 9_500), started=9_450))["ok"] is True      # 50 s after its start
    assert health(wired, registry((True, 9_500), started=9_000))["ok"] is False


def test_health_skips_a_session_without_a_usable_id(wired):
    reg = {"s:@1.%1": {"alive": True, "statusUpdatedAt": 9_000_000, "sessionId": "../../etc"}}
    assert health(wired, reg)["ok"] is True


def test_health_reason_lists_a_few_sessions_and_counts_the_rest(wired):
    h = health(wired, registry(*[(True, 9_000)] * 6))
    assert h["ok"] is False and "s0" in h["reason"] and "+3 more" in h["reason"]


def test_prune_beats_drops_old_files_of_sessions_that_are_gone(wired):
    state = wired["state"]
    beat(state, 0, 1_000)                                     # alive: kept, however old
    beat(state, 1, 1_000)                                     # gone and old: dropped
    beat(state, 2, 99_000)                                    # gone but recent: kept
    (state / tmhooks.SESSION_BEATS / "not-an-id").write_text("")
    removed = tmhooks.prune_beats(state, registry((True, 1), (False, 1), (False, 1)), now=100_000)
    assert removed == 1
    assert sorted(p.name for p in (state / tmhooks.SESSION_BEATS).iterdir()) == sorted([sid(0), sid(2), "not-an-id"])
    assert tmhooks.prune_beats(tmp := state / "nowhere", {}, now=1) == 0 and not tmp.exists()


def test_health_not_ok_when_settings_lack_our_entries(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    h = tmhooks.hook_health(settings, wrapper, tmp_path / "state", {})
    assert h["ok"] is False and "settings.json" in h["reason"]
    settings.write_text("{broken")
    assert tmhooks.hook_health(settings, wrapper, tmp_path / "state", {})["ok"] is False


def test_health_reason_names_the_settings_path_once(tmp_path, wrapper):
    settings = tmp_path / "settings.json"
    settings.write_text("[]")
    reason = tmhooks.hook_health(settings, wrapper, tmp_path / "state", {})["reason"]
    assert reason.count(str(settings)) == 1
    settings.write_text("{broken")
    assert str(settings) in tmhooks.hook_health(settings, wrapper, tmp_path / "state", {})["reason"]


def test_cached_health_prunes_session_beats_at_most_once_an_hour(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(tmhooks, "prune_beats", lambda *a, **kw: calls.append(1) or 0)
    clock = iter([10_000, 10_100, 10_000 + tmhooks.PRUNE_BEATS_EVERY + 1])
    cache = tmhooks.HookHealth(wired["settings"], wired["wrapper"], wired["state"], clock=lambda: next(clock))
    for _ in range(3):
        cache.check({})
    assert len(calls) == 2


def test_cached_health_rereads_settings_only_when_they_change(wired, monkeypatch):
    calls = []
    real = tmhooks.wiring_problems
    monkeypatch.setattr(tmhooks, "wiring_problems", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    cache = tmhooks.HookHealth(wired["settings"], wired["wrapper"], wired["state"])
    assert cache.check({})["ok"] and cache.check({})["ok"]
    assert len(calls) == 1
    tmhooks.uninstall_hooks(wired["settings"])
    os.utime(wired["settings"], (2_000_000, 2_000_000))
    assert cache.check({})["ok"] is False and len(calls) == 2


# ---------- giving cc-status back to iTerm2 ----------
def test_reclaim_points_a_kalmux_link_back_at_the_bundle(tmp_path):
    bundle = tmp_path / "iTerm.app" / "cc-status"
    bundle.parent.mkdir()
    bundle.write_text("binary")
    link = tmp_path / "iterm2" / "cc-status"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "old" / "src" / "kalmux" / "assets" / "cc-status-tmux")
    assert "gave" in tmhooks.reclaim_iterm2_link(link, bundle)
    assert os.readlink(link) == str(bundle)
    assert tmhooks.reclaim_iterm2_link(link, bundle) == ""               # iTerm2's now: left alone


def test_reclaim_without_a_bundle_restores_the_users_file_or_removes_the_link(tmp_path):
    link = tmp_path / "cc-status"
    link.symlink_to("/gone/kalmux-hook")
    backup = tmp_path / "cc-status.bak-kalmux"
    backup.write_text("mine")
    tmhooks.reclaim_iterm2_link(link, tmp_path / "no-bundle")
    assert not link.is_symlink() and link.read_text() == "mine" and not backup.exists()
    link.unlink()
    link.symlink_to("/gone/cc-status-tmux")
    tmhooks.reclaim_iterm2_link(link, tmp_path / "no-bundle")
    assert not link.exists() and not link.is_symlink()


def test_reclaim_leaves_anything_that_is_not_ours(tmp_path):
    link = tmp_path / "cc-status"
    assert tmhooks.reclaim_iterm2_link(link, tmp_path / "b") == ""        # missing
    link.symlink_to("/Applications/iTerm.app/Contents/Resources/utilities/cc-status")
    assert tmhooks.reclaim_iterm2_link(link, tmp_path / "b") == "" and link.is_symlink()
    link.unlink()
    link.write_text("a real file")
    assert tmhooks.reclaim_iterm2_link(link, tmp_path / "b") == "" and link.read_text() == "a real file"


# ---------- the setup step ----------
def test_wire_claude_hooks_end_to_end(tmp_path, wrapper):
    import plistlib
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(iterm_settings()))
    plist = tmp_path / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "3.7.6"}))
    link = tmp_path / "iterm2" / "cc-status"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "old" / "cc-status-tmux")
    bundle = tmp_path / "bundle-cc-status"
    bundle.write_text("")
    note = tmhooks.wire_claude_hooks(settings, wrapper, tmp_path / "state", hook_link=link, bundle=bundle, info_plist=plist)
    assert "iTerm2" in note and "9 event" in note and "status by iTerm2" in note
    assert "restart one only if" in note and "when restarted" not in note
    assert os.readlink(link) == str(bundle)
    cmds = {h["command"] for hs in kalmux_entries(json.loads(settings.read_text())).values() for h in hs}
    assert cmds == {tmhooks.hook_command(wrapper, tmhooks.STATUS_ITERM2)}
    assert "already" in tmhooks.wire_claude_hooks(settings, wrapper, tmp_path / "state", hook_link=link, bundle=bundle,
                                                  info_plist=plist)


def test_wire_claude_hooks_refuses_a_wrapper_that_cannot_run(tmp_path):
    w = tmp_path / "kalmux-hook"
    w.write_text("")
    with pytest.raises(OSError, match="executable"):
        tmhooks.wire_claude_hooks(tmp_path / "s.json", w, tmp_path / "state", hook_link=tmp_path / "l",
                                  bundle=tmp_path / "b", info_plist=tmp_path / "p")
    assert not (tmp_path / "s.json").exists()
