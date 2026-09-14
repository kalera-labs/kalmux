"""Unit tests for lib/tmconfig.py."""
import pytest
import tmconfig


def test_config_path_precedence(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert tmconfig.config_path(env) == tmp_path / ".config" / "kalmux" / "config.toml"
    assert tmconfig.legacy_config_paths(env) == [tmp_path / ".config" / "kmux" / "config.toml"]
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    assert tmconfig.config_path(env) == tmp_path / "xdg" / "kalmux" / "config.toml"
    assert tmconfig.legacy_config_paths(env) == [tmp_path / "xdg" / "kmux" / "config.toml"]
    env["KALMUX_CONFIG"] = str(tmp_path / "custom.toml")
    assert tmconfig.config_path(env) == tmp_path / "custom.toml"


def test_missing_file_gives_defaults_without_errors(tmp_path):
    cfg = tmconfig.load_config(tmp_path / "none.toml")
    assert cfg["claude"] == tmconfig.DEFAULTS["claude"]
    assert cfg["tombstones"]["keep_days"] == 30
    assert cfg["_errors"] == []
    assert "defaults in effect" in tmconfig.describe(cfg)


def test_ensure_config_writes_template_once_and_template_parses(tmp_path):
    path = tmp_path / "cfg" / "config.toml"
    assert tmconfig.ensure_config(path) is True
    assert tmconfig.ensure_config(path) is False
    assert path.read_text() == tmconfig.CONFIG_TEMPLATE
    cfg = tmconfig.load_config(path)
    assert cfg["_errors"] == []
    assert cfg["claude"]["resume"] == "claude --resume {session_id}"


def test_user_values_are_honoured(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[claude]\nnew = "claude --dangerously-skip-permissions"\n'
                    'resume = "claude --resume {session_id} --dangerously-skip-permissions"\nresume_mode = "run"\n'
                    "[tombstones]\nkeep_days = 7\n")
    cfg = tmconfig.load_config(path)
    assert cfg["_errors"] == []
    assert cfg["claude"]["new"] == "claude --dangerously-skip-permissions"
    assert cfg["claude"]["resume_mode"] == "run"
    assert cfg["tombstones"]["keep_days"] == 7
    assert tmconfig.render_resume(cfg, "7c9e1f20-4a3b-4d5e-9f01-2b3c4d5e6f70") == \
        "claude --resume 7c9e1f20-4a3b-4d5e-9f01-2b3c4d5e6f70 --dangerously-skip-permissions"


@pytest.mark.parametrize("body,expect", [
    ('[claude]\nresume = "claude --continue"\n', "must contain {session_id}"),
    ('[claude]\nnew = "claude\\nrm -rf /"\n', "one printable line"),
    ('[claude]\nnew = 42\n', "expected a string"),
    ('[claude]\nresume_mode = "maybe"\n', "resume_mode"),
    ("[tombstones]\nkeep_days = 0\n", "keep_days"),
    ("[tombstones]\nkeep_days = true\n", "keep_days"),
    ('claude = "not a table"\n', "expected a table"),
    ("this is not toml =\n", "config.toml"),
])
def test_bad_values_fall_back_to_defaults_and_report(tmp_path, body, expect):
    path = tmp_path / "config.toml"
    path.write_text(body)
    cfg = tmconfig.load_config(path)
    assert cfg["claude"] == tmconfig.DEFAULTS["claude"]
    assert cfg["tombstones"] == tmconfig.DEFAULTS["tombstones"]
    assert any(expect in e for e in cfg["_errors"]), cfg["_errors"]
    assert "!" in tmconfig.describe(cfg)


def test_overlong_command_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[claude]\nnew = "%s"\n' % ("x" * 600))
    cfg = tmconfig.load_config(path)
    assert cfg["claude"]["new"] == "claude"
    assert cfg["_errors"]


def test_render_resume_rejects_bad_ids():
    cfg = tmconfig.load_config("/nonexistent/config.toml")
    for bad in ("", "abc", "7c9e1f20-4a3b-4d5e-9f01-2b3c4d5e6f70; rm -rf /", "7C9E1F20-4A3B-4D5E-9F01-2B3C4D5E6F70"):
        with pytest.raises(ValueError):
            tmconfig.render_resume(cfg, bad)


def test_defaults_are_not_mutated_by_loading(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[claude]\nnew = "other"\n')
    tmconfig.load_config(path)
    assert tmconfig.DEFAULTS["claude"]["new"] == "claude"


def test_ensure_config_migrates_the_file_written_before_the_rename(tmp_path):
    env = {"HOME": str(tmp_path)}
    legacy = tmp_path / ".config" / "kmux" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[claude]\nnew = "claude --mine"\n')
    assert tmconfig.ensure_config(env=env) is True
    new = tmconfig.config_path(env)
    assert new == tmp_path / ".config" / "kalmux" / "config.toml"
    assert new.read_text() == '[claude]\nnew = "claude --mine"\n'    # the user's file, not the template
    assert not legacy.exists()
    assert oct(new.parent.stat().st_mode)[-3:] == "700"
    assert tmconfig.ensure_config(env=env) is False                  # already there: nothing to do
    assert tmconfig.load_config(env=env)["claude"]["new"] == "claude --mine"


def test_ensure_config_leaves_the_legacy_file_alone_when_a_new_one_exists(tmp_path):
    env = {"HOME": str(tmp_path)}
    legacy = tmp_path / ".config" / "kmux" / "config.toml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('[claude]\nnew = "old"\n')
    new = tmconfig.config_path(env)
    new.parent.mkdir(parents=True)
    new.write_text('[claude]\nnew = "current"\n')
    assert tmconfig.ensure_config(env=env) is False
    assert legacy.read_text() == '[claude]\nnew = "old"\n' and new.read_text() == '[claude]\nnew = "current"\n'
