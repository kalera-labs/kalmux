#!/usr/bin/env python3
"""Render assets/social-preview.png, the 1280x640 card GitHub shows when the repo is shared.

    python3 scripts/dev/social_card.py

Headless Chrome renders an HTML card that embeds the first frame of assets/toolbelt.gif, so the
picture always matches whatever the recording currently shows.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GIF = ROOT / "assets" / "toolbelt.gif"
OUT = ROOT / "assets" / "social-preview.png"

CARD = """<!doctype html><meta charset="utf-8">
<style>
  * {{ margin: 0; box-sizing: border-box; }}
  body {{ width: 1280px; height: 640px; display: flex; align-items: center; gap: 64px;
         padding: 0 72px; background: #0b0d10; color: #e9edf2; overflow: hidden;
         font: 400 20px/1.5 -apple-system, "SF Pro Text", Helvetica, Arial, sans-serif; }}
  h1 {{ font-size: 84px; letter-spacing: -2px; font-weight: 700; }}
  .tag {{ font-size: 30px; line-height: 1.35; margin-top: 18px; color: #b9c2cd; max-width: 15em; }}
  .row {{ display: flex; gap: 12px; margin-top: 34px; flex-wrap: wrap; }}
  .chip {{ border: 1px solid #2a3038; border-radius: 999px; padding: 8px 16px; font-size: 19px; color: #95a1ae; }}
  .shot {{ width: 420px; height: 760px; border-radius: 18px; border: 1px solid #232a33;
          box-shadow: 0 40px 90px rgba(0,0,0,.6); transform: rotate(-3deg) translateY(86px); flex: none; }}
  .by {{ margin-top: 40px; font-size: 20px; color: #6d7885; }}
</style>
<div>
  <h1>Kalmux</h1>
  <div class="tag">Ten Claude Code agents in tmux, one place to watch them all.</div>
  <div class="row"><span class="chip">iTerm2 toolbelt</span><span class="chip">tmux</span>
    <span class="chip">no runtime deps</span><span class="chip">MIT</span></div>
  <div class="by">github.com/kalera-labs/kalmux</div>
</div>
<img class="shot" src="data:image/png;base64,{shot}">
"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("needs playwright: uv run --no-project --with playwright python scripts/dev/social_card.py", file=sys.stderr)
        return 2
    if not GIF.exists():
        print(f"{GIF} is missing; run scripts/dev/record_demo.py first", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="kalmux-card-"))
    frame = tmp / "frame.png"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(GIF), "-frames:v", "1", "-y", str(frame)], check=True)
    html = tmp / "card.html"
    html.write_text(CARD.format(shot=base64.b64encode(frame.read_bytes()).decode()))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context(viewport={"width": 1280, "height": 640}, device_scale_factor=1).new_page()
        page.goto(html.as_uri(), wait_until="networkidle")
        page.screenshot(path=str(OUT))
        browser.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{OUT}  {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
