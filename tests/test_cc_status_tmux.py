"""End-to-end tests for the cc-status-tmux hook wrapper."""
import base64
import json
import subprocess

import pytest

from conftest import ASSETS

WRAPPER = ASSETS / "cc-status-tmux"
ESC = "\x1b"
BEL = "\x07"


def run_hook(fake_env, payload, extra_env=None, raw=None):
    env = dict(fake_env["env"])
    if extra_env:
        env.update(extra_env)
    data = raw if raw is not None else json.dumps(payload)
    return subprocess.run([str(WRAPPER)], input=data, text=True, env=env, capture_output=True, timeout=20, check=False)


def tmux_log(fake_env):
    return fake_env["log"].read_text() if fake_env["log"].exists() else ""


def tty_bytes(fake_env):
    return fake_env["tty"].read_text()


def osc_status(state, dot, text, detail=""):
    return f"{ESC}]21337;status={state};indicator={dot};status-color={text};detail={detail}{ESC}\\"


def uservar(name, value):
    b64 = base64.b64encode(value.encode()).decode()
    return f"{ESC}]1337;SetUserVar={name}={b64}{BEL}"


def test_user_prompt_submit_sets_working(fake_env):
    r = run_hook(fake_env, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/tmp"})
    assert r.returncode == 0, r.stderr
    log = tmux_log(fake_env)
    assert "set -p -t %7 @cc_state working" in log
    assert "@cc_event UserPromptSubmit" in log
    assert "@cc_since " in log
    out = tty_bytes(fake_env)
    assert osc_status("working", "#ff9500", "#ff9500") in out
    assert uservar("cc_state", "working") in out


def test_permission_request_is_waiting_with_tool(fake_env):
    run_hook(fake_env, {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "session_id": "s1"})
    assert "@cc_state waiting" in tmux_log(fake_env)
    assert "@cc_detail Allow Bash?" in tmux_log(fake_env)
    assert osc_status("waiting", "#5f87ff", "#5f87ff", "Allow Bash?") in tty_bytes(fake_env)


def test_permission_request_exit_plan_mode(fake_env):
    run_hook(fake_env, {"hook_event_name": "PermissionRequest", "tool_name": "ExitPlanMode"})
    assert "@cc_detail Review proposed plan?" in tmux_log(fake_env)


def test_notification_permission_prompt_waiting(fake_env):
    run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                        "message": "Claude needs your permission to use Edit"})
    assert "@cc_state waiting" in tmux_log(fake_env)
    assert "Claude needs your permission to use Edit" in tty_bytes(fake_env)


def test_notification_idle_prompt_is_idle(fake_env):
    run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "x"})
    assert "@cc_state idle" in tmux_log(fake_env)
    assert osc_status("idle", "#00d75f", "#888888") in tty_bytes(fake_env)


def test_notification_other_types_wait_with_message(fake_env):
    run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": "elicitation_dialog",
                        "message": "MCP needs input"})
    assert "@cc_state waiting" in tmux_log(fake_env)
    assert "@cc_detail MCP needs input" in tmux_log(fake_env)


def test_pre_tool_use_working_with_tool_name(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "/x"}})
    assert "@cc_state working" in tmux_log(fake_env)
    assert "@cc_detail Edit" in tmux_log(fake_env)


