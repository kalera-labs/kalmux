#!/usr/bin/env python3
"""Record the README animation from the mock server, so no real session name is ever on screen.

    python3 scripts/dev/mock_server.py 47401          # in one shell
    python3 scripts/dev/record_demo.py 47401 <token>  # in another; writes assets/toolbelt.gif

Frames come from headless Chrome (`channel="chrome"`, so nothing is downloaded), the timeline is
driven through the mock's /api/_mock route, and ffmpeg turns the frames into a palette-optimised GIF.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "assets" / "toolbelt.gif"
FPS = 6
WIDTH, HEIGHT = 400, 820


def patch(port: int, token: str, name: str, **fields) -> None:
    body = json.dumps({"name": name, "patch": fields}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/_mock", data=body,
                                 headers={"X-TM-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 47401
    token = argv[2]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs playwright: uv run --no-project --with playwright python scripts/dev/record_demo.py ...",
              file=sys.stderr)
        return 2
    if not shutil.which("ffmpeg"):
        print("needs ffmpeg (brew install ffmpeg)", file=sys.stderr)
        return 2

    frames = Path(tempfile.mkdtemp(prefix="kalmux-frames-"))
    n = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT},
                                   device_scale_factor=1, color_scheme="dark").new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1200)

        def hold(seconds: float) -> None:
            nonlocal n
            for _ in range(max(1, round(seconds * FPS))):
                page.screenshot(path=str(frames / f"{n:04d}.png"))
                n += 1
                page.wait_for_timeout(int(1000 / FPS))

        hold(1.6)                                                   # the wall, as you find it
        patch(port, token, "api", state="waiting", detail="Allow Bash(pytest -q)?")
        hold(2.6)                                                   # api turns amber and jumps to the top
        page.click("#gowait")                                       # one click takes you to that tab
        hold(2.0)
        patch(port, token, "web", state="idle", detail="✓ Done: dark mode ships behind a flag")
        hold(2.4)                                                   # web finishes and settles down the list
        page.click("#gone-h")                                       # the sessions that died, still resumable
        page.locator("#gone-list").scroll_into_view_if_needed()
        hold(3.0)
        browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    palette = frames / "palette.png"
    common = ["-v", "error", "-framerate", str(FPS), "-i", str(frames / "%04d.png")]
    subprocess.run(["ffmpeg", *common, "-vf", "palettegen=stats_mode=diff", "-y", str(palette)], check=True)
    subprocess.run(["ffmpeg", *common, "-i", str(palette), "-lavfi",
                    "paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle", "-loop", "0", "-y", str(OUT)],
                   check=True)
    shutil.rmtree(frames, ignore_errors=True)
    print(f"{OUT}  {OUT.stat().st_size // 1024} KB  ({n} frames at {FPS} fps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
