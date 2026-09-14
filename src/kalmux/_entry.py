"""The console-script entry point.

`kalmux statusline` is re-run by Claude Code on every status-line refresh (and the in-flight one is
cancelled), so it gets a fast path that skips argparse, tmserver (http.client, ssl, email) and
tmactions. Everything else falls through to the full command line.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["statusline"]:
        from . import tmstatusline
        from .tmsetup import state_dir
        return tmstatusline.run_tap(state_dir())
    from .cli import main as run
    return run(argv)