def test_ask_user_question_is_waiting_with_question(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": [{"question": "Which DB?"}]}})
    assert "@cc_state waiting" in tmux_log(fake_env)
    assert "@cc_detail Which DB?" in tmux_log(fake_env)
    run_hook(fake_env, {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"})
    states = [line.split("@cc_state ", 1)[1].split(" ;")[0] for line in tmux_log(fake_env).splitlines() if "@cc_state " in line]
    assert states == ["waiting", "working"]


def test_stop_sanitizes_and_truncates_last_message(fake_env):
    msg = "Đã xong — 3 tệp " + ESC + "]0;evil" + BEL + " line2\nline3 " + "x" * 300
    run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": msg})
    log = tmux_log(fake_env)
    assert "@cc_state idle" in log
    detail_line = next(line for line in log.splitlines() if "@cc_detail" in line)
    detail = detail_line.split("@cc_detail ", 1)[1]
    assert detail.startswith("✓ Đã xong — 3 tệp")
    assert ESC not in detail and BEL not in detail and "\n" not in detail
    assert len(detail) <= 122  # "✓ " + 120
    out = tty_bytes(fake_env)
    status_seq = out[out.index(ESC + "]21337"):]
    status_seq = status_seq[: status_seq.index(ESC + "\\") + 2]
    assert status_seq.count(ESC) == 2  # only the OSC opener and the ST terminator
    assert ";" not in detail


def test_stop_failure_idle_with_error(fake_env):
    run_hook(fake_env, {"hook_event_name": "StopFailure", "error": "rate limit"})
    assert "@cc_state idle" in tmux_log(fake_env)
    assert "@cc_detail ✗ rate limit" in tmux_log(fake_env)


def test_session_start_records_identity(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "session_id": "abc-123", "cwd": "/Volumes/X/proj"})
    log = tmux_log(fake_env)
    assert "@cc_state idle" in log
    assert "@cc_session_id abc-123" in log
    assert "@cc_cwd /Volumes/X/proj" in log


def test_session_end_clears_everything(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionEnd", "reason": "exit"})
    log = tmux_log(fake_env)
    assert "-u @cc_state" in log and "-u @cc_detail" in log
    assert osc_status("", "", "", "") in tty_bytes(fake_env)
    assert uservar("cc_state", "") in tty_bytes(fake_env)


def test_ignored_events_do_nothing(fake_env):
    for ev in ("SubagentStop", "PreCompact", "SomethingNew"):
        r = run_hook(fake_env, {"hook_event_name": ev})
        assert r.returncode == 0
    assert "@cc_state" not in tmux_log(fake_env)
    assert tty_bytes(fake_env) == ""


def test_outside_tmux_delegates_to_original(fake_env):
    env = {"TMUX_PANE": "", "TMUX": ""}
    payload = {"hook_event_name": "Stop", "last_assistant_message": "hi"}
    r = run_hook(fake_env, payload, extra_env=env)
    assert r.returncode == 0
    orig = fake_env["orig_log"].read_text()
    assert "STDIN: " + json.dumps(payload) in orig
    assert "@cc_state" not in tmux_log(fake_env)


def test_tmux_failure_still_exits_zero(fake_env):
    r = run_hook(fake_env, {"hook_event_name": "Stop"}, extra_env={"FAKE_TMUX_FAIL": "1"})
    assert r.returncode == 0


def test_invalid_json_exits_zero(fake_env):
    r = run_hook(fake_env, None, raw="not json at all")
    assert r.returncode == 0
    assert "@cc_state" not in tmux_log(fake_env)


def test_reapplies_session_tab_color_when_set(fake_env):
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit"}, extra_env={"FAKE_TM_COLOR": "#ff8800"})
    out = tty_bytes(fake_env)
    assert f"{ESC}]6;1;bg;red;brightness;255{BEL}" in out
    assert f"{ESC}]6;1;bg;green;brightness;136{BEL}" in out
    assert f"{ESC}]6;1;bg;blue;brightness;0{BEL}" in out


def test_no_color_sequence_without_tm_color(fake_env):
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit"})
    assert "]6;1;bg" not in tty_bytes(fake_env)


def test_hostile_tool_input_does_not_kill_the_hook(fake_env):
    for bad in ({"questions": "one two"}, {"questions": ["a"]}, {"questions": {"a": 1}}, "str", [1]):
        run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "mcp__x__q", "tool_input": bad})
    assert tmux_log(fake_env).count("@cc_state working") == 5


def test_session_start_compact_keeps_current_state(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s", "cwd": "/x"})
    assert "@cc_state" not in tmux_log(fake_env)
    run_hook(fake_env, {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s", "cwd": "/x"})
    assert "@cc_state idle" in tmux_log(fake_env)


def test_post_tool_use_keeps_tool_name_as_detail(fake_env):
    run_hook(fake_env, {"hook_event_name": "PostToolUse", "tool_name": "Bash"})
    assert "@cc_detail Bash" in tmux_log(fake_env)


def test_session_end_clears_identity_too(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionEnd"})
    log = tmux_log(fake_env)
    assert "-u @cc_session_id" in log and "-u @cc_cwd" in log


def test_identity_fields_are_sanitized(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "id;1", "cwd": "/tmp/a;b"})
    log = tmux_log(fake_env)
    assert "@cc_session_id id,1" in log and "@cc_cwd /tmp/a,b" in log


def test_no_tty_write_when_session_has_no_client(fake_env):
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit"}, extra_env={"FAKE_ATTACHED": "0"})
    assert "@cc_state working" in tmux_log(fake_env)
    assert tty_bytes(fake_env) == ""


def test_single_tmux_display_roundtrip(fake_env):
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit"}, extra_env={"FAKE_TM_COLOR": "#ff8800"})
    log = tmux_log(fake_env)
    assert log.count("display -p") == 1 and "show " not in log
    assert "]6;1;bg;red;brightness;255" in tty_bytes(fake_env)


def test_stop_truncation_is_utf8_safe_even_with_c_locale(fake_env):
    msg = "ă" * 200
    run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": msg}, extra_env={"LC_ALL": "C"})
    detail = next(line for line in tmux_log(fake_env).splitlines() if "@cc_detail" in line).split("@cc_detail ", 1)[1]
    assert detail == "✓ " + "ă" * 120


# --- phase 4 §2: hook hygiene (the tty write is now rare) -----------------------------------

UUID = "0b9c3b41-2f7e-4a1d-8c55-7e1f9a2d4b60"


def trail_lines(fake_env, session_id=UUID):
    path = fake_env["trace"] / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_the_read_and_the_write_are_one_tmux_invocation(fake_env):
    """Two invocations let two concurrent hook runs both read the OLD @cc_state and both write the tty."""
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit"})
    log = tmux_log(fake_env).splitlines()
    assert len(log) == 1 and log[0].startswith("display -p")
    assert "#{@cc_state}" in log[0] and "#{session_name}" in log[0]
    assert log[0].index("#{@cc_state}") < log[0].index("@cc_state working")   # read first, then write
    assert "; set -p -t %7 @cc_state working ;" in log[0]


def test_session_end_clears_every_option_in_the_same_invocation(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionEnd", "reason": "logout"},
             extra_env={"FAKE_CC_STATE": "idle"})
    log = tmux_log(fake_env).splitlines()
    assert len(log) == 1
    for opt in ("@cc_state", "@cc_since", "@cc_event", "@cc_session_id", "@cc_cwd", "@cc_detail"):
        assert f"set -p -t %7 -u {opt}" in log[0]


def test_a_payload_larger_than_256k_is_read_whole(fake_env):
    """`head -c 262144` cut the JSON in half, so jq failed and the whole event was dropped."""
    body = "start " + "x" * 300_000 + " end"
    r = run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": body,
                            "session_id": UUID, "cwd": "/tmp"})
    assert r.returncode == 0
    assert "@cc_state idle" in tmux_log(fake_env)
    lines = trail_lines(fake_env)
    assert len(lines) == 1 and lines[0]["detail"].startswith("start xxx") and len(lines[0]["detail"]) == 200


def test_working_to_working_sets_options_but_skips_the_tty(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "Edit"},
             extra_env={"FAKE_CC_STATE": "working"})
    assert "@cc_state working" in tmux_log(fake_env)
    assert "@cc_detail Edit" in tmux_log(fake_env)
    assert tty_bytes(fake_env) == ""


def test_post_tool_use_while_working_skips_the_tty(fake_env):
    run_hook(fake_env, {"hook_event_name": "PostToolUse", "tool_name": "Bash"},
             extra_env={"FAKE_CC_STATE": "working"})
    assert "@cc_detail Bash" in tmux_log(fake_env)
    assert tty_bytes(fake_env) == ""


def test_state_change_writes_the_tty(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "source": "startup"},
             extra_env={"FAKE_CC_STATE": "working"})
    assert osc_status("idle", "#00d75f", "#888888") in tty_bytes(fake_env)


