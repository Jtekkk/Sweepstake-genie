"""
Playwright-based form filler for sweepstakes entry pages.

Returns a result dict with a 'status' key:
  - "entered"  — form submitted successfully
  - "captcha"  — CAPTCHA detected, skipped
  - "no_form"  — no recognisable entry form found
  - "error"    — unexpected failure; 'message' key contains details
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ── CAPTCHA detection selectors ───────────────────────────────────────────────

_CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='captcha']",
    ".g-recaptcha",
    ".h-captcha",
    "#captcha",
    "[class*='captcha']",
    "[id*='captcha']",
    "div[data-sitekey]",
]

# ── Field selector map ────────────────────────────────────────────────────────
# Maps profile key → list of CSS selectors tried in order

_FIELD_SELECTORS: dict[str, list[str]] = {
    "first_name": [
        "input[name*='first' i]",
        "input[id*='first' i]",
        "input[placeholder*='first name' i]",
        "input[autocomplete='given-name']",
    ],
    "last_name": [
        "input[name*='last' i]",
        "input[id*='last' i]",
        "input[placeholder*='last name' i]",
        "input[autocomplete='family-name']",
    ],
    "email": [
        "input[type='email']",
        "input[name*='email' i]",
        "input[id*='email' i]",
        "input[placeholder*='email' i]",
        "input[autocomplete='email']",
    ],
    "email_confirm": [
        "input[name*='confirm' i][type='email']",
        "input[id*='confirm' i]",
        "input[name*='verify' i]",
        "input[placeholder*='confirm' i]",
    ],
    "phone": [
        "input[type='tel']",
        "input[name*='phone' i]",
        "input[id*='phone' i]",
        "input[placeholder*='phone' i]",
        "input[autocomplete='tel']",
    ],
    "address1": [
        "input[name*='address1' i]",
        "input[name*='address_1' i]",
        "input[id*='address1' i]",
        "input[name*='street' i]",
        "input[placeholder*='address' i]",
        "input[autocomplete='street-address']",
        "input[autocomplete='address-line1']",
    ],
    "address2": [
        "input[name*='address2' i]",
        "input[name*='address_2' i]",
        "input[id*='address2' i]",
        "input[autocomplete='address-line2']",
    ],
    "city": [
        "input[name*='city' i]",
        "input[id*='city' i]",
        "input[placeholder*='city' i]",
        "input[autocomplete='address-level2']",
    ],
    "zip": [
        "input[name*='zip' i]",
        "input[name*='postal' i]",
        "input[id*='zip' i]",
        "input[id*='postal' i]",
        "input[placeholder*='zip' i]",
        "input[autocomplete='postal-code']",
    ],
    "dob_month": [
        "select[name*='month' i]",
        "select[id*='month' i]",
        "input[name*='month' i]",
        "input[id*='month' i]",
    ],
    "dob_day": [
        "select[name*='day' i]",
        "select[id*='day' i]",
        "input[name*='day' i]",
        "input[id*='day' i]",
    ],
    "dob_year": [
        "select[name*='year' i]",
        "select[id*='year' i]",
        "input[name*='year' i]",
        "input[id*='year' i]",
    ],
}

# State selectors tried separately because they may be <select> or <input>
_STATE_SELECTORS = [
    "select[name*='state' i]",
    "select[id*='state' i]",
    "input[name*='state' i]",
    "input[id*='state' i]",
    "input[autocomplete='address-level1']",
    "select[autocomplete='address-level1']",
]

# Submit button selectors
_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Enter')",
    "button:has-text('Submit')",
    "button:has-text('Enter Now')",
    "button:has-text('Enter Sweepstakes')",
    "button:has-text('Enter to Win')",
    "input[value*='Enter' i]",
    "input[value*='Submit' i]",
]

# Terms/consent checkboxes
_TERMS_SELECTORS = [
    "input[type='checkbox'][name*='terms' i]",
    "input[type='checkbox'][name*='agree' i]",
    "input[type='checkbox'][name*='consent' i]",
    "input[type='checkbox'][id*='terms' i]",
    "input[type='checkbox'][id*='agree' i]",
    "input[type='checkbox'][id*='consent' i]",
    "input[type='checkbox'][name*='optin' i]",
]

_FIELD_DELAY = 0.3  # seconds between field fills


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _has_captcha(page: Page) -> bool:
    for sel in _CAPTCHA_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                return True
        except Exception:
            pass
    return False


async def _fill_field(page: Page, selectors: list[str], value: str, is_select: bool = False) -> bool:
    """Try each selector in order; fill the first visible, enabled match. Returns True if filled."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el is None:
                continue
            visible = await el.is_visible()
            enabled = await el.is_enabled()
            if not (visible and enabled):
                continue

            tag = (await el.get_attribute("tagName") or "").lower()
            if tag == "" :
                tag = await page.eval_on_selector(sel, "el => el.tagName.toLowerCase()")

            if tag == "select" or is_select:
                await page.select_option(sel, value=value)
            else:
                await el.triple_click()
                await el.fill(value)

            await asyncio.sleep(_FIELD_DELAY)
            return True
        except Exception as exc:
            logger.debug("fill_field selector=%s error=%s", sel, exc)
    return False


