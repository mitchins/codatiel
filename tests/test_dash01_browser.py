"""DASH-01 browser smoke: real Chromium drives the major views.

Skips when Playwright/Chromium is unavailable (CI without browsers).
Locally it starts the dashboard in-thread against the deterministic
rich fixture, visits every major view, asserts visible content, and
writes screenshots for the PR evidence bundle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_dash01 import _serve, rich_state  # noqa: F401

playwright_mod = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def shots(tmp_path: Path) -> Path:
    out = tmp_path / "shots"
    out.mkdir()
    return out


def _launch():
    from playwright.sync_api import sync_playwright
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        pytest.skip(f"chromium unavailable: {exc}")
    return pw, browser


PAGES = [("/", "overview"), ("/repos", "repos"),
         ("/pr/o/r/1", "pr"), ("/spend", "spend"),
         ("/efficiency", "efficiency"), ("/ops", "ops"),
         ("/settings", "settings")]


@pytest.mark.parametrize("path,name", PAGES)
def test_browser_view_renders(rich_state, shots, path, name):
    pw, browser = _launch()
    server, thread, port = _serve(rich_state)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{port}{path}",
                  wait_until="networkidle", timeout=15000)
        body = page.content()
        assert "Codatiel operator dashboard" in body, path
        assert "Traceback" not in body, path
        page.screenshot(path=str(shots / f"dash01-{name}.png"))
        texts = {"overview": "Poller status",
                 "repos": "Repositories",
                 "pr": "o/r#1",
                 "spend": "Codatiel ledger only",
                 "efficiency": "Cache hit",
                 "ops": "Recent ingestions",
                 "settings": "Save settings"}
        assert texts[name] in body, (path, texts[name])
    finally:
        browser.close()
        pw.stop()
        server.should_exit = True
        thread.join(timeout=10)
