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
import random
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ── CAPTCHA detection selectors ───────────────────────────────────────────────

_CAPTCHA_SELECTORS = [
    # Specific CAPTCHA iframes (most reliable signal)
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare']",
    # Official widget classes
    ".g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    # Data attributes used by reCAPTCHA / hCaptcha
    "div[data-sitekey]",
    "[data-hcaptcha-widget-id]",
    # Broad ID match only (not class — too many false positives)
    "#captcha",
    "#recaptcha",
    "#hcaptcha",
]

# ── Age gate selectors ────────────────────────────────────────────────────────

_AGE_GATE_SELECTORS = [
    # Container selectors — if found, look for Yes/Enter button inside
    ".age-gate", "#age-gate", "[class*='age-gate']",
    ".age-verification", "#age-verification", "[class*='age-verify']",
    "#ageModal", ".modal-age", "[data-age-gate]",
]

_AGE_GATE_BUTTONS = [
    "button:has-text('Yes, I am')",
    "button:has-text('I am 18')",
    "button:has-text('I am over')",
    "button:has-text('Yes, I\\'m')",
    "a:has-text('I am 18')",
    "a:has-text('Yes')",
    ".age-gate button",
    "#age-gate button",
    "[class*='age-gate'] button",
    "[class*='age-gate'] a",
    "input[value*='Yes' i][type='button']",
    "input[value*='Enter' i][type='button']",
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

# Next/Continue step selectors for multi-step forms
_NEXT_STEP_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Continue')",
    "button:has-text('Next Step')",
    "button:has-text('Next Page')",
    "button:has-text('Proceed')",
    "input[value*='Next' i]",
    "input[value*='Continue' i]",
    ".btn-next", ".next-step", ".next-btn",
    "[data-action='next']",
    "button[class*='next' i]",
]

# Success URL path fragments
_SUCCESS_URL_PATTERNS = [
    "/thank", "/thanks", "/success", "/confirm", "/thank-you",
    "/thankyou", "/entry-complete", "/entered", "/congratulations",
]

# Success page text fragments (lower-cased)
_SUCCESS_TEXT_PATTERNS = [
    "thank you for entering", "thank you for your entry",
    "you have been entered", "you're entered", "you are entered",
    "entry received", "entry confirmed", "entry complete",
    "successfully entered", "successfully submitted",
    "good luck", "submission received", "congratulations",
    "you have successfully", "your entry has been",
]

# Expired/closed sweepstake text fragments (lower-cased)
_EXPIRED_PATTERNS = [
    "this sweepstakes has ended", "this giveaway has ended",
    "this contest has ended", "sweepstakes has ended",
    "giveaway has ended", "contest has ended",
    "contest is closed", "sweepstakes is closed", "giveaway is closed",
    "entries are closed", "entry period has ended", "entry period has closed",
    "entry period is now closed", "no longer accepting entries",
    "entry deadline has passed", "winner has been selected",
    "winners have been selected", "winner has been announced",
    "drawing has been held", "promotion has ended", "promotion has closed",
    "giveaway is over", "contest is over", "sweepstakes is over",
    "this promotion has ended", "unfortunately, this contest",
    "competition has closed", "submission period is closed",
    "closed to entries", "this offer has expired", "offer has expired",
]

# Daily re-entry text fragments (lower-cased)
_DAILY_ENTRY_PATTERNS = [
    "enter daily", "enter once per day", "enter once a day",
    "daily entry", "one entry per day", "1 entry per day",
    "come back tomorrow", "enter again tomorrow", "enter every day",
    "daily sweepstakes", "enter each day", "entries per day",
    "once daily", "once per day", "daily giveaway", "daily contest",
    "enter again each day", "you may enter again",
]

_FIELD_DELAY_MIN = 0.15
_FIELD_DELAY_MAX = 0.45


async def _human_delay(min_s: float = _FIELD_DELAY_MIN, max_s: float = _FIELD_DELAY_MAX) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


# ── Full name field selectors ─────────────────────────────────────────────────
# Used when a form has a single combined name field instead of first + last.

_FULL_NAME_SELECTORS = [
    "input[name='name']",
    "input[id='name']",
    "input[name*='fullname' i]",
    "input[name*='full_name' i]",
    "input[name*='full-name' i]",
    "input[id*='fullname' i]",
    "input[id*='full_name' i]",
    "input[placeholder*='full name' i]",
    "input[placeholder*='your name' i]",
    "input[placeholder*='name' i]:not([placeholder*='first']):not([placeholder*='last'])",
]

# ── Gender field selectors ────────────────────────────────────────────────────

_GENDER_SELECTORS_MALE = [
    "input[type='radio'][value*='male' i]:not([value*='female' i])",
    "input[type='radio'][value='M']",
    "input[type='radio'][value='m']",
    "input[type='radio'][id*='male' i]:not([id*='female' i])",
    "input[type='radio'][name*='gender' i][value*='male' i]",
    "input[type='radio'][name*='sex' i][value*='male' i]",
]