async def _fill_state(page: Page, state_value: str) -> None:
    """Fill state field — handles both <select> and <input>."""
    for sel in _STATE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el is None:
                continue
            visible = await el.is_visible()
            if not visible:
                continue
            tag = await page.eval_on_selector(sel, "el => el.tagName.toLowerCase()")
            if tag == "select":
                # Try value first, then label
                try:
                    await page.select_option(sel, value=state_value)
                except Exception:
                    await page.select_option(sel, label=state_value)
            else:
                await page.fill(sel, state_value)
            await asyncio.sleep(_FIELD_DELAY)
            return
        except Exception as exc:
            logger.debug("fill_state selector=%s error=%s", sel, exc)


async def _check_terms(page: Page) -> None:
    """Check any unchecked terms/consent checkboxes."""
    for sel in _TERMS_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
            for el in elements:
                if await el.is_visible() and not await el.is_checked():
                    await el.check()
                    await asyncio.sleep(0.2)
        except Exception as exc:
            logger.debug("check_terms selector=%s error=%s", sel, exc)


async def _click_submit(page: Page) -> bool:
    """Find and click the submit button. Returns True if clicked."""
    for sel in _SUBMIT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                await el.click()
                return True
        except Exception as exc:
            logger.debug("click_submit selector=%s error=%s", sel, exc)
    return False


# ── Main entry function ───────────────────────────────────────────────────────

async def fill_and_submit(page: Page, profile: dict[str, str]) -> dict[str, Any]:
    """
    Attempt to fill and submit the entry form on the current page.

    Parameters
    ----------
    page:
        A Playwright Page already navigated to the sweepstake URL.
    profile:
        User profile dict from config (keys match _FIELD_SELECTORS above).

    Returns
    -------
    dict with 'status' key: "entered" | "captcha" | "no_form" | "error"
    """
    try:
        # Wait for the page to be reasonably loaded
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeout:
        return {"status": "error", "message": "Page load timeout"}

    # ── CAPTCHA check ─────────────────────────────────────────────────────────
    if await _has_captcha(page):
        logger.info("CAPTCHA detected on %s", page.url)
        return {"status": "captcha"}

    # ── Fill known profile fields ─────────────────────────────────────────────
    fields_filled = 0

    for profile_key, selectors in _FIELD_SELECTORS.items():
        value = profile.get(profile_key, "")
        if not value:
            continue
        if await _fill_field(page, selectors, value):
            fields_filled += 1

    # State is handled separately (value vs. label matching)
    state_value = profile.get("state", "")
    if state_value:
        await _fill_state(page, state_value)

    if fields_filled == 0:
        logger.info("No form fields found on %s", page.url)
        return {"status": "no_form"}

    # ── Terms checkboxes ──────────────────────────────────────────────────────
    await _check_terms(page)

    # ── Submit ────────────────────────────────────────────────────────────────
    clicked = await _click_submit(page)
    if not clicked:
        return {"status": "error", "message": "Could not find submit button"}

    # Wait briefly for post-submit navigation or confirmation
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeout:
        pass  # Some pages don't fully reload after submission; treat as entered

    return {"status": "entered"}


async def enter_sweepstake(page: Page, url: str, profile: dict[str, str]) -> dict[str, Any]:
    """
    Navigate to *url* and attempt entry.  Wraps :func:`fill_and_submit` with
    navigation error handling.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
    except PlaywrightTimeout:
        return {"status": "error", "message": f"Navigation timeout: {url}"}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return await fill_and_submit(page, profile)
