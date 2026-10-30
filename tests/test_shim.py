"""bin/kalmux must survive an old `python3` long enough to hand over to a 3.11+ interpreter."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys

import pytest

from conftest import BIN, write_exec


@pytest.fixture(scope="module")
def shim():
    loader = importlib.machinery.SourceFileLoader("kalmux_shim", str(BIN / "kalmux"))
    spec = importlib.util.spec_from_loader("kalmux_shim", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def fake_python(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    return str(write_exec(directory / name, "#!/bin/sh\n"))


def test_the_named_interpreter_is_the_only_candidate(shim, tmp_path):
    named = fake_python(tmp_path, "mypython")
    assert list(shim.modern_pythons({"KALMUX_PYTHON": named, "PATH": "/usr/bin"}, str(tmp_path))) == [named]


def test_well_known_places_and_PATH_are_searched_newest_first(shim, tmp_path):
    local = fake_python(tmp_path / ".local" / "bin", "python3.12")
    onpath = fake_python(tmp_path / "pathdir", "python3.13")
    uv = fake_python(tmp_path / ".local" / "share" / "uv" / "python" / "cpython-3.11.9-macos-aarch64-none" / "bin",
                     "python3.11")
    found = list(shim.modern_pythons({"PATH": f"/nonexistent::{tmp_path / 'pathdir'}"}, str(tmp_path), well_known=()))
    assert [p for p in found if p in (local, onpath, uv)] == [onpath, local, uv]
    assert all(os.path.isabs(p) for p in found)                    # an empty PATH entry never means "here"
    assert len(found) == len(set(found))


def test_hand_over_execs_the_first_usable_interpreter(shim, tmp_path):
    good = fake_python(tmp_path, "python3.13")
    calls, env = [], {"KALMUX_PYTHON": good}
    shim.hand_over(env, ["/x/kalmux", "ls", "--json"], lambda path, argv: calls.append((path, argv)), (3, 9, 6))
    assert calls == [(good, [good, "/x/kalmux", "ls", "--json"])]
    assert env[shim.GUARD] == "1"


def test_hand_over_skips_candidates_that_cannot_be_exec_ed(shim, tmp_path):
    bad, good = fake_python(tmp_path, "python3.14"), fake_python(tmp_path, "python3.13")
    seen = []

    def execv(path, argv):
        seen.append(path)
        if path == bad:
            raise OSError("bad CPU type")

    shim.hand_over({"PATH": str(tmp_path)}, ["/x/kalmux"], execv, (3, 9, 6), home=str(tmp_path), well_known=())
    assert seen == [bad, good]


def test_hand_over_without_any_modern_python_prints_one_clear_error(shim, tmp_path):
    with pytest.raises(SystemExit) as stop:
        shim.hand_over({"PATH": ""}, ["/x/kalmux"], lambda *a: None, (3, 9, 6), home=str(tmp_path), well_known=())
    message = str(stop.value)
    assert "3.9.6" in message and "3.11" in message and "KALMUX_PYTHON" in message
    assert "brew install python@3.13" in message


def test_hand_over_refuses_a_named_interpreter_that_does_not_exist(shim, tmp_path):
    with pytest.raises(SystemExit) as stop:
        shim.hand_over({"KALMUX_PYTHON": str(tmp_path / "nope")}, ["/x/kalmux"], lambda *a: None, (3, 9, 6))
    assert "KALMUX_PYTHON" in str(stop.value) and str(tmp_path / "nope") in str(stop.value)


def test_hand_over_does_not_loop_when_the_new_interpreter_is_old_too(shim):
    with pytest.raises(SystemExit) as stop:
        shim.hand_over({shim.GUARD: "1"}, ["/x/kalmux"], lambda *a: pytest.fail("must not exec again"), (3, 9, 6))
    assert "3.9.6" in str(stop.value)


def old_python():
    for candidate in ("/usr/bin/python3", "/usr/bin/python3.9", "/usr/bin/python3.10"):
        if os.path.exists(candidate):
            out = subprocess.run([candidate, "-c", "import sys; print(sys.version_info < (3, 11))"],
                                 check=False, capture_output=True, text=True)
            if out.stdout.strip() == "True":
                return candidate
    return None


@pytest.mark.skipif(old_python() is None, reason="needs a Python older than 3.11 (macOS ships 3.9)")
def test_an_old_python3_reruns_the_command_under_a_modern_one(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "KALMUX_PYTHON": sys.executable,
           "KALMUX_STATE_DIR": str(tmp_path / "state")}
    out = subprocess.run([old_python(), str(BIN / "kalmux"), "--version"], check=False, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("kalmux ")
    bad = subprocess.run([old_python(), str(BIN / "kalmux"), "--version"], check=False, capture_output=True, text=True,
                         env={**env, "KALMUX_PYTHON": str(tmp_path / "nope")})
    assert bad.returncode == 1 and "KALMUX_PYTHON" in bad.stderr and "Traceback" not in bad.stderr