_GENDER_SELECTORS_FEMALE = [
    "input[type='radio'][value*='female' i]",
    "input[type='radio'][value='F']",
    "input[type='radio'][value='f']",
    "input[type='radio'][id*='female' i]",
    "input[type='radio'][name*='gender' i][value*='female' i]",
    "input[type='radio'][name*='sex' i][value*='female' i]",
]

_GENDER_SELECT_SELECTORS = [
    "select[name*='gender' i]",
    "select[id*='gender' i]",
    "select[name*='sex' i]",
    "select[id*='sex' i]",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _wait_for_form(page: Page) -> bool:
    """Wait up to 8 s for any visible form element. Returns True if found."""
    try:
        await page.wait_for_selector(
            "input:not([type='hidden']), select, textarea, button[type='submit'], input[type='submit']",
            state="visible",
            timeout=8_000,
        )
        return True
    except PlaywrightTimeout:
        return False


async def _has_captcha(page: Page) -> bool:
    for sel in _CAPTCHA_SELECTORS:
        try:
            el = await page.query_selector(sel)
            # Must be visible — many sites have hidden captcha containers in the
            # DOM that are never shown to the user, causing false positives.
            if el and await el.is_visible():
                return True
        except Exception:
            pass
    return False


async def _detect_captcha(page: Page) -> tuple[str | None, str | None]:
    """
    Detect visible CAPTCHA and return (type, sitekey).
    type is 'recaptcha', 'hcaptcha', or None.
    sitekey is the data-sitekey value or None.
    """
    # reCAPTCHA
    for sel in [".g-recaptcha", "div[data-sitekey]", "iframe[src*='recaptcha']"]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                sitekey = await el.get_attribute("data-sitekey")
                if not sitekey:
                    # Try to extract from iframe src
                    src = await el.get_attribute("src") or ""
                    if "k=" in src:
                        sitekey = src.split("k=")[1].split("&")[0]
                return "recaptcha", sitekey
        except Exception:
            pass

    # Also check for recaptcha via JavaScript
    try:
        sitekey = await page.evaluate("""
            () => {
                const el = document.querySelector('.g-recaptcha, [data-sitekey]');
                return el ? el.getAttribute('data-sitekey') : null;
            }
        """)
        if sitekey:
            return "recaptcha", sitekey
    except Exception:
        pass

    # hCaptcha
    for sel in [".h-captcha", "[data-hcaptcha-widget-id]", "iframe[src*='hcaptcha']"]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                sitekey = await el.get_attribute("data-sitekey")
                return "hcaptcha", sitekey
        except Exception:
            pass

    # Cloudflare Turnstile
    for sel in [".cf-turnstile", "iframe[src*='challenges.cloudflare']"]:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                sitekey = await el.get_attribute("data-sitekey")
                return "recaptcha", sitekey  # treat as recaptcha-compatible
        except Exception:
            pass

    return None, None


async def _inject_captcha_token(page: Page, captcha_type: str, token: str) -> None:
    """Inject a solved CAPTCHA token into the page and trigger callbacks."""
    # Escape token for JS string (tokens are alphanumeric so minimal risk)
    safe_token = token.replace("'", "\\'").replace("\n", "")

    if captcha_type == "recaptcha":
        await page.evaluate(f"""
            (function() {{
                // Set the response textarea
                var resp = document.getElementById('g-recaptcha-response');
                if (resp) resp.innerHTML = '{safe_token}';
                document.querySelectorAll('.g-recaptcha-response').forEach(
                    function(el) {{ el.innerHTML = '{safe_token}'; }}
                );
                // Fire the grecaptcha callback if registered
                try {{
                    var cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {{
                        Object.values(cfg.clients).forEach(function(c) {{
                            if (c && c.callback) c.callback('{safe_token}');
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        """)
    elif captcha_type == "hcaptcha":
        await page.evaluate(f"""
            (function() {{
                var sel = 'textarea[name="h-captcha-response"], ' +
                          'textarea[name="g-recaptcha-response"]';
                document.querySelectorAll(sel).forEach(
                    function(el) {{ el.value = '{safe_token}'; }}
                );
                // Fire hcaptcha callback
                try {{
                    if (window.hcaptcha) {{
                        Object.values(window.hcaptcha._state || {{}}).forEach(function(s) {{
                            if (s && s.response && s.onSuccess) s.onSuccess('{safe_token}');
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        """)

    await asyncio.sleep(0.5)


async def _bypass_age_gate(page: Page, profile: dict) -> bool:
    """Try to click through age verification gate. Returns True if one was found."""
    # First try clicking a button directly
    for sel in _AGE_GATE_BUTTONS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.8)
                return True
        except Exception:
            pass

    # Check for age gate containers that might have a DOB form
    for sel in _AGE_GATE_SELECTORS:
        try:
            container = await page.query_selector(sel)
            if container and await container.is_visible():
                # Try filling DOB in the gate
                for month_sel in ["select[name*='month' i]", "input[name*='month' i]"]:
                    m = await page.query_selector(month_sel)
                    if m and await m.is_visible():
                        tag = await page.eval_on_selector(month_sel, "el => el.tagName.toLowerCase()")
                        if tag == "select":
                            await page.select_option(month_sel, value=profile.get("dob_month", "01"))
                        else:
                            await page.fill(month_sel, profile.get("dob_month", "01"))
                for day_sel in ["select[name*='day' i]", "input[name*='day' i]"]:
                    d = await page.query_selector(day_sel)
                    if d and await d.is_visible():
                        tag = await page.eval_on_selector(day_sel, "el => el.tagName.toLowerCase()")
                        if tag == "select":
                            await page.select_option(day_sel, value=profile.get("dob_day", "15"))
                        else:
                            await page.fill(day_sel, profile.get("dob_day", "15"))
                for year_sel in ["select[name*='year' i]", "input[name*='year' i]"]:
                    y = await page.query_selector(year_sel)
                    if y and await y.is_visible():
                        tag = await page.eval_on_selector(year_sel, "el => el.tagName.toLowerCase()")
                        if tag == "select":
                            await page.select_option(year_sel, value=profile.get("dob_year", "1990"))
                        else:
                            await page.fill(year_sel, profile.get("dob_year", "1990"))
                # Now find and click submit in the gate
                for btn_sel in ["button[type='submit']", "input[type='submit']",
                                 "button:has-text('Submit')", "button:has-text('Verify')",
                                 "button:has-text('Confirm')", "button:has-text('Enter')"]:
                    btn = await page.query_selector(btn_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await asyncio.sleep(0.8)
                        return True
        except Exception:
            pass
    return False


async def _enter_rafflecopter(page: Page, profile: dict) -> dict[str, Any] | None:
    """Handle Rafflecopter widgets. Returns result dict or None if not detected."""
    # Check for Rafflecopter iframe
    iframe_el = await page.query_selector(
        "iframe[src*='rafflecopter'], iframe[src*='widget-prime']"
    )
    if iframe_el is None:
        # Check for inline Rafflecopter (non-iframe embed)
        rc = await page.query_selector(".rc-entry-method, #rc-container, [class*='rcWidget']")
        if rc is None:
            return None

    try:
        if iframe_el:
            frame = await iframe_el.content_frame()
            if frame is None:
                return None
            target = frame
        else:
            target = page

        # Look for "Enter with Email" or first entry method
        email_input = await target.query_selector(
            "input[type='email'], input[name*='email' i], input[placeholder*='email' i]"
        )
        if email_input and await email_input.is_visible():
            await email_input.fill(profile.get("email", ""))
            await asyncio.sleep(0.3)
            submit = await target.query_selector(
                "button[type='submit'], .btn-enter, input[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "rafflecopter"}
    except Exception as exc:
        logger.debug("Rafflecopter handler error: %s", exc)
    return None


async def _enter_gleam(page: Page, profile: dict) -> dict[str, Any] | None:
    """Handle Gleam.io giveaway widgets. Returns result dict or None if not detected."""
    is_gleam = "gleam.io" in page.url or await page.query_selector(
        "script[src*='gleam'], div[data-gleam], .gleam-widget, #gleam-widget"
    ) is not None
    if not is_gleam:
        return None

    try:
        # Gleam uses an iframe or direct embed; find the email entry
        iframe_el = await page.query_selector("iframe[src*='gleam']")
        target = (await iframe_el.content_frame()) if iframe_el else page

        email_input = await target.query_selector(
            "input[type='email'], input[name*='email' i]"
        )
        if email_input and await email_input.is_visible():
            await email_input.fill(profile.get("email", ""))
            await asyncio.sleep(0.3)
            submit = await target.query_selector(
                "button[type='submit'], button:has-text('Enter'), .entry-btn"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "gleam"}
    except Exception as exc:
        logger.debug("Gleam handler error: %s", exc)
    return None


async def _fill_full_name(page: Page, profile: dict[str, str]) -> bool:
    """Fill a combined full-name field if separate first/last weren't found."""
    full_name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    if not full_name:
        return False
    for sel in _FULL_NAME_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                await el.triple_click()
                await el.fill(full_name)
                await _human_delay()
                return True
        except Exception:
            pass
    return False


async def _handle_gender(page: Page, gender: str = "M") -> None:
    """Try to fill a gender field with the given gender ('M' or 'F')."""
    selectors = _GENDER_SELECTORS_MALE if gender.upper() in ("M", "MALE") else _GENDER_SELECTORS_FEMALE
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                await el.click()
                await asyncio.sleep(0.2)
                return
        except Exception:
            pass

    # Try select dropdown
    for sel in _GENDER_SELECT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                # Try common values
                for val in (["male", "m", "1"] if gender.upper() in ("M", "MALE") else ["female", "f", "2"]):
                    try:
                        await page.select_option(sel, value=val)
                        await asyncio.sleep(0.2)
                        return
                    except Exception:
                        pass
        except Exception:
            pass


async def _enter_viralsweep(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle ViralSweep giveaway widgets."""
    is_vs = "viralsweep.com" in page.url or await page.query_selector(
        "iframe[src*='viralsweep'], script[src*='viralsweep'], .viralsweep-container, #viralsweep"
    ) is not None
    if not is_vs:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='viralsweep']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        email = await target.query_selector("input[type='email'], input[name*='email' i]")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
            fn = await target.query_selector("input[name*='first' i], input[placeholder*='first' i]")
            if fn:
                await fn.fill(profile.get("first_name", ""))
            ln = await target.query_selector("input[name*='last' i], input[placeholder*='last' i]")
            if ln:
                await ln.fill(profile.get("last_name", ""))
            submit = await target.query_selector("button[type='submit'], input[type='submit'], .vs-submit")
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "viralsweep"}
    except Exception as exc:
        logger.debug("ViralSweep handler error: %s", exc)
    return None


async def _enter_woobox(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Woobox giveaway embeds."""
    is_wb = "woobox.com" in page.url or await page.query_selector(
        "iframe[src*='woobox'], script[src*='woobox.com']"
    ) is not None
    if not is_wb:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='woobox']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        email = await target.query_selector("input[type='email'], input[name*='email' i]")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
            fn = await target.query_selector("input[name*='first' i]")
            if fn:
                await fn.fill(profile.get("first_name", ""))
            ln = await target.query_selector("input[name*='last' i]")
            if ln:
                await ln.fill(profile.get("last_name", ""))
            await asyncio.sleep(0.3)
            submit = await target.query_selector("button[type='submit'], input[type='submit'], .woobox-submit")
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "woobox"}
    except Exception as exc:
        logger.debug("Woobox handler error: %s", exc)
    return None


async def _enter_kingsumo(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle KingSumo giveaway pages."""
    is_ks = "kingsumo.com" in page.url or await page.query_selector(
        "form.giveaway-form, #kingsumo-form, .kingsumo-widget"
    ) is not None
    if not is_ks:
        return None
    try:
        email = await page.query_selector("input[type='email'], input[name='email']")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
            fn = await page.query_selector("input[name='first_name'], input[placeholder*='first' i]")
            if fn:
                await fn.fill(profile.get("first_name", ""))
            submit = await page.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "kingsumo"}
    except Exception as exc:
        logger.debug("KingSumo handler error: %s", exc)
    return None


async def _enter_promosimple(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle PromoSimple giveaway widgets."""
    is_ps = "promosimple.com" in page.url or await page.query_selector(
        "iframe[src*='promosimple'], script[src*='promosimple']"
    ) is not None
    if not is_ps:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='promosimple']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        email = await target.query_selector("input[type='email'], input[name*='email' i]")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
            fn = await target.query_selector("input[name*='first' i]")
            if fn:
                await fn.fill(profile.get("first_name", ""))
            ln = await target.query_selector("input[name*='last' i]")
            if ln:
                await ln.fill(profile.get("last_name", ""))
            submit = await target.query_selector("button[type='submit'], input[type='submit']")
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "promosimple"}
    except Exception as exc:
        logger.debug("PromoSimple handler error: %s", exc)
    return None


