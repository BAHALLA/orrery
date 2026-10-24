"""Browser smoke test for the Studio canvas: what jsdom can't check.

React Flow needs real layout to measure nodes and its pane, so the canvas's
fit and its layout direction are checked here, in Chrome, against the built
console (``web/dist``) served with the server's real CSP and canned
``/me``, ``/studio/*`` answers. No Orrery server, model or network needed.

Checks:

1. 1440x900: the canvas is taller than wide, so the demo lays out top to
   bottom (handles on top) and fits at zoom 1.
2. 1920x1080: wide, so left to right, zoom 1.
3. Drag a node at 1440, widen the window to 1920: the hand-placed positions
   survive the direction flip (no relayout), and Tidy then lays it out again.
   A zoom from the controls' button (pressed from the keyboard) survives a
   resize, and their fit button fits at zoom 1, like a load.
4. 1280x800: a graph loaded over another one is fitted to its own measured
   nodes (a 2-node then a 3-node pipeline: every node in the pane, equal
   margins); a 6-node pipeline import overflows even at the minimum zoom
   (0.75), and the first node is in view, not cut off by centring.
5. No Content-Security-Policy violation anywhere.

Run from the repo root (``make web-build`` first; Google Chrome, or pass
``--browser chromium`` after ``playwright install chromium``)::

    uv run --with playwright==1.55.0 python web/scripts/studio_smoke.py

Exits non-zero on the first failed check.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright  # ty: ignore[unresolved-import]

WEB = Path(__file__).resolve().parent.parent

# core/orrery_core/serving/security_headers.py build_csp(), no extra origins.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

REGISTRY = {
    "mcp": [
        {
            "id": "tirraflow",
            "url": "https://tirraflow.com/mcp",
            "transport": "streamable_http",
            "private_network": False,
            "tools": [
                {"name": "search_articles", "classification": "read_only", "pinned": False},
                {"name": "get_article", "classification": "read_only", "pinned": False},
            ],
        }
    ],
    "a2a": [
        {
            "id": "demo-writer",
            "card_url": "https://demo-writer.studio.internal:8443/.well-known/agent-card.json",
            "skills": ["draft"],
            "effect": "read_only",
            "private_network": True,
            "authenticated": False,
            "description": "Drafts release notes.",
            "card": {
                "digest": "0" * 64,
                "pinned_by": "first_use",
                "resolved": True,
                "fetched_at": time.time(),
                "error": None,
                "skills": [{"id": "draft", "name": "Draft", "description": "Drafts notes."}],
            },
        }
    ],
    "catalog": [],
}


def pipeline(n: int, name: str) -> dict:
    """A spec of `n` llm_agent nodes in a line: `<name>_1` → … → `<name>_<n>`."""
    return {
        "spec_version": 1,
        "name": name,
        "pattern": "pipeline",
        "nodes": [
            {"id": f"{name}_{i}", "kind": "llm_agent", "instruction": f"Step {i}.", "tools": []}
            for i in range(1, n + 1)
        ],
        "edges": [{"from": f"{name}_{i}", "to": f"{name}_{i + 1}"} for i in range(1, n)],
    }


class Handler(SimpleHTTPRequestHandler):
    """web/dist, plus the API answers the Studio needs to draw."""

    schema: bytes = b"{}"

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def _json(self, body: object, status: int = 200) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/me":
            return self._json(
                {
                    "subject": "smoke",
                    "role": "admin",
                    "autonomy_level": "L2",
                    "model_provider": "gemini",
                    "model_name": "gemini-2.5-flash",
                    "self_test_available": False,
                }
            )
        if path == "/sessions":
            return self._json({"sessions": []})
        if path == "/confirmations/pending":
            return self._json({"pending": None})
        if path == "/console-config.js":
            data = b"window.__ORRERY_CONSOLE_CONFIG__ = {};\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        if path == "/studio/schema":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.schema)))
            self.end_headers()
            self.wfile.write(self.schema)
            return None
        if path == "/studio/registry":
            return self._json(REGISTRY)
        if not (Path(self.directory) / path.lstrip("/")).is_file():
            self.path = "/index.html"  # the console's own routes
        return super().do_GET()


def serve(dist: Path) -> ThreadingHTTPServer:
    Handler.schema = (WEB / "src/studio/fixtures/studio-schema.json").read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(dist)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def token() -> str:
    def part(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"sub": "smoke", "roles": ["admin"], "exp": int(time.time()) + 3600}
    return f"{part({'alg': 'HS256'})}.{part(claims)}.sig"


class Failed(Exception):
    pass


def check(ok: bool, what: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        raise Failed(what)


async def canvas(page: Page) -> dict:
    """The viewport's zoom, the handles' side, and each node's box on screen."""
    return await page.evaluate(
        """() => {
          const t = document.querySelector('.react-flow__viewport').style.transform;
          const pane = document.querySelector('.react-flow').getBoundingClientRect();
          const nodes = {};
          for (const n of document.querySelectorAll('.react-flow__node')) {
            const r = n.getBoundingClientRect();
            nodes[n.dataset.id] = {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
                                   transform: n.style.transform};
          }
          return {
            zoom: Number(/scale\\(([\\d.]+)\\)/.exec(t)?.[1]),
            top: !!document.querySelector('.react-flow__handle-top'),
            left: !!document.querySelector('.react-flow__handle-left'),
            pane: {left: pane.left, top: pane.top, right: pane.right, bottom: pane.bottom},
            nodes,
          };
        }"""
    )