def test_permission_request_always_writes_even_without_a_state_change(fake_env):
    run_hook(fake_env, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"},
             extra_env={"FAKE_CC_STATE": "waiting"})
    assert osc_status("waiting", "#5f87ff", "#5f87ff", "Allow Bash?") in tty_bytes(fake_env)


def test_ask_user_question_always_writes_even_without_a_state_change(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": [{"question": "Which DB?"}]}},
             extra_env={"FAKE_CC_STATE": "waiting"})
    assert osc_status("waiting", "#5f87ff", "#5f87ff", "Which DB?") in tty_bytes(fake_env)


def test_stop_and_notification_always_write(fake_env):
    run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": "done"},
             extra_env={"FAKE_CC_STATE": "idle"})
    assert osc_status("idle", "#00d75f", "#888888", "✓ done") in tty_bytes(fake_env)
    fake_env["tty"].write_text("")  # the fake tty is a plain file; each write starts at offset 0
    run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": "idle_prompt"},
             extra_env={"FAKE_CC_STATE": "idle"})
    assert osc_status("idle", "#00d75f", "#888888") in tty_bytes(fake_env)


def test_working_detail_is_empty_in_the_osc_but_kept_in_tmux(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "Edit"},
             extra_env={"FAKE_CC_STATE": "idle"})
    assert "@cc_detail Edit" in tmux_log(fake_env)
    assert osc_status("working", "#ff9500", "#ff9500", "") in tty_bytes(fake_env)