async def _enter_shortstack(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle ShortStack campaign pages."""
    is_ss = "shortstack.com" in page.url or await page.query_selector(
        "iframe[src*='shortstack'], script[src*='shortstack.com'], .ss-widget"
    ) is not None
    if not is_ss:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='shortstack']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        email = await target.query_selector("input[type='email'], input[name*='email' i]")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
            fn = await target.query_selector("input[name*='first' i]")
            if fn:
                await fn.fill(profile.get("first_name", ""))
            ln = await target.query_selector("input[name*='last' i]")
            if ln:
                await ln.fill(profile.get("last_name", ""))
            submit = await target.query_selector("button[type='submit'], input[type='submit'], .ss-form-submit")
            if submit:
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "shortstack"}
    except Exception as exc:
        logger.debug("ShortStack handler error: %s", exc)
    return None


async def _dismiss_gdpr_consent(page: Page) -> None:
    """Click 'Accept All' / 'Accept Cookies' banners before interacting with forms."""
    _CONSENT_SELECTORS = [
        # OneTrust
        "#onetrust-accept-btn-handler",
        ".onetrust-accept-btn-handler",
        "#accept-all-cookies",
        # Cookiebot
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        # CookieYes / LegalMonster
        ".cookie-accept-all",
        ".cky-btn-accept",
        # Generic patterns
        "button[id*='accept'][id*='cookie' i]",
        "button[class*='accept'][class*='cookie' i]",
        "button[aria-label*='accept all' i]",
        "button[aria-label*='accept cookies' i]",
        # Text-matched buttons (broad — try last)
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow All')",
        "button:has-text('I Accept')",
        "button:has-text('Agree')",
    ]
    for sel in _CONSENT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.6)
                return
        except Exception:
            pass


async def _enter_typeform(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Typeform-hosted and embedded Typeform surveys/giveaways."""
    is_tf = (
        "typeform.com" in page.url
        or await page.query_selector("iframe[src*='typeform.com'], [data-tf-widget]") is not None
    )
    if not is_tf:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='typeform.com']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None

        # Typeform shows one question at a time; iterate up to 20 questions
        for _ in range(20):
            await asyncio.sleep(0.8)
            filled = False
            # Email question
            email_input = await target.query_selector("input[type='email']")
            if email_input and await email_input.is_visible():
                await email_input.fill(profile.get("email", ""))
                filled = True
            # Short/long text questions — map placeholder/aria-label to profile keys
            for inp in await target.query_selector_all("input[type='text'], textarea"):
                if not await inp.is_visible():
                    continue
                placeholder = (await inp.get_attribute("placeholder") or "").lower()
                aria = (await inp.get_attribute("aria-label") or "").lower()
                hint = placeholder + " " + aria
                val = ""
                if "first" in hint:
                    val = profile.get("first_name", "")
                elif "last" in hint:
                    val = profile.get("last_name", "")
                elif "name" in hint:
                    val = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
                elif "phone" in hint or "mobile" in hint:
                    val = profile.get("phone", "")
                elif "zip" in hint or "postal" in hint:
                    val = profile.get("zip", "")
                elif "city" in hint:
                    val = profile.get("city", "")
                if val:
                    await inp.fill(val)
                    filled = True
            # Press Enter to advance to next question
            await target.keyboard.press("Enter")
            await asyncio.sleep(0.6)
            # Detect thank-you / success screen
            content = (await target.text_content("body") or "").lower()
            if any(p in content for p in _SUCCESS_TEXT_PATTERNS):
                return {"status": "entered", "platform": "typeform"}
            # Check for a Submit button (final question)
            submit = await target.query_selector("button[type='submit'], [data-qa='submit-button']")
            if submit and await submit.is_visible():
                await submit.click()
                await asyncio.sleep(2)
                return {"status": "entered", "platform": "typeform"}
            if not filled:
                break
    except Exception as exc:
        logger.debug("Typeform handler error: %s", exc)
    return None


