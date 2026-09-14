import importlib.machinery
import importlib.util
import os
import pathlib
import stat
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def load_tm():
    """Load bin/kalmux (no .py suffix) as a module named `tm`."""
    loader = importlib.machinery.SourceFileLoader("tm", str(BIN / "kalmux"))
    spec = importlib.util.spec_from_loader("tm", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tm"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def tm():
    return load_tm()


def write_exec(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """A PATH with a fake `tmux` that logs its argv and answers display/show queries,
    a fake tty (plain file) and a fake original cc-status that records stdin."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "tmux.log"
    tty = tmp_path / "pane.tty"
    tty.write_text("")
    orig_log = tmp_path / "orig.log"
    state = tmp_path / "state"
    write_exec(
        fakebin / "tmux",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$FAKE_TMUX_LOG"\n'
        '[ -n "${FAKE_TMUX_FAIL:-}" ] && exit 1\n'
        'case "$1" in\n'
        "  display) printf '%s\\037%s\\037%s\\037%s\\037%s\\n' \"${FAKE_TTY}\" \"${FAKE_ATTACHED:-1}\""
        ' "${FAKE_TM_COLOR:-}" "${FAKE_CC_STATE:-}" "${FAKE_SESSION:-fakesess}";;\n'
        "esac\n"
        "exit 0\n",
    )
    orig = write_exec(
        tmp_path / "cc-status-orig",
        "#!/bin/bash\n"
        '{ echo "ARGS: $*"; echo "STDIN: $(cat)"; } >> "$FAKE_ORIG_LOG"\n'
        "exit 0\n",
    )
    env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMUX_PANE": "%7",
        "TMUX": "/tmp/tmux-501/default,1,0",
        "FAKE_TMUX_LOG": str(log),
        "FAKE_TTY": str(tty),
        "FAKE_ORIG_LOG": str(orig_log),
        "CC_STATUS_ORIG": str(orig),
        "KALMUX_STATE_DIR": str(state),
    }
    return {"env": env, "log": log, "tty": tty, "orig_log": orig_log, "tmp": tmp_path,
            "state": state, "trace": state / "trace"}


@pytest.fixture
def ptty():
    """A real pseudo-terminal: (slave path under /dev, read-what-was-written). write_tty() refuses anything that
    is not a character device under /dev, so tests that check replayed escape sequences need a real pty."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    os.set_blocking(master, False)

    def read() -> str:
        time.sleep(0.02)
        chunks = []
        while True:
            try:
                chunks.append(os.read(master, 65536))
            except BlockingIOError:
                break
        return b"".join(chunks).decode("utf-8", "replace")
    yield path, read
    os.close(master)
    os.close(slave)