async def open_studio(browser, base: str, width: int, height: int, csp: list[str]) -> Page:
    context = await browser.new_context(viewport={"width": width, "height": height})
    await context.add_init_script(
        f"localStorage.setItem('orrery.console.token', {json.dumps(token())})"
    )
    page = await context.new_page()
    page.on(
        "console",
        lambda m: csp.append(m.text) if "Content Security Policy" in m.text else None,
    )
    await page.goto(base)
    await page.get_by_role("button", name="Studio").click()
    await page.get_by_text("Compose a run").wait_for()
    return page


async def load(page: Page, spec: dict) -> None:
    await page.get_by_role("button", name="Import", exact=True).click()
    await page.get_by_label("Paste a spec").fill(json.dumps(spec))
    await page.get_by_role("button", name="Load onto the canvas").click()
    await settle(page)


def margins(c: dict) -> dict[str, float]:
    """Space between the graph's outer nodes and the pane's edges, in pixels."""
    boxes = c["nodes"].values()
    pane = c["pane"]
    return {
        "left": min(b["left"] for b in boxes) - pane["left"],
        "right": pane["right"] - max(b["right"] for b in boxes),
        "top": min(b["top"] for b in boxes) - pane["top"],
        "bottom": pane["bottom"] - max(b["bottom"] for b in boxes),
    }


