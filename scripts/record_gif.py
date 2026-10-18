#!/usr/bin/env python3
"""
Record a replay as an animated GIF by stepping the real viewer in a headless browser.

    pip install playwright pillow && playwright install chromium
    python scripts/record_gif.py runs/busy_shift.json docs/demo.gif

--font-dir serves Barlow from local .woff2 files instead of Google Fonts
(useful offline). Expects barlow-latin-{400,500,600}-normal.woff2 and
barlow-condensed-latin-{500,600,700}-normal.woff2 in that folder.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from fleetplanner.viewer import build_html  # noqa: E402


# Robot colors and status colors from viewer.html.
KEY_COLORS = ["#0f9d9a", "#c2185b", "#6a9a1f", "#6c4ad6", "#b5651d", "#00838f",
              "#d7263d", "#2e8b57", "#c98a00", "#f2c300", "#e8792b", "#24507f", "#2e3438", "#ffffff"]


def font_css(font_dir: Path) -> str:
    faces = [("Barlow", w, f"barlow-latin-{w}-normal.woff2") for w in (400, 500, 600)]
    faces += [("Barlow Condensed", w, f"barlow-condensed-latin-{w}-normal.woff2") for w in (500, 600, 700)]
    return "\n".join(
        f"@font-face{{font-family:'{fam}';font-weight:{w};src:url('{(font_dir / f).resolve().as_uri()}') format('woff2');}}"
        for fam, w, f in faces)


async def record(log: dict, name: str, width: int, height: int, every: int, font_dir: Path | None) -> list[Image.Image]:
    html = build_html(log, name)
    with tempfile.TemporaryDirectory() as tmp:
        page_path = Path(tmp) / "replay.html"
        page_path.write_text(html, encoding="utf-8")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            if font_dir:
                css = font_css(font_dir)
                await page.route("https://fonts.googleapis.com/**", lambda r: r.fulfill(body=css, content_type="text/css"))
            await page.goto(page_path.as_uri())
            await page.wait_for_timeout(600)
            await page.evaluate("document.fonts.ready")
            last = len(log["frames"]) - 1
            frames = []
            for step in list(range(0, last + 1, every)) + ([last] if last % every else []):
                await page.evaluate(f"seek({step})")
                frames.append(Image.open(io.BytesIO(await page.screenshot())).convert("RGB"))
            await browser.close()
    return frames


def save_gif(frames: list[Image.Image], out: Path, scale: float, ms: int, hold_ms: int) -> None:
    if scale != 1:
        frames = [f.resize((round(f.width * scale), round(f.height * scale)), Image.LANCZOS) for f in frames]
    # One shared palette (from a strip of sampled frames) avoids color flicker between frames.
    # Key UI colors get large swatches so the quantizer keeps them exact (robots are only a few pixels).
    sample = frames[:: max(1, len(frames) // 6)]
    w, h = frames[0].width, frames[0].height
    swatch_h = 40
    strip = Image.new("RGB", (w, h * len(sample) + swatch_h * len(KEY_COLORS)))
    for i, f in enumerate(sample):
        strip.paste(f, (0, i * h))
    for i, color in enumerate(KEY_COLORS):
        strip.paste(Image.new("RGB", (w, swatch_h), color), (0, h * len(sample) + i * swatch_h))
    palette = strip.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    durations = [ms] * len(gif)
    durations[0] = durations[-1] = hold_ms
    out.parent.mkdir(parents=True, exist_ok=True)
    gif[0].save(out, save_all=True, append_images=gif[1:], duration=durations, loop=0, optimize=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=760)
    ap.add_argument("--every", type=int, default=1, help="record every Nth step")
    ap.add_argument("--scale", type=float, default=0.7)
    ap.add_argument("--ms", type=int, default=110, help="milliseconds per frame")
    ap.add_argument("--hold-ms", type=int, default=1500, help="pause on the first and last frame")
    ap.add_argument("--font-dir", type=Path)
    args = ap.parse_args()

    log = json.loads(Path(args.log).read_text())
    frames = asyncio.run(record(log, Path(args.log).stem, args.width, args.height, args.every, args.font_dir))
    save_gif(frames, Path(args.out), args.scale, args.ms, args.hold_ms)
    print(f"wrote {args.out}: {len(frames)} frames, {Path(args.out).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