def test_tab_color_is_replayed_on_every_write(fake_env):
    run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": "ok"},
             extra_env={"FAKE_CC_STATE": "idle", "FAKE_TM_COLOR": "#ff8800"})
    assert f"{ESC}]6;1;bg;red;brightness;255{BEL}" in tty_bytes(fake_env)


# --- phase 4 §2: the trail -------------------------------------------------------------------

def test_trail_records_the_four_lifecycle_events(fake_env):
    common = {"session_id": UUID, "cwd": "/Volumes/X/proj"}
    run_hook(fake_env, dict(common, hook_event_name="SessionStart", source="startup"),
             extra_env={"FAKE_TM_COLOR": "#bf5af2", "FAKE_SESSION": "worker"})
    run_hook(fake_env, dict(common, hook_event_name="Stop", last_assistant_message="all done pa"),
             extra_env={"FAKE_SESSION": "worker"})
    run_hook(fake_env, dict(common, hook_event_name="StopFailure", error="rate limit"),
             extra_env={"FAKE_SESSION": "worker"})
    run_hook(fake_env, dict(common, hook_event_name="SessionEnd", reason="prompt_input_exit"),
             extra_env={"FAKE_SESSION": "worker"})

    lines = trail_lines(fake_env)
    assert [ln["event"] for ln in lines] == ["SessionStart", "Stop", "StopFailure", "SessionEnd"]
    for ln in lines:
        assert ln["session_id"] == UUID
        assert ln["tmux_session"] == "worker"
        assert ln["pane"] == "%7"
        assert ln["cwd"] == "/Volumes/X/proj"
        assert isinstance(ln["ts"], int) and ln["ts"] > 1_700_000_000
        assert set(ln) >= {"ts", "event", "session_id", "tmux_session", "pane", "cwd", "color"}
    assert lines[0]["source"] == "startup"
    assert lines[0]["color"] == "#bf5af2"
    assert lines[1]["detail"] == "all done pa"
    assert lines[2]["detail"] == "rate limit"
    assert lines[3]["reason"] == "prompt_input_exit"