async def settle(page: Page) -> None:
    await page.wait_for_timeout(600)  # the 200 ms fit animation, and a frame or two


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dist", type=Path, default=WEB / "dist")
    parser.add_argument("--browser", choices=["chrome", "chromium"], default="chrome")
    args = parser.parse_args()
    if not (args.dist / "index.html").is_file():
        print(f"No build in {args.dist}: run `make web-build` first.", file=sys.stderr)
        return 2

    server = serve(args.dist)
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    csp: list[str] = []
    async with async_playwright() as p:
        launch = {"channel": "chrome"} if args.browser == "chrome" else {}
        browser = await p.chromium.launch(headless=True, **launch)
        try:
            print("1440x900: tall canvas")
            page = await open_studio(browser, base, 1440, 900, csp)
            await page.get_by_role("button", name="Research → writer → critic").click()
            await settle(page)
            c = await canvas(page)
            check(c["top"] and not c["left"], "top-to-bottom layout (handles on top)")
            check(c["zoom"] == 1, f"fits at zoom 1 (got {c['zoom']})")

            print("1440 -> 1920 after a drag: hand-placed positions survive")
            box = c["nodes"]["writer"]
            x, y = (box["left"] + box["right"]) / 2, box["top"] + 20
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x + 72, y, steps=5)
            await page.mouse.move(x + 144, y, steps=5)
            await page.mouse.up()
            await settle(page)
            before = {k: v["transform"] for k, v in (await canvas(page))["nodes"].items()}
            check(before["writer"] != c["nodes"]["writer"]["transform"], "writer moved")
            await page.set_viewport_size({"width": 1920, "height": 1080})
            await settle(page)
            after = await canvas(page)
            check(
                {k: v["transform"] for k, v in after["nodes"].items()} == before,
                "no relayout on the direction flip",
            )
            check(after["top"], "handles still match the positions (top to bottom)")
            await page.get_by_role("button", name="Tidy").click()
            await settle(page)
            check((await canvas(page))["left"], "Tidy lays it out left to right")
            await page.context.close()

            print("1920x1080: wide canvas")
            page = await open_studio(browser, base, 1920, 1080, csp)
            await page.get_by_role("button", name="Research → writer → critic").click()
            await settle(page)
            c = await canvas(page)
            check(c["left"] and not c["top"], "left-to-right layout")
            check(c["zoom"] == 1, f"fits at zoom 1 (got {c['zoom']})")

            print("1920: a zoom from the controls (by keyboard) survives a resize")
            await page.get_by_role("button", name="Zoom in").focus()
            await page.keyboard.press("Enter")
            await settle(page)
            zoomed = (await canvas(page))["zoom"]
            check(zoomed > 1, f"zoomed in (got {zoomed})")
            await page.set_viewport_size({"width": 1920, "height": 1000})  # still wide
            await settle(page)
            c = await canvas(page)
            check(c["zoom"] == zoomed, f"the zoom is kept (got {c['zoom']})")
            await page.get_by_role("button", name="Fit the composition in view").click()
            await settle(page)
            c = await canvas(page)
            check(
                c["zoom"] == 1, f"the fit button fits at zoom 1, as a load does (got {c['zoom']})"
            )
            await page.context.close()

            print("1280x800: a graph loaded over another is fitted to its own nodes")
            page = await open_studio(browser, base, 1280, 800, csp)
            await load(page, pipeline(2, "a"))
            await load(page, pipeline(3, "b"))
            c = await canvas(page)
            m = margins(c)
            check(sorted(c["nodes"]) == ["b_1", "b_2", "b_3"], "the second graph is on the canvas")
            check(min(m.values()) >= 0, f"every node inside the pane {m}")
            check(
                abs(m["left"] - m["right"]) <= 2 and abs(m["top"] - m["bottom"]) <= 2,
                "centred: equal margins",
            )

            # Again with the undo banner already up: the pane keeps its size,
            # so only waiting for the new nodes' sizes gets this one right.
            await load(page, pipeline(2, "c"))
            c = await canvas(page)
            m = margins(c)
            check(sorted(c["nodes"]) == ["c_1", "c_2"], "a third graph is on the canvas")
            check(
                min(m.values()) >= 0
                and abs(m["left"] - m["right"]) <= 2
                and abs(m["top"] - m["bottom"]) <= 2,
                f"centred too {m}",
            )

            print("1280x800: a 6-node import overflows")
            await load(page, pipeline(6, "step"))
            c = await canvas(page)
            first = c["nodes"]["step_1"]
            check(c["zoom"] == 0.75, f"clamped at zoom 0.75 (got {c['zoom']})")
            check(
                first["top"] >= c["pane"]["top"] and first["left"] >= c["pane"]["left"],
                "the first node is in view, not cut off",
            )
            await page.context.close()

            print("CSP")
            check(not csp, f"no violations ({len(csp)})")
        except Failed:
            return 1
        finally:
            await browser.close()
            server.shutdown()
    print("studio smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
