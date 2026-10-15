"""Record the README GIF: the console against ``scripted_server.py``.

Diagnose a lagging consumer → see the live trace → ask for the fix → approve
it. Frames are screenshots taken while the real UI works; they are stitched
into a GIF with Pillow.

    make demo-gif          # or, with the scripted server already running:
    uv run --with pillow --with playwright==1.55.0 python scripts/demo/record_gif.py

Needs Google Chrome (``channel="chrome"``); ``playwright install chromium`` and
``--browser chromium`` work too.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import time
from pathlib import Path

import jwt

# Recording-only dependencies, supplied by `uv run --with` (see make demo-gif),
# deliberately not workspace dependencies.
from PIL import Image  # ty: ignore[unresolved-import]
from playwright.async_api import Page, async_playwright  # ty: ignore[unresolved-import]
from scripted_server import DEMO_SECRET  # type: ignore[import-not-found]  # same directory

FRAME_SECONDS = 0.35


class Recorder:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.frames: list[tuple[bytes, int]] = []  # (png, duration ms)

    async def snap(self, hold_ms: int = int(FRAME_SECONDS * 1000)) -> None:
        self.frames.append((await self.page.screenshot(), hold_ms))

    async def until(self, locator, *, timeout: float = 60.0) -> None:
        """Keep filming until *locator* is visible."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.snap()
            if await locator.count() and await locator.first.is_visible():
                return
            await asyncio.sleep(FRAME_SECONDS)
        raise TimeoutError(f"never saw {locator}")

    async def settle(self, locator) -> None:
        """Film until the turn is over (Send is back), then show *locator*."""
        await self.until(self.page.get_by_role("button", name="Send", exact=True))
        await locator.first.scroll_into_view_if_needed()
        await asyncio.sleep(0.6)  # the transcript scrolls smoothly

    async def type(self, text: str) -> None:
        box = self.page.get_by_role("textbox", name="Message")
        await box.click()
        for i in range(0, len(text), 6):
            await box.press_sequentially(text[i : i + 6], delay=15)
            await self.snap(120)


def token() -> str:
    return jwt.encode(
        {"sub": "ana", "email": "ana@example.com", "roles": ["operator"], "exp": time.time() + 900},
        DEMO_SECRET,
        algorithm="HS256",
    )


async def record(base: str, browser_name: str) -> list[tuple[bytes, int]]:
    async with async_playwright() as p:
        launch = {"channel": "chrome"} if browser_name == "chrome" else {}
        browser = await p.chromium.launch(headless=True, **launch)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800}, color_scheme="light"
        )
        await ctx.add_init_script(f"localStorage.setItem('orrery.console.token', {token()!r});")
        page = await ctx.new_page()
        await page.goto(base)
        await page.get_by_role("textbox", name="Message").wait_for()
        await page.wait_for_timeout(600)
        await page.get_by_role("button", name="New chat", exact=True).click()

        rec = Recorder(page)
        await rec.snap(1600)  # the welcome screen

        await rec.type("Why is the orders consumer lagging?")
        await page.get_by_role("button", name="Send", exact=True).click()
        await rec.until(page.get_by_text("Want me to?"))
        await rec.settle(page.get_by_text("Want me to?"))
        await rec.snap(2600)  # read the diagnosis

        await rec.type("Yes, raise the limit and restart it")
        await page.get_by_role("button", name="Send", exact=True).click()
        await rec.until(page.get_by_role("alertdialog", name="Pending confirmation"))
        await rec.snap(2400)  # the approval card

        await page.get_by_role("button", name="Approve", exact=True).click()
        await rec.until(page.get_by_text("stopped restarting"))
        await rec.settle(page.get_by_text("stopped restarting"))
        await rec.snap(3500)  # done
        await browser.close()
        return rec.frames


def write_gif(frames: list[tuple[bytes, int]], out: Path, width: int) -> None:
    images = []
    for png, _ in frames:
        img = Image.open(io.BytesIO(png)).convert("RGB")
        img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.LANCZOS)
        images.append(img.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    out.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        out,
        save_all=True,
        append_images=images[1:],
        duration=[d for _, d in frames],
        loop=0,
        optimize=True,
        disposal=1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--out", type=Path, default=Path("docs/images/console-demo.gif"))
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--browser", choices=["chrome", "chromium"], default="chrome")
    args = parser.parse_args()

    frames = asyncio.run(record(args.base, args.browser))
    write_gif(frames, args.out, args.width)
    print(f"▶ {args.out}: {len(frames)} frames, {args.out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