def test_trail_detail_is_sanitized_and_capped_at_200_chars(fake_env):
    msg = "hello " + ESC + "]0;evil" + BEL + "\nworld " + "x" * 400
    run_hook(fake_env, {"hook_event_name": "Stop", "session_id": UUID, "last_assistant_message": msg})
    detail = trail_lines(fake_env)[0]["detail"]
    assert len(detail) == 200
    assert detail.startswith("hello ")
    assert ESC not in detail and BEL not in detail and "\n" not in detail


def test_trail_json_survives_hostile_text(fake_env):
    msg = 'he said "hi" \\ {"a": 1} \t and ] more'
    run_hook(fake_env, {"hook_event_name": "Stop", "session_id": UUID, "last_assistant_message": msg},
             extra_env={"FAKE_SESSION": 'we"ird\\name'})
    line = trail_lines(fake_env)[0]           # json.loads would raise on a broken line
    assert line["tmux_session"] == 'we"ird\\name'
    assert '"hi"' in line["detail"] and "{" in line["detail"]


def test_no_trail_for_non_lifecycle_events(fake_env):
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": UUID})
    run_hook(fake_env, {"hook_event_name": "UserPromptSubmit", "session_id": UUID})
    run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": "idle_prompt",
                        "session_id": UUID})
    assert trail_lines(fake_env) == []


def test_no_trail_without_a_uuid_session_id(fake_env):
    for sid in ("", "abc-123", "../../etc/passwd", UUID.upper(), UUID + "x", UUID.replace("-", "")):
        run_hook(fake_env, {"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": "x"})
    trace = fake_env["trace"]
    assert not trace.exists() or list(trace.iterdir()) == []


def test_trail_directory_is_private(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "session_id": UUID, "source": "startup"})
    assert oct(fake_env["trace"].stat().st_mode)[-3:] == "700"


def test_trail_defaults_to_the_state_dir_under_home(fake_env):
    run_hook(fake_env, {"hook_event_name": "SessionStart", "session_id": UUID, "source": "startup"},
             extra_env={"KALMUX_STATE_DIR": ""})
    path = fake_env["tmp"] / ".local/state/kalmux/trace" / f"{UUID}.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["event"] == "SessionStart"


def test_unwritable_state_dir_does_not_break_the_hook(fake_env):
    blocked = fake_env["tmp"] / "blocked"
    blocked.write_text("not a directory")
    r = run_hook(fake_env, {"hook_event_name": "Stop", "session_id": UUID, "last_assistant_message": "x"},
                 extra_env={"KALMUX_STATE_DIR": str(blocked / "kalmux")})
    assert r.returncode == 0 and r.stderr == ""
    assert "@cc_state idle" in tmux_log(fake_env)
    assert osc_status("idle", "#00d75f", "#888888", "✓ x") in tty_bytes(fake_env)


# ---------- the alert Claude Code cannot post from inside tmux ----------
def osc9(message):
    return f"{ESC}]9;{message}{BEL}"


def notif(fake_env, message="Claude is waiting for your input", ntype="idle_prompt", **kw):
    return run_hook(fake_env, {"hook_event_name": "Notification", "notification_type": ntype, "message": message}, **kw)


def test_notification_posts_the_iterm2_alert_claude_skips_in_tmux(fake_env):
    """Claude Code picks its channel from TERM_PROGRAM, which tmux sets to `tmux`, and then finds no method at
    all. The hook posts the OSC 9 alert Claude would have posted from a plain iTerm2 tab, on the same event."""
    notif(fake_env)
    out = tty_bytes(fake_env)
    assert osc9("Claude is waiting for your input") in out
    assert out.index(ESC + "]9;") < out.index(ESC + "]21337")      # the alert is the point of this write
    assert osc9("Claude is waiting for your input") + BEL not in out  # no bell unless asked for