async def _enter_jotform(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle JotForm embedded forms."""
    is_jf = (
        "jotform.com" in page.url
        or await page.query_selector("iframe[src*='jotform.com'], form[action*='jotform.com']") is not None
    )
    if not is_jf:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='jotform.com']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        # Fill name widgets
        for inp in await target.query_selector_all("input[id*='first' i], input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[id*='last' i], input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[id*='phone' i], input[name*='phone' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("phone", ""))
        for inp in await target.query_selector_all("input[id*='zip' i], input[name*='postal' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("zip", ""))
        # Terms checkbox
        for cb in await target.query_selector_all("input[type='checkbox']"):
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check()
        submit = await target.query_selector("button[type='submit'], input[type='submit'], .jotform-submit-button")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "jotform"}
    except Exception as exc:
        logger.debug("JotForm handler error: %s", exc)
    return None


async def _enter_sweepwidget(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle SweepWidget embedded giveaway widgets."""
    is_sw = (
        "sweepwidget.com" in page.url
        or await page.query_selector(
            "iframe[src*='sweepwidget.com'], div[id*='sw-'], script[src*='sweepwidget.com']"
        ) is not None
    )
    if not is_sw:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='sweepwidget.com']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        email = await target.query_selector("input[type='email'], input[name*='email' i]")
        if email and await email.is_visible():
            await email.fill(profile.get("email", ""))
        name = await target.query_selector("input[name*='name' i]")
        if name and await name.is_visible():
            await name.fill(f"{profile.get('first_name','')} {profile.get('last_name','')}".strip())
        submit = await target.query_selector("button[type='submit'], .sw-enter-btn, input[type='submit']")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "sweepwidget"}
    except Exception as exc:
        logger.debug("SweepWidget handler error: %s", exc)
    return None


