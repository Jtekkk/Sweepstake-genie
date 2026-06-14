"""
Playwright browser lifecycle manager.

Usage::

    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.goto("https://example.com")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    "Chrome/120.0.0.0 Safari/537.36"
)

_VIEWPORT = {"width": 1280, "height": 720}

_LOCALE = "en-US"

# Extra HTTP headers that make requests look like a real browser
_EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
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
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--disable-web-security",
                "--allow-running-insecure-content",
                "--disable-extensions",
                "--mute-audio",
            ],
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
                except Exception:
                    pass

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