def test_alert_text_is_sanitized_and_capped(fake_env):
    notif(fake_env, "Allow " + ESC + "]0;evil" + BEL + " Bash;\nnow " + "y" * 400, ntype="permission_prompt")
    out = tty_bytes(fake_env)
    start = out.index(ESC + "]9;") + len(ESC + "]9;")
    alert = out[start:out.index(BEL, start)]
    assert ESC not in alert and ";" not in alert and "\n" not in alert and alert.startswith("Allow")
    assert len(alert) <= 200


def test_only_notification_events_post_alerts(fake_env):
    run_hook(fake_env, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    run_hook(fake_env, {"hook_event_name": "Stop", "last_assistant_message": "done"})
    run_hook(fake_env, {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": [{"question": "which one?"}]}})
    notif(fake_env, "", ntype="idle_prompt")                        # nothing to say: no empty alert either
    assert ESC + "]9;" not in tty_bytes(fake_env)


@pytest.mark.parametrize("channel", ["iterm2", "iterm2_with_bell", "terminal_bell", "kitty", "notifications_disabled"])
def test_hook_steps_aside_when_the_user_picked_claudes_own_channel(fake_env, channel):
    """With preferredNotifChannel set, Claude posts (or deliberately does not); a second alert would be noise."""
    (fake_env["tmp"] / ".claude.json").write_text(json.dumps({"preferredNotifChannel": channel, "projects": {}}))
    notif(fake_env)
    assert ESC + "]9;" not in tty_bytes(fake_env)


def test_channel_auto_is_the_same_as_unset(fake_env):
    (fake_env["tmp"] / ".claude.json").write_text(json.dumps({"preferredNotifChannel": "auto"}))
    notif(fake_env, "m")
    assert osc9("m") in tty_bytes(fake_env)


def test_claude_config_dir_is_honoured_for_the_channel(fake_env):
    other = fake_env["tmp"] / "cfgdir"
    other.mkdir()
    (other / ".claude.json").write_text(json.dumps({"preferredNotifChannel": "terminal_bell"}))
    notif(fake_env, "m", extra_env={"CLAUDE_CONFIG_DIR": str(other)})
    assert ESC + "]9;" not in tty_bytes(fake_env)


def test_notify_config_never_always_and_bell(fake_env):
    cfg = fake_env["tmp"] / ".config" / "kalmux" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[claude]\nnew = "claude"\n\n[notify]\n# a comment\niterm2 = "never"   # trailing comment\n')
    notif(fake_env, "m")
    assert ESC + "]9;" not in tty_bytes(fake_env)

    cfg.write_text("[notify]\niterm2='always'\nbell = true\n")      # no spaces, single quotes: still read
    (fake_env["tmp"] / ".claude.json").write_text(json.dumps({"preferredNotifChannel": "iterm2"}))
    notif(fake_env, "m")
    assert osc9("m") + BEL in tty_bytes(fake_env)                   # posted despite Claude's channel, plus a bell

    fake_env["tty"].write_text("")
    cfg.write_text('[other]\niterm2 = "never"\n[notify]\nbell = false\n')   # the key only counts inside [notify]
    (fake_env["tmp"] / ".claude.json").unlink()
    notif(fake_env, "m")
    assert osc9("m") in tty_bytes(fake_env) and osc9("m") + BEL not in tty_bytes(fake_env)


def test_kalmux_config_env_points_the_hook_at_another_file(fake_env):
    other = fake_env["tmp"] / "elsewhere.toml"
    other.write_text('[notify]\niterm2 = "never"\n')
    notif(fake_env, "m", extra_env={"KALMUX_CONFIG": str(other)})
    assert ESC + "]9;" not in tty_bytes(fake_env)
