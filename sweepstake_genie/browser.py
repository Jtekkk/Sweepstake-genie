"""
Playwright browser lifecycle manager.

Usage::

    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.goto("https://example.com")
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

if TYPE_CHECKING:
    from types import TracebackType

# ── Constants ─────────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_VIEWPORT = {"width": 1280, "height": 720}

_LOCALE = "en-US"

# Extra HTTP headers that make requests look like a real browser.
# Note: Sec-Ch-Ua / Sec-Ch-Ua-Mobile / Sec-Ch-Ua-Platform are intentionally
# omitted — Playwright sets those from the actual Chromium binary version,
# and overriding them with a static string would create a detectable mismatch.
_EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
}


# ── BrowserManager ────────────────────────────────────────────────────────────

class BrowserManager:
    """
    Async context manager that owns a Playwright browser instance.

    Parameters
    ----------
    headless:
        Whether to run in headless mode (no visible window).
    """

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    # ── Context manager protocol ──────────────────────────────────────────

    async def __aenter__(self) -> "BrowserManager":
        self._playwright = await async_playwright().start()
        _args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--mute-audio",
        ]
        if sys.platform != "win32":
            # These flags bypass the Linux sandbox — meaningless/harmful on Windows
            _args += ["--no-sandbox", "--disable-setuid-sandbox"]
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=_args,
        )
        self._context = await self._browser.new_context(
            viewport=_VIEWPORT,
            user_agent=_USER_AGENT,
            locale=_LOCALE,
            extra_http_headers=_EXTRA_HEADERS,
            # Avoid common automation fingerprinting checks
            java_script_enabled=True,
        )
        # Hide the webdriver property that many anti-bot scripts check
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: "TracebackType | None",
    ) -> None:
        for obj, method in [
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ]:
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception as exc:
                    logger.debug("Cleanup error (%s.%s): %s", type(obj).__name__, method, exc)

    # ── Public API ────────────────────────────────────────────────────────

    async def new_page(self) -> Page:
        """
        Return a new :class:`playwright.async_api.Page` configured with
        realistic browser properties.
        """
        if self._context is None:
            raise RuntimeError(
                "BrowserManager must be used as an async context manager."
            )
        page = await self._context.new_page()
        # Default navigation timeout — individual callers can override
        page.set_default_navigation_timeout(15_000)
        page.set_default_timeout(15_000)
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except Exception:
            pass  # stealth not installed or JS files missing (e.g. PyInstaller bundle)
        return page
