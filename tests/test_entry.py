"""The console-script entry point: `kalmux statusline` must stay on the cheap path."""
import json
import subprocess
import sys

from conftest import SRC


def test_statusline_takes_the_fast_path_without_loading_the_command_layer(tmp_path):
    """Claude Code re-runs this on every refresh, so argparse, tmserver and tmactions must stay unimported."""
    state = tmp_path / "state"
    (state / "status").mkdir(parents=True)
    (state / "statusline.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "cat >/dev/null"}}))
    code = ("import sys; from kalmux._entry import main; rc = main(['statusline']); "
            "heavy = sorted(m for m in ('argparse', 'kalmux.cli', 'kalmux.tmserver', 'kalmux.tmactions') if m in sys.modules); "
            "print(rc, heavy)")
    payload = json.dumps({"session_id": "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d", "context_window": {"used_percentage": 7}})
    out = subprocess.run([sys.executable, "-c", code], input=payload, text=True, capture_output=True, check=True,
                         cwd=str(SRC), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "KALMUX_STATE_DIR": str(state)})
    assert out.stdout.strip() == "0 []"
    assert json.loads((state / "status" / "0f7b1c2d-3e4f-4a5b-8c9d-0e1f2a3b4c5d.json").read_text())["context_pct"] == 7


def test_every_other_command_goes_to_the_command_layer(monkeypatch):
    from kalmux import _entry
    seen = []
    monkeypatch.setattr("kalmux.cli.main", lambda argv: seen.append(argv) or 3)
    assert _entry.main(["ls", "--json"]) == 3
    assert seen == [["ls", "--json"]]


def test_python_dash_m_kalmux_is_the_same_command():
    out = subprocess.run([sys.executable, "-m", "kalmux", "--version"], capture_output=True, text=True,
                         cwd=str(SRC), check=True)
    assert out.stdout.strip().startswith("kalmux ")