async def _enter_wishpond(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Wishpond contest/giveaway landing pages and embeds."""
    is_wp = (
        "wishpond.com" in page.url
        or await page.query_selector(
            "iframe[src*='wishpond.com'], [data-wishpond], script[src*='wishpond.com']"
        ) is not None
    )
    if not is_wp:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='wishpond.com']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        submit = await target.query_selector(
            "button[type='submit'], input[type='submit'], .wishpond-submit, .wp-enter-btn"
        )
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "wishpond"}
    except Exception as exc:
        logger.debug("Wishpond handler error: %s", exc)
    return None


async def _enter_mailchimp(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Mailchimp embedded subscribe forms."""
    is_mc = (
        "list-manage.com" in page.url
        or "mailchimp.com" in page.url
        or await page.query_selector(
            "form#mc-embedded-subscribe-form, form[action*='list-manage.com'], form[action*='mailchimp.com']"
        ) is not None
    )
    if not is_mc:
        return None
    try:
        for inp in await page.query_selector_all("input[type='email'], input[name*='EMAIL' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await page.query_selector_all("input[name='FNAME'], input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await page.query_selector_all("input[name='LNAME'], input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        submit = await page.query_selector(
            "input#mc-embedded-subscribe, button[type='submit'], input[type='submit']"
        )
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "mailchimp"}
    except Exception as exc:
        logger.debug("Mailchimp handler error: %s", exc)
    return None


async def _enter_wordpress_forms(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Gravity Forms, WPForms, Contact Form 7, and Ninja Forms."""
    _WP_FORM_SELECTORS = {
        "gravityforms": ".gform_wrapper form, #gform_wrapper",
        "wpforms": ".wpforms-form",
        "cf7": ".wpcf7-form",
        "ninjaforms": "form[id^='nf-form-'], .nf-form-cont",
        "formidable": ".frm_forms form",
        "caldera": ".caldera-forms-form",
    }
    detected_form = None
    detected_platform = None
    for platform, sel in _WP_FORM_SELECTORS.items():
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            detected_form = el
            detected_platform = platform
            break
    if detected_form is None:
        return None
    try:
        # Email
        for inp in await page.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        # First name
        for inp in await page.query_selector_all(
            "input[name*='first' i], input[id*='first' i], input[class*='first' i]"
        ):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        # Last name
        for inp in await page.query_selector_all(
            "input[name*='last' i], input[id*='last' i], input[class*='last' i]"
        ):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        # Phone
        for inp in await page.query_selector_all("input[type='tel'], input[name*='phone' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("phone", ""))
        # ZIP
        for inp in await page.query_selector_all("input[name*='zip' i], input[name*='postal' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("zip", ""))
        # Terms / consent checkboxes
        for cb in await page.query_selector_all("input[type='checkbox']"):
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check()
        # Submit
        submit = await page.query_selector(
            ".gform_button, .wpforms-submit, input.wpcf7-submit, .nf-form-submit input[type='submit'], "
            "button[type='submit'], input[type='submit']"
        )
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": detected_platform}
    except Exception as exc:
        logger.debug("WordPress forms handler (%s) error: %s", detected_platform, exc)
    return None


async def _enter_secondstreet(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Second Street / UpicKem contest pages."""
    is_ss = (
        "secondstreet.com" in page.url
        or "upickem.net" in page.url
        or "contest.secondstreet.com" in page.url
        or await page.query_selector(
            "iframe[src*='secondstreet.com'], iframe[src*='upickem.net'], [data-second-street]"
        ) is not None
    )
    if not is_ss:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='secondstreet.com'], iframe[src*='upickem.net']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        for inp in await target.query_selector_all("input[type='tel'], input[name*='phone' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("phone", ""))
        for cb in await target.query_selector_all("input[type='checkbox']"):
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check()
        submit = await target.query_selector("button[type='submit'], input[type='submit']")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "secondstreet"}
    except Exception as exc:
        logger.debug("Second Street handler error: %s", exc)
    return None


async def _enter_easypromos(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Easypromos contest pages and embeds."""
    is_ep = (
        "easypromosapp.com" in page.url
        or "easypromos.com" in page.url
        or await page.query_selector("iframe[src*='easypromosapp.com'], iframe[src*='easypromos.com']") is not None
    )
    if not is_ep:
        return None
    try:
        iframe_el = await page.query_selector(
            "iframe[src*='easypromosapp.com'], iframe[src*='easypromos.com']"
        )
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[name*='name' i]"):
            if await inp.is_visible():
                full = f"{profile.get('first_name','')} {profile.get('last_name','')}".strip()
                await inp.fill(full)
        for inp in await target.query_selector_all("input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        for cb in await target.query_selector_all("input[type='checkbox']"):
            if await cb.is_visible() and not await cb.is_checked():
                await cb.check()
        submit = await target.query_selector("button[type='submit'], input[type='submit'], .ep-button-submit")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "easypromos"}
    except Exception as exc:
        logger.debug("Easypromos handler error: %s", exc)
    return None


async def _enter_kickofflabs(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle KickoffLabs landing pages and embeds."""
    is_kl = (
        "kickofflabs.com" in page.url
        or await page.query_selector(
            "iframe[src*='kickofflabs.com'], script[src*='kickofflabs.com'], [data-kickofflabs]"
        ) is not None
    )
    if not is_kl:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='kickofflabs.com']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[name*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[name*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        submit = await target.query_selector("button[type='submit'], input[type='submit'], .kol-submit")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "kickofflabs"}
    except Exception as exc:
        logger.debug("KickoffLabs handler error: %s", exc)
    return None


async def _enter_vyper(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """Handle Vyper (formerly Socialman) viral giveaway pages."""
    is_vy = (
        "vyper.io" in page.url
        or "socialman.net" in page.url
        or await page.query_selector(
            "iframe[src*='vyper.io'], iframe[src*='socialman.net'], script[src*='vyper.io']"
        ) is not None
    )
    if not is_vy:
        return None
    try:
        iframe_el = await page.query_selector("iframe[src*='vyper.io'], iframe[src*='socialman.net']")
        target = (await iframe_el.content_frame()) if iframe_el else page
        if target is None:
            return None
        for inp in await target.query_selector_all("input[type='email']"):
            if await inp.is_visible():
                await inp.fill(profile.get("email", ""))
        for inp in await target.query_selector_all("input[name*='first' i], input[placeholder*='first' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("first_name", ""))
        for inp in await target.query_selector_all("input[name*='last' i], input[placeholder*='last' i]"):
            if await inp.is_visible():
                await inp.fill(profile.get("last_name", ""))
        submit = await target.query_selector("button[type='submit'], input[type='submit'], .vyper-submit")
        if submit and await submit.is_visible():
            await submit.click()
            await asyncio.sleep(2)
            return {"status": "entered", "platform": "vyper"}
    except Exception as exc:
        logger.debug("Vyper handler error: %s", exc)
    return None


async def _enter_generic_iframe(page: Page, profile: dict[str, str]) -> dict[str, Any] | None:
    """
    Fallback: scan all iframes on the page and attempt to fill any form found inside.
    Used when no specific platform was detected.
    """
    try:
        frames = page.frames
        for frame in frames[1:]:  # skip main frame
            try:
                if not frame.url or frame.url == "about:blank":
                    continue
                email_inp = await frame.query_selector("input[type='email']")
                if email_inp is None or not await email_inp.is_visible():
                    continue
                await email_inp.fill(profile.get("email", ""))
                for inp in await frame.query_selector_all("input[name*='first' i]"):
                    if await inp.is_visible():
                        await inp.fill(profile.get("first_name", ""))
                for inp in await frame.query_selector_all("input[name*='last' i]"):
                    if await inp.is_visible():
                        await inp.fill(profile.get("last_name", ""))
                for inp in await frame.query_selector_all("input[type='tel'], input[name*='phone' i]"):
                    if await inp.is_visible():
                        await inp.fill(profile.get("phone", ""))
                for cb in await frame.query_selector_all("input[type='checkbox']"):
                    if await cb.is_visible() and not await cb.is_checked():
                        await cb.check()
                submit = await frame.query_selector("button[type='submit'], input[type='submit']")
                if submit and await submit.is_visible():
                    await submit.click()
                    await asyncio.sleep(2)
                    return {"status": "entered", "platform": "iframe_generic"}
            except Exception:
                continue
    except Exception as exc:
        logger.debug("Generic iframe handler error: %s", exc)
    return None


async def _detect_success(page: Page) -> bool:
    """Return True if the current page shows signs of a successful entry."""
    url = page.url.lower()
    if any(p in url for p in _SUCCESS_URL_PATTERNS):
        return True
    try:
        content = (await page.text_content("body") or "").lower()
        if any(p in content for p in _SUCCESS_TEXT_PATTERNS):
            return True
    except Exception:
        pass
    return False


async def _is_expired(page: Page) -> bool:
    """Return True if the page indicates the sweepstake is closed/expired."""
    try:
        content = (await page.text_content("body") or "").lower()
        return any(p in content for p in _EXPIRED_PATTERNS)
    except Exception:
        return False


async def _check_daily_entry(page: Page) -> bool:
    """Return True if the page indicates daily re-entry is allowed."""
    try:
        content = (await page.text_content("body") or "").lower()
        return any(p in content for p in _DAILY_ENTRY_PATTERNS)
    except Exception:
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

            await _human_delay()
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
            await _human_delay()
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


async def _try_next_step(page: Page) -> bool:
    """Click a Next/Continue button if visible. Returns True if clicked."""
    for sel in _NEXT_STEP_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible() and await el.is_enabled():
                await el.click()
                await asyncio.sleep(1.0)
                return True
        except Exception:
            pass
    return False


# ── Main entry function ───────────────────────────────────────────────────────

async def fill_and_submit(page: Page, profile: dict[str, str], captcha_solver=None) -> dict[str, Any]:
    """
    Attempt to fill and submit the entry form on the current page.

    Parameters
    ----------
    page:
        A Playwright Page already navigated to the sweepstake URL.
    profile:
        User profile dict from config (keys match _FIELD_SELECTORS above).
    captcha_solver:
        Optional CaptchaSolver instance for auto-solving CAPTCHAs.

    Returns
    -------
    dict with 'status' key: "entered" | "captcha" | "no_form" | "error"
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeout:
        return {"status": "error", "message": "Page load timeout"}

    # ── Dismiss GDPR / cookie-consent banners first ───────────────────────────
    await _dismiss_gdpr_consent(page)

    # ── Platform-specific handlers first ─────────────────────────────────────
    rc_result = await _enter_rafflecopter(page, profile)
    if rc_result:
        return rc_result

    gleam_result = await _enter_gleam(page, profile)
    if gleam_result:
        return gleam_result

    vs_result = await _enter_viralsweep(page, profile)
    if vs_result:
        return vs_result

    wb_result = await _enter_woobox(page, profile)
    if wb_result:
        return wb_result

    ks_result = await _enter_kingsumo(page, profile)
    if ks_result:
        return ks_result

    ps_result = await _enter_promosimple(page, profile)
    if ps_result:
        return ps_result

    ss_result = await _enter_shortstack(page, profile)
    if ss_result:
        return ss_result

    tf_result = await _enter_typeform(page, profile)
    if tf_result:
        return tf_result

    jf_result = await _enter_jotform(page, profile)
    if jf_result:
        return jf_result

    sww_result = await _enter_sweepwidget(page, profile)
    if sww_result:
        return sww_result

    wp_result = await _enter_wishpond(page, profile)
    if wp_result:
        return wp_result

    mc_result = await _enter_mailchimp(page, profile)
    if mc_result:
        return mc_result

    wpf_result = await _enter_wordpress_forms(page, profile)
    if wpf_result:
        return wpf_result

    street_result = await _enter_secondstreet(page, profile)
    if street_result:
        return street_result

    ep_result = await _enter_easypromos(page, profile)
    if ep_result:
        return ep_result

    kl_result = await _enter_kickofflabs(page, profile)
    if kl_result:
        return kl_result

    vy_result = await _enter_vyper(page, profile)
    if vy_result:
        return vy_result

    iframe_result = await _enter_generic_iframe(page, profile)
    if iframe_result:
        return iframe_result

    # ── Age gate bypass ───────────────────────────────────────────────────────
    await _bypass_age_gate(page, profile)

    # ── Wait for dynamic form to load ─────────────────────────────────────────
    await _wait_for_form(page)

    # ── Expiry check — skip closed sweepstakes immediately ────────────────────
    if await _is_expired(page):
        return {"status": "expired"}

    # ── CAPTCHA detection & solving ───────────────────────────────────────────
    captcha_type, sitekey = await _detect_captcha(page)
    if captcha_type:
        if captcha_solver and captcha_solver.enabled and sitekey:
            logger.info("Attempting to solve %s (sitekey: %s…)", captcha_type, (sitekey or "")[:8])
            if captcha_type == "hcaptcha":
                token = await asyncio.get_event_loop().run_in_executor(
                    None, captcha_solver.solve_hcaptcha, sitekey, page.url
                )
            else:
                token = await asyncio.get_event_loop().run_in_executor(
                    None, captcha_solver.solve_recaptcha, sitekey, page.url
                )
            if token:
                await _inject_captcha_token(page, captcha_type, token)
                # Don't return — continue to fill and submit
            else:
                return {"status": "captcha", "detail": "solver returned no token"}
        else:
            return {"status": "captcha"}

    # ── Multi-step form loop (max 4 steps) ────────────────────────────────────
    total_fields_filled = 0
    submitted = False

    for step in range(4):
        fields_filled = 0

        for profile_key, selectors in _FIELD_SELECTORS.items():
            value = profile.get(profile_key, "")
            if not value:
                continue
            if await _fill_field(page, selectors, value):
                fields_filled += 1

        state_value = profile.get("state", "")
        if state_value:
            await _fill_state(page, state_value)

        # Full name field (fallback when first+last fields not found)
        if fields_filled < 2:
            fn_filled = await _fill_full_name(page, profile)
            if fn_filled:
                fields_filled += 1

        # Gender field
        gender = profile.get("gender", "M")
        await _handle_gender(page, gender)

        await _check_terms(page)

        total_fields_filled += fields_filled

        # Try submitting
        clicked = await _click_submit(page)
        if clicked:
            await asyncio.sleep(1.5)
            # Check if we landed on a success page
            if await _detect_success(page):
                return {"status": "entered", "steps": step + 1}
            # Check if another step appeared
            next_clicked = await _try_next_step(page)
            if not next_clicked:
                # No next step, no clear success — assume entered
                submitted = True
                break
        else:
            # No submit found — try next step button
            next_clicked = await _try_next_step(page)
            if not next_clicked:
                break

    if total_fields_filled == 0:
        return {"status": "no_form"}

    if not submitted:
        return {"status": "error", "message": "Could not find or complete submit"}

    return {"status": "entered"}


async def enter_sweepstake(page: Page, url: str, profile: dict[str, str], captcha_solver=None) -> dict[str, Any]:
    """
    Navigate to *url* and attempt entry.  Wraps :func:`fill_and_submit` with
    navigation error handling and a single automatic retry on transient errors.
    """
    for attempt in range(2):
        try:
            if attempt == 0:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            else:
                # Retry: reload the page
                await page.reload(wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(random.uniform(1.5, 3.0))

            result = await fill_and_submit(page, profile, captcha_solver=captcha_solver)

            # Annotate successful entries with daily re-entry flag
            if result["status"] == "entered" and "allows_daily" not in result:
                result["allows_daily"] = await _check_daily_entry(page)

            # Only retry on transient errors, not on no_form/captcha/entered/expired
            if result["status"] not in ("error",) or attempt == 1:
                return result

        except PlaywrightTimeout:
            if attempt == 1:
                return {"status": "error", "message": f"Navigation timeout after retry: {url}"}
        except Exception as exc:
            if attempt == 1:
                return {"status": "error", "message": str(exc)}

    return {"status": "error", "message": "Unknown failure"}
