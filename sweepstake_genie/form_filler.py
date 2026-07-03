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
import json
import logging
import random
import re
from typing import Any
from urllib.parse import urlparse, urljoin

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
        "select[name*='dob_m' i]",
        "select[name*='birth_m' i]",
        "input[name*='month' i]",
        "input[id*='month' i]",
        "input[name*='dob_m' i]",
        "input[name*='birth_m' i]",
    ],
    "dob_day": [
        "select[name*='day' i]",
        "select[id*='day' i]",
        "select[name*='dob_d' i]",
        "select[name*='birth_d' i]",
        "input[name*='day' i]",
        "input[id*='day' i]",
        "input[name*='dob_d' i]",
        "input[name*='birth_d' i]",
    ],
    "dob_year": [
        "select[name*='year' i]",
        "select[id*='year' i]",
        "select[name*='dob_y' i]",
        "select[name*='birth_y' i]",
        "input[name*='year' i]",
        "input[id*='year' i]",
        "input[name*='dob_y' i]",
        "input[name*='birth_y' i]",
        "input[placeholder*='YYYY' i]",
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
    "button:has-text('Enter the Sweepstakes')",
    "button:has-text('Enter Giveaway')",
    "button:has-text('Enter the Contest')",
    "input[value*='Enter' i]",
    "input[value*='Submit' i]",
    "button:has-text('Sign Up')",
    "button:has-text('Register')",
    "button:has-text('Join Now')",
    "button:has-text('Join')",
    "button:has-text('Subscribe')",
    "button:has-text('Confirm')",
    "button:has-text('Get Started')",
    "button:has-text('Claim')",
    "button:has-text('Claim Prize')",
    "button:has-text('Claim Your Prize')",
    "button:has-text('Yes, Enter Me')",
    "button:has-text('Count Me In')",
    "button:has-text('I\\'m In')",
    "button:has-text('Take Part')",
    "button:has-text('Participate')",
    "button:has-text('Complete Entry')",
    "button:has-text('Complete My Entry')",
    "button:has-text('Finish')",
    "button:has-text('Done')",
    "button:has-text('Send')",
    "button:has-text('Send Entry')",
    "button:has-text('Apply')",
    "button:has-text('Try My Luck')",
    "button:has-text('Spin')",
    "button:has-text('Play')",
    "button:has-text('Play Now')",
    "button:has-text('Reveal')",
    "button:has-text('Unlock')",
    "button:has-text('Access')",
    "input[value*='Sign Up' i]",
    "input[value*='Register' i]",
    "input[value*='Join' i]",
    "input[value*='Claim' i]",
    "input[value*='Participate' i]",
    "input[value*='Complete' i]",
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
    "button:has-text('Start')",
    "button:has-text('Begin')",
    "button:has-text('Go')",
    "button:has-text('I Agree')",
    "button:has-text('I agree')",
    "button:has-text('Accept')",
    "button:has-text('Yes')",
    "button:has-text('Click Here')",
    "button:has-text(\"Let's Go\")",
    "button:has-text('OK')",
    "button:has-text('Okay')",
    "a:has-text('Next')",
    "a:has-text('Continue')",
    "a:has-text('Next Step')",
    "a:has-text('Proceed')",
    "a:has-text('Start')",
    "a:has-text('Begin')",
    "input[value*='Next' i]",
    "input[value*='Continue' i]",
    "input[value*='Start' i]",
    "input[value*='Begin' i]",
    "input[value*='Proceed' i]",
    ".btn-next", ".next-step", ".next-btn",
    "[data-action='next']",
    "button[class*='next' i]",
    "button[class*='continue' i]",
    "a[class*='next' i]",
    "a[class*='continue' i]",
]

# Success URL path fragments
_SUCCESS_URL_PATTERNS = [
    "/thank", "/thanks", "/success", "/confirm", "/thank-you",
    "/thankyou", "/entry-complete", "/entered", "/congratulations",
    "/complete", "/done", "/registered", "/confirmation",
    "/receipt", "submitted=true", "success=1", "entered=1",
]

# Success page text fragments (lower-cased)
_SUCCESS_TEXT_PATTERNS = [
    "thank you for entering", "thank you for your entry",
    "you have been entered", "you're entered", "you are entered",
    "entry received", "entry confirmed", "entry complete",
    "successfully entered", "successfully submitted",
    "good luck", "submission received", "congratulations",
    "you have successfully", "your entry has been",
    "thanks for entering", "thanks for your entry",
    "you've been entered", "you have entered",
    "your entry is confirmed", "your submission has been received",
    "you're in", "you are in the draw",
    "we received your entry", "entry has been received",
    # Already-entered is still a success — user is in the draw
    "already entered", "already registered",
    "you've already entered", "you have already entered",
    "you already entered", "already submitted an entry",
    "duplicate entry", "entry already exists",
    "you are already entered", "already been entered",
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

async def _has_form_fields(page: Page) -> bool:
    """Return True if visible form input fields are already on the page."""
    try:
        for sel in ["input[type='email']", "input[type='text']:not([type='hidden'])", "input[type='tel']"]:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                return True
    except Exception:
        pass
    return False


async def _count_entry_fields(page: Page) -> int:
    """
    Count how many *distinct* recognised entry fields are visibly on the page.

    A sweepstakes entry form asks for several things (name, address, city, zip,
    phone, DOB, …). A blog's newsletter/comment box asks for one or two (usually
    just email). Counting distinct recognised field types lets us tell a real
    entry form apart from a newsletter signup that merely happens to carry a
    reCAPTCHA — so manual mode doesn't stop you to solve newsletter CAPTCHAs.
    """
    # Fields that strongly indicate a real entry form (not a newsletter box).
    strong_keys = (
        "first_name", "last_name", "address1", "address2",
        "city", "state", "zip", "phone", "dob_month", "dob_day",
        "dob_year", "age",
    )
    count = 0
    for key in strong_keys:
        for sel in _FIELD_SELECTORS.get(key, []):
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    count += 1
                    break
            except Exception:
                continue
    return count


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


async def _element_box(page: Page, selector: str):
    """Return (element, bounding_box) for the first match, or (None, None)."""
    try:
        el = await page.query_selector(selector)
        if not el:
            return None, None
        box = await el.bounding_box()
        return el, box
    except Exception:
        return None, None


def _is_real_box(box, min_w: int = 40, min_h: int = 40) -> bool:
    """True if a bounding box is actually rendered at a usable size."""
    return bool(box and box.get("width", 0) >= min_w and box.get("height", 0) >= min_h)


def _box_present(box) -> bool:
    """
    True if the element is laid out on the page (has any extent).

    Deliberately lenient: a reCAPTCHA v2 container often reports height 0 until
    its iframe injects, so requiring a large size would miss real, solvable
    widgets. ``display:none`` elements return no box and are correctly excluded.
    """
    return bool(box and (box.get("width", 0) > 0 or box.get("height", 0) > 0))


async def _detect_captcha(page: Page) -> tuple[str | None, str | None, bool]:
    """
    Detect a CAPTCHA and return (type, sitekey, interactive).

    - type: 'recaptcha', 'hcaptcha', or None
    - sitekey: the data-sitekey value (or extracted from an iframe src), or None
    - interactive: True only when a *user-solvable* challenge widget is actually
      rendered on the page — a reCAPTCHA v2 checkbox, an hCaptcha checkbox, or an
      interactive Turnstile widget. It is False for invisible / score-based
      reCAPTCHA v3 (the little "protected by reCAPTCHA" badge), which a human
      cannot click to solve. Manual-solve mode uses this to avoid pausing on
      ordinary entry pages that merely carry an invisible v3 badge.
    """
    # ── reCAPTCHA ─────────────────────────────────────────────────────────────
    recaptcha_present = False
    recaptcha_interactive = False
    recaptcha_sitekey = None

    # sitekey (present for both v2 and v3)
    try:
        recaptcha_sitekey = await page.evaluate("""
            () => {
                const el = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey]');
                return el ? el.getAttribute('data-sitekey') : null;
            }
        """)
    except Exception:
        recaptcha_sitekey = None

    # A rendered anchor iframe means a clickable v2 checkbox is on the page.
    for sel in ('iframe[src*="recaptcha"][src*="anchor"]',
                'iframe[title="reCAPTCHA"]',
                'iframe[title*="recaptcha" i]'):
        el, box = await _element_box(page, sel)
        if el is not None:
            recaptcha_present = True
            if _box_present(box):
                recaptcha_interactive = True
                if not recaptcha_sitekey:
                    src = await el.get_attribute("src") or ""
                    if "k=" in src:
                        recaptcha_sitekey = src.split("k=")[1].split("&")[0]
            break

    # A rendered, non-invisible .g-recaptcha container is an interactive v2 —
    # even if its iframe hasn't injected yet (height may momentarily be 0).
    if not recaptcha_interactive:
        el, box = await _element_box(page, ".g-recaptcha")
        if el is not None:
            recaptcha_present = True
            size = (await el.get_attribute("data-size") or "").lower()
            if size != "invisible" and _box_present(box):
                recaptcha_interactive = True
            if not recaptcha_sitekey:
                recaptcha_sitekey = await el.get_attribute("data-sitekey")

    # The floating v3 badge counts as "present but not interactive".
    if not recaptcha_present:
        badge, _ = await _element_box(page, ".grecaptcha-badge")
        if badge is not None or recaptcha_sitekey:
            recaptcha_present = True

    if recaptcha_present:
        return "recaptcha", recaptcha_sitekey, recaptcha_interactive

    # ── hCaptcha ──────────────────────────────────────────────────────────────
    hcaptcha_sitekey = None
    try:
        hcaptcha_sitekey = await page.evaluate("""
            () => {
                const el = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]');
                return el ? (el.getAttribute('data-sitekey')
                            || el.getAttribute('data-hcaptcha-sitekey')) : null;
            }
        """)
    except Exception:
        hcaptcha_sitekey = None

    for sel in ('iframe[src*="hcaptcha"][src*="checkbox"]',
                'iframe[src*="hcaptcha.com"]',
                '.h-captcha'):
        el, box = await _element_box(page, sel)
        if el is not None:
            size = (await el.get_attribute("data-size") or "").lower()
            interactive = size != "invisible" and _box_present(box)
            if not hcaptcha_sitekey:
                hcaptcha_sitekey = await el.get_attribute("data-sitekey")
            return "hcaptcha", hcaptcha_sitekey, interactive

    # ── Cloudflare Turnstile (treated as reCAPTCHA-compatible) ────────────────
    for sel in ('.cf-turnstile', 'iframe[src*="challenges.cloudflare"]'):
        el, box = await _element_box(page, sel)
        if el is not None:
            sitekey = await el.get_attribute("data-sitekey")
            return "recaptcha", sitekey, _box_present(box)

    return None, None, False


async def _detect_captcha_wait(page: Page, timeout: float = 6.0) -> tuple[str | None, str | None, bool]:
    """
    Like :func:`_detect_captcha`, but gives an async-loading widget a few seconds
    to finish rendering before deciding whether it is interactive.

    reCAPTCHA / hCaptcha inject their iframes after page load, so a single check
    can miss a real, solvable widget. This polls until an interactive CAPTCHA is
    found or *timeout* elapses. Pages with no CAPTCHA at all return immediately.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    ctype, sitekey, interactive = await _detect_captcha(page)
    # Only keep waiting while there is a hint of a CAPTCHA that hasn't rendered
    # its interactive widget yet — never stall a page that has none.
    while (loop.time() < deadline) and ctype and not interactive:
        await asyncio.sleep(0.7)
        ctype, sitekey, interactive = await _detect_captcha(page)
    return ctype, sitekey, interactive


async def _inject_captcha_token(page: Page, captcha_type: str, token: str) -> None:
    """Inject a solved CAPTCHA token into the page and trigger callbacks."""
    # json.dumps produces a fully-escaped JS string literal (handles backslashes,
    # quotes, control chars) — safe for direct interpolation into JS source.
    js_token = json.dumps(token)

    if captcha_type == "recaptcha":
        await page.evaluate(f"""
            (function() {{
                var t = {js_token};
                var resp = document.getElementById('g-recaptcha-response');
                if (resp) resp.innerHTML = t;
                document.querySelectorAll('.g-recaptcha-response').forEach(
                    function(el) {{ el.innerHTML = t; }}
                );
                // Fire the grecaptcha callback if registered
                try {{
                    var cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {{
                        Object.values(cfg.clients).forEach(function(c) {{
                            if (c && c.callback) c.callback(t);
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        """)
    elif captcha_type == "hcaptcha":
        await page.evaluate(f"""
            (function() {{
                var t = {js_token};
                var sel = 'textarea[name="h-captcha-response"], ' +
                          'textarea[name="g-recaptcha-response"]';
                document.querySelectorAll(sel).forEach(
                    function(el) {{ el.value = t; }}
                );
                // Fire hcaptcha callback
                try {{
                    if (window.hcaptcha) {{
                        Object.values(window.hcaptcha._state || {{}}).forEach(function(s) {{
                            if (s && s.response && s.onSuccess) s.onSuccess(t);
                        }});
                    }}
                }} catch(e) {{}}
            }})();
        """)

    await asyncio.sleep(0.5)


async def _is_captcha_solved(page: Page) -> bool:
    """Return True if a CAPTCHA response token is now present in the page."""
    try:
        return bool(await page.evaluate("""
            () => {
                const sels = [
                    '#g-recaptcha-response',
                    'textarea[name="g-recaptcha-response"]',
                    'textarea[name="h-captcha-response"]',
                    'input[name="cf-turnstile-response"]',
                ];
                for (const s of sels) {
                    const els = document.querySelectorAll(s);
                    for (const el of els) {
                        if (el && el.value && el.value.trim().length > 0) return true;
                    }
                }
                return false;
            }
        """))
    except Exception:
        return False


async def _wait_for_manual_captcha(page: Page, timeout: float = 180.0) -> bool:
    """
    Pause and wait for a human to solve the CAPTCHA in a visible browser window.

    Polls for a response token every couple of seconds and returns True as soon
    as one appears, or False if *timeout* seconds elapse first.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await _is_captcha_solved(page):
            return True
        await asyncio.sleep(2.0)
    # One last check in case it was solved right at the deadline
    return await _is_captcha_solved(page)


async def _solve_recaptcha_audio(page: Page) -> bool:
    """Solve reCAPTCHA v2 via the audio challenge + Google Speech-to-Text (free).

    Requires: SpeechRecognition and pydub (+ ffmpeg) installed.
    Returns True if the CAPTCHA was solved directly in the browser.
    """
    try:
        import io
        import speech_recognition as sr
    except ImportError:
        logger.warning("SpeechRecognition not installed — audio CAPTCHA disabled. "
                       "Run: pip install SpeechRecognition pydub  and install ffmpeg.")
        return False
    try:
        from pydub import AudioSegment
    except ImportError:
        logger.warning("pydub not installed — audio CAPTCHA disabled. "
                       "Run: pip install pydub  and install ffmpeg.")
        return False

    try:
        # Locate the anchor frame (checkbox widget)
        anchor_frame = None
        for frame in page.frames:
            if "recaptcha" in frame.url and "anchor" in frame.url:
                anchor_frame = frame
                break
        if not anchor_frame:
            logger.debug("Audio CAPTCHA: no anchor frame found (may be reCAPTCHA v3/invisible)")
            return False

        checkbox = await anchor_frame.query_selector("#recaptcha-anchor")
        if not checkbox:
            logger.debug("Audio CAPTCHA: #recaptcha-anchor not found in anchor frame")
            return False
        await checkbox.click()
        await asyncio.sleep(1.5)

        # Check if the easy-pass already ticked it
        passed = await anchor_frame.evaluate(
            "() => document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'"
        )
        if passed:
            logger.info("reCAPTCHA passed without challenge (easy-pass)")
            return True

        # Wait for the challenge iframe (bframe)
        bframe = None
        for _ in range(12):
            for frame in page.frames:
                if "recaptcha" in frame.url and "bframe" in frame.url:
                    bframe = frame
                    break
            if bframe:
                break
            await asyncio.sleep(0.5)
        if not bframe:
            logger.warning("Audio CAPTCHA: challenge iframe (bframe) not found")
            return False

        # Click the audio button
        audio_btn = await bframe.query_selector("#recaptcha-audio-button")
        if not audio_btn or not await audio_btn.is_visible():
            logger.warning("Audio CAPTCHA: audio button not visible (image challenge may be shown)")
            return False
        await audio_btn.click()
        await asyncio.sleep(1.5)

        # Grab the audio source URL from within the challenge iframe
        audio_url = await bframe.evaluate("""
            () => {
                const src = document.querySelector('#audio-source');
                if (src && src.src) return src.src;
                const link = document.querySelector('.rc-audiochallenge-tdownload-link a');
                if (link) return link.href;
                return null;
            }
        """)
        if not audio_url:
            logger.warning("Audio CAPTCHA: audio URL not found in challenge iframe")
            return False

        # Download the MP3 via Playwright's async HTTP client (non-blocking)
        try:
            api_response = await page.request.get(audio_url)
            mp3_bytes = await api_response.body()
        except Exception as exc:
            logger.warning("Audio CAPTCHA: download failed: %s", exc)
            return False

        # Convert MP3 → WAV in memory
        try:
            segment = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
            wav_buf = io.BytesIO()
            segment.export(wav_buf, format="wav")
            wav_buf.seek(0)
        except Exception as exc:
            logger.warning("Audio CAPTCHA: MP3→WAV conversion failed: %s", exc)
            return False

        # Transcribe with Google's free Speech-to-Text
        try:
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_buf) as source:
                audio_data = recognizer.record(source)
            transcript = recognizer.recognize_google(audio_data).strip().lower()
        except Exception as exc:
            logger.warning("Audio CAPTCHA: transcription failed: %s", exc)
            return False

        if not transcript:
            return False
        logger.info("Audio CAPTCHA transcript: %r", transcript)

        # Type the answer and submit
        response_input = await bframe.query_selector("#audio-response")
        if not response_input:
            return False
        await response_input.fill(transcript)
        await asyncio.sleep(0.3)

        verify_btn = await bframe.query_selector("#recaptcha-verify-button")
        if verify_btn:
            await verify_btn.click()
            await asyncio.sleep(2)

        # Confirm the checkbox is now ticked
        solved = await anchor_frame.evaluate(
            "() => document.querySelector('#recaptcha-anchor')?.getAttribute('aria-checked') === 'true'"
        )
        if solved:
            logger.info("Audio CAPTCHA solved successfully")
            return True

        logger.warning("Audio CAPTCHA: verify clicked but checkbox not ticked (wrong transcript or rate-limited)")
        return False

    except Exception as exc:
        logger.warning("Audio CAPTCHA exception: %s", exc)
        return False


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


async def _fill_dob_date_input(page: Page, profile: dict[str, str]) -> int:
    """
    Fill ``input[type='date']`` and combined DOB text fields (e.g. name*='dob').
    Separate month/day/year selects are handled by _FIELD_SELECTORS; this
    function handles the common case of a *single* DOB field.
    Returns the number of fields filled.
    """
    month = profile.get("dob_month", "").zfill(2)
    day   = profile.get("dob_day",   "").zfill(2)
    year  = profile.get("dob_year",  "")
    if not (month and day and year):
        return 0

    filled   = 0
    iso_date = f"{year}-{month}-{day}"   # YYYY-MM-DD  (HTML date input)
    mdy_date = f"{month}/{day}/{year}"   # MM/DD/YYYY  (common text mask)

    # ── input[type='date'] ────────────────────────────────────────────────
    for el in await page.query_selector_all("input[type='date']"):
        try:
            if not await el.is_visible() or not await el.is_enabled():
                continue
            if await el.input_value():
                continue
            await el.fill(iso_date)
            await _human_delay()
            filled += 1
        except Exception as exc:
            logger.debug("fill_dob date-input error: %s", exc)

    # ── Combined DOB text inputs ───────────────────────────────────────────
    _DOB_COMBINED_SELECTORS = [
        "input[name*='dob' i]:not([type='hidden']):not([type='date'])",
        "input[id*='dob' i]:not([type='hidden']):not([type='date'])",
        "input[name*='birthday' i]:not([type='hidden']):not([type='date'])",
        "input[id*='birthday' i]:not([type='hidden']):not([type='date'])",
        "input[name*='birthdate' i]:not([type='hidden']):not([type='date'])",
        "input[name*='birth_date' i]:not([type='hidden']):not([type='date'])",
        "input[name*='dateofbirth' i]:not([type='hidden']):not([type='date'])",
        "input[placeholder*='MM/DD/YYYY' i]",
        "input[placeholder*='Date of Birth' i]",
        "input[placeholder*='Birth Date' i]",
        "input[placeholder*='Birthday' i]",
    ]
    for sel in _DOB_COMBINED_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el is None or not await el.is_visible() or not await el.is_enabled():
                continue
            if await el.input_value():
                continue
            placeholder = (await el.get_attribute("placeholder") or "").upper()
            value = iso_date if placeholder.startswith("YYYY") else mdy_date
            await el.triple_click()
            await el.fill(value)
            await _human_delay()
            filled += 1
        except Exception as exc:
            logger.debug("fill_dob combined sel=%s error: %s", sel, exc)

    return filled


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


# US state abbreviation ↔ full-name lookup for smart state field filling
_US_STATES_ABBREV_TO_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_STATES_NAME_TO_ABBREV: dict[str, str] = {v: k for k, v in _US_STATES_ABBREV_TO_NAME.items()}


async def _fill_state(page: Page, state_value: str) -> None:
    """Fill state field — handles <select> and <input>, tries both abbrev and full name."""
    sv_upper = state_value.strip().upper()
    sv_title = state_value.strip().title()

    # Build ordered list of values to try: original, then alternate form
    candidates: list[str] = [state_value]
    if sv_upper in _US_STATES_ABBREV_TO_NAME:
        candidates.append(_US_STATES_ABBREV_TO_NAME[sv_upper])   # CA → California
    elif sv_title in _US_STATES_NAME_TO_ABBREV:
        candidates.append(_US_STATES_NAME_TO_ABBREV[sv_title])   # California → CA

    for sel in _STATE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el is None or not await el.is_visible():
                continue
            tag = await page.eval_on_selector(sel, "el => el.tagName.toLowerCase()")
            if tag == "select":
                filled = False
                for v in candidates:
                    for method in ("value", "label"):
                        try:
                            if method == "value":
                                await page.select_option(sel, value=v)
                            else:
                                await page.select_option(sel, label=v)
                            filled = True
                            break
                        except Exception:
                            pass
                    if filled:
                        break
                if not filled:
                    continue
            else:
                await page.fill(sel, state_value)
            await _human_delay()
            return
        except Exception as exc:
            logger.debug("fill_state selector=%s error=%s", sel, exc)


async def _fill_phone_smart(page: Page, phone: str) -> bool:
    """
    Fill phone fields using the format the input expects (detected from placeholder).
    Tries XXX-XXX-XXXX, (XXX) XXX-XXXX, XXX.XXX.XXXX, and raw digits.
    Returns True if a field was filled.
    """
    if not phone:
        return False
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False

    d = digits
    _PHONE_SELECTORS = [
        "input[type='tel']",
        "input[name*='phone' i]",
        "input[id*='phone' i]",
        "input[placeholder*='phone' i]",
        "input[autocomplete='tel']",
        "input[name*='mobile' i]",
        "input[name*='cell' i]",
    ]
    for sel in _PHONE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el is None or not await el.is_visible() or not await el.is_enabled():
                continue
            placeholder = (await el.get_attribute("placeholder") or "").lower()
            # Detect expected format from placeholder
            if re.search(r"\(\s*[x0]\{3\}\s*\)", placeholder) or re.search(r"\(\d{3}\)", placeholder):
                value = f"({d[:3]}) {d[3:6]}-{d[6:]}"
            elif "." in placeholder and placeholder.count(".") >= 2:
                value = f"{d[:3]}.{d[3:6]}.{d[6:]}"
            elif re.search(r"x{3}[\s-]x{3}[\s-]x{4}", placeholder) or re.search(r"0{3}[\s-]0{3}", placeholder):
                value = f"{d[:3]}-{d[3:6]}-{d[6:]}"
            else:
                value = f"{d[:3]}-{d[3:6]}-{d[6:]}"  # default: XXX-XXX-XXXX
            current = await el.input_value()
            if current:
                continue
            await el.triple_click()
            await el.fill(value)
            await _human_delay()
            return True
        except Exception as exc:
            logger.debug("fill_phone_smart sel=%s error=%s", sel, exc)
    return False


async def _dismiss_popup_overlays(page: Page) -> None:
    """
    Dismiss newsletter subscribe popups, exit-intent overlays, and generic
    modals that can block access to the actual entry form.
    """
    _OVERLAY_CLOSE_SELECTORS = [
        # Generic modal close / dismiss buttons
        "button[aria-label*='close' i]", "button[aria-label*='dismiss' i]",
        "button[class*='close' i]:not([class*='cookie'])",
        "[class*='modal'] button[class*='close' i]",
        "[class*='popup'] button[class*='close' i]",
        "[id*='modal'] button[class*='close' i]",
        ".modal-close", ".popup-close", ".dialog-close",
        "[data-dismiss='modal']", "[data-close]",
        # "No thanks" / skip patterns
        "button:has-text('No thanks')", "button:has-text('No Thanks')",
        "button:has-text('No, thanks')", "button:has-text('Skip')",
        "button:has-text('Maybe Later')", "button:has-text('Not Now')",
        "button:has-text('Close')", "button:has-text('Dismiss')",
        "a:has-text('No thanks')", "a:has-text('Skip')",
        "a:has-text('Maybe Later')", "a:has-text('Not Now')",
        # SVG / icon close buttons common in overlays
        "button svg[class*='close']", ".overlay button",
        ".newsletter-popup button", ".email-popup button",
        ".subscribe-popup button", ".opt-in button",
        # Specific well-known popup tools
        "#privy-popup-content button[data-behavior='close']",  # Privy
        ".pum-close",  # Popup Maker
        ".fancybox-close-small", ".mfp-close",  # Magnific / Fancybox
        ".klaviyo-close-form",  # Klaviyo
        "#attentive_creative button[aria-label*='close' i]",  # Attentive SMS
    ]
    for sel in _OVERLAY_CLOSE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass


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


# Aggregator blog sites whose article pages embed an external sweepstakes link
# in the article body.  When we land on one of these pages, we follow the first
# external link inside the post content to reach the actual entry form.
_AGGREGATOR_BLOG_DOMAINS: frozenset[str] = frozenset([
    "freebieshark.com", "freebiesharks.com",
    "iheartgiveaways.net", "thesweepstakesguy.com",
    "luckyattitudes.com", "thefrugalfreebies.com",
    "hip2save.com", "freebies4mom.com", "simplyfreestuff.com",
    "pennypinchinmom.com", "thriftynorthwestmom.com",
    "missfrugalmommy.com", "moneysavingmom.com",
    "contestblogger.com", "sweepstakesfanatics.com",
    "contestqueen.com", "giveawaymonkey.com",
    "winningbecause.com", "sweepstakescrazies.com",
    "contestchest.com", "contestgirl.com", "thegiveawaygeek.com",
    "contestalley.com", "nationalfamilyfun.com", "thewinningmom.com",
    "sweepstakesinsider.com", "singlemomsincome.com",
    "dailycheapskate.com", "budgetsavvydiva.com", "thriftyjinxy.com",
    "sweepstakeswithkathy.com", "giveawaybase.com", "winbigpicture.com",
    "winbigpicture.com",
])

# Social/shortener domains to skip when extracting article links
_LINK_SKIP_HOSTS: frozenset[str] = frozenset([
    "twitter.com", "x.com", "instagram.com", "facebook.com", "fb.com",
    "tiktok.com", "youtube.com", "youtu.be", "pinterest.com", "reddit.com",
    "t.co", "bit.ly", "ow.ly", "buff.ly", "tinyurl.com",
    "amazon.com", "amzn.to", "ebay.com", "etsy.com",
    "google.com", "google.co.uk",
])

# Keywords that signal a link is to an actual sweepstakes entry page
_SWEEP_LINK_KEYWORDS: frozenset[str] = frozenset([
    "sweepstake", "giveaway", "contest", "enter", "prize",
    "instant-win", "instant_win", "drawing", "sweeps", "promotion", "promo",
    "win-", "-win", "/win", "raffle",
])


async def _follow_article_sweepstake_link(page: Page) -> bool:
    """
    On aggregator blog article pages, find the first external link inside the
    post content area and navigate to it.  Returns True if navigation occurred.

    These sites post articles *about* a sweepstake with a hyperlink in the body
    (e.g. Freebie Shark) — there is no entry form on the article page itself.
    """
    try:
        host = urlparse(page.url).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        if host not in _AGGREGATOR_BLOG_DOMAINS:
            return False
    except Exception:
        return False

    _CONTENT_SELECTORS = [
        ".entry-content", ".post-content", ".article-content",
        ".post-body", ".entry-body", ".single-content",
        "article", ".content-area", "main",
    ]
    current_host = urlparse(page.url).netloc.lower()

    for container_sel in _CONTENT_SELECTORS:
        try:
            container = await page.query_selector(container_sel)
            if not container:
                continue
            links = await container.query_selector_all("a[href]")
            for link in links:
                try:
                    href = (await link.get_attribute("href") or "").strip()
                    if not href or href.startswith(("#", "javascript:", "mailto:")):
                        continue
                    full_url = urljoin(page.url, href)
                    link_host = urlparse(full_url).netloc.lower()
                    clean_host = link_host[4:] if link_host.startswith("www.") else link_host
                    # Must be a different external domain, not social/ad junk
                    if not link_host or link_host == current_host:
                        continue
                    if clean_host in _LINK_SKIP_HOSTS:
                        continue
                    if clean_host in _AGGREGATOR_BLOG_DOMAINS:
                        continue
                    text = (await link.text_content() or "").strip()
                    if len(text) < 6:
                        continue
                    # Only follow links that look like a sweepstakes entry page
                    text_lower = text.lower()
                    url_lower  = full_url.lower()
                    if not any(kw in text_lower or kw in url_lower for kw in _SWEEP_LINK_KEYWORDS):
                        continue
                    logger.info("Aggregator blog redirect: %s → %s", page.url, full_url)
                    await link.click()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                    except PlaywrightTimeout:
                        pass
                    await _wait_for_form(page)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


async def _try_click_through(page: Page) -> bool:
    """
    For interstitial pages with no form fields — click any visible entry/nav
    link or button to advance toward the actual form.  Returns True if clicked.
    """
    _INTERSTITIAL_SELECTORS = [
        # Explicit enter links
        "a:has-text('Enter Now')", "a:has-text('Enter Here')",
        "a:has-text('Click to Enter')", "a:has-text('Click Here to Enter')",
        "a:has-text('Enter the Sweepstakes')", "a:has-text('Enter Sweepstakes')",
        "a:has-text('Enter to Win')", "a:has-text('Enter Giveaway')",
        "a:has-text('Enter the Giveaway')", "a:has-text('Enter the Contest')",
        "a:has-text('Enter the Drawing')",
        "button:has-text('Enter Now')", "button:has-text('Enter Here')",
        "button:has-text('Enter to Win')", "button:has-text('Enter Sweepstakes')",
        # Generic forward-navigation links
        "a:has-text('Get Started')", "a:has-text('Start')",
        "a:has-text('Begin')", "a:has-text('Continue')",
        "a:has-text('Next')", "a:has-text('Proceed')",
        "a:has-text('Go')",
        # Class-based CTAs
        "a[class*='enter' i]", "a[class*='cta' i]",
        ".enter-btn", ".cta-btn", ".entry-btn",
        "a[class*='btn'][href]", "a[class*='button'][href]",
    ]
    for sel in _INTERSTITIAL_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except PlaywrightTimeout:
                    pass
                await asyncio.sleep(1.0)
                return True
        except Exception:
            pass
    return False


# ── Main entry function ───────────────────────────────────────────────────────

async def fill_and_submit(
    page: Page,
    profile: dict[str, str],
    captcha_solver=None,
    *,
    manual_captcha: bool = False,
    manual_captcha_timeout: float = 180.0,
    defer_captcha: bool = False,
) -> dict[str, Any]:
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
    manual_captcha:
        When True, pause on CAPTCHA and wait for a human to solve it in the
        (visible) browser window instead of skipping or auto-solving.
    manual_captcha_timeout:
        How long, in seconds, to wait for the human to solve the CAPTCHA.
    defer_captcha:
        When True (used by the headless first pass), an interactive CAPTCHA is
        *reported* — the function returns ``{"status": "needs_captcha"}`` right
        away instead of waiting — so a later visible pass can open a window only
        for the pages that actually need a human.

    Returns
    -------
    dict with 'status' key: "entered" | "captcha" | "needs_captcha" |
    "no_form" | "expired" | "error"
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeout:
        return {"status": "error", "message": "Page load timeout"}

    # ── Dismiss GDPR / cookie-consent banners and popup overlays ─────────────
    await _dismiss_gdpr_consent(page)
    await _dismiss_popup_overlays(page)

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

    # ── Aggregator blog redirect ──────────────────────────────────────────────
    # Freebie Shark, Hip2Save, etc. post articles that link to the actual form.
    # Follow the embedded external sweepstakes link to reach the real entry page.
    # If no qualifying link is found (tips/advice article), return no_form immediately
    # so we don't mistakenly detect the blog's newsletter CAPTCHA as a sweepstake.
    if not await _has_form_fields(page):
        _current_host = urlparse(page.url).netloc.lower()
        _host_clean   = _current_host[4:] if _current_host.startswith("www.") else _current_host
        if _host_clean in _AGGREGATOR_BLOG_DOMAINS:
            if not await _follow_article_sweepstake_link(page):
                return {"status": "no_form"}

    # ── "Enter Now" link follower ──────────────────────────────────────────────
    # Some aggregators or landing pages require clicking through to the form.
    # We allow relative URLs and JS-triggered links (no href restriction).
    if not await _has_form_fields(page):
        _ENTER_LINK_SELECTORS = [
            "a:has-text('Enter Now')",
            "a:has-text('Enter Here')",
            "a:has-text('Click to Enter')",
            "a:has-text('Click Here to Enter')",
            "a:has-text('Enter Sweepstakes')",
            "a:has-text('Enter the Sweepstakes')",
            "a:has-text('Enter Giveaway')",
            "a:has-text('Enter to Win')",
            "a:has-text('Enter the Giveaway')",
            "a:has-text('Enter the Contest')",
            "a:has-text('Enter the Drawing')",
            "a:has-text('Enter Drawing')",
            "button:has-text('Enter Now')",
            "button:has-text('Enter Here')",
            "button:has-text('Enter the Sweepstakes')",
            "button:has-text('Click Here to Enter')",
            "a.enter-link",
            "a[class*='enter' i]",
            "[data-action='enter']",
            ".enter-btn", ".cta-enter",
        ]
        for sel in _ENTER_LINK_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=12_000)
                    except PlaywrightTimeout:
                        pass
                    await _wait_for_form(page)
                    break
            except Exception:
                pass

    # ── Age gate bypass ───────────────────────────────────────────────────────
    await _bypass_age_gate(page, profile)

    # ── Wait for dynamic form to load ─────────────────────────────────────────
    await _wait_for_form(page)

    # ── Expiry check — skip closed sweepstakes immediately ────────────────────
    if await _is_expired(page):
        return {"status": "expired"}

    # ── CAPTCHA detection & solving ───────────────────────────────────────────
    # In manual mode, give async CAPTCHA widgets a few seconds to render so we
    # reliably catch (and pause on) real v2/hCaptcha checkboxes.
    if manual_captcha:
        captcha_type, sitekey, captcha_interactive = await _detect_captcha_wait(page)
    else:
        captcha_type, sitekey, captcha_interactive = await _detect_captcha(page)
    if captcha_type:
        solved = False

        if manual_captcha:
            if not captcha_interactive:
                # Invisible / score-based CAPTCHA (e.g. reCAPTCHA v3): there is
                # nothing for a human to click. Don't pause the run — just carry
                # on and let the form submit; the token is issued in the
                # background. This keeps manual mode from stopping on ordinary
                # entry pages that only carry an invisible v3 badge.
                logger.info(
                    "Non-interactive CAPTCHA (likely reCAPTCHA v3) on %s — "
                    "no manual step needed, continuing.", page.url,
                )
            elif defer_captcha:
                # Headless first pass: don't wait now — report that this page
                # needs a human so a later visible pass can open a window for it.
                logger.info(
                    "Interactive CAPTCHA on %s — deferring to the visible pass.",
                    page.url,
                )
                return {"status": "needs_captcha"}
            elif await _count_entry_fields(page) < 2:
                # A CAPTCHA is showing, but the page has no real entry form — just
                # a newsletter/comment box that happens to carry a reCAPTCHA (very
                # common on sweepstakes *blogs*). Don't make the user solve it;
                # skip the page instead of wasting their time.
                logger.info(
                    "CAPTCHA present but no real entry form on %s — looks like a "
                    "newsletter/comment box; skipping.", page.url,
                )
                return {"status": "no_form"}
            else:
                # Human-in-the-loop: pause and let the user solve it in the browser.
                logger.warning(
                    "🔒 CAPTCHA detected on %s — please solve it in the browser "
                    "window. Waiting up to %ds…",
                    page.url, int(manual_captcha_timeout),
                )
                solved = await _wait_for_manual_captcha(page, manual_captcha_timeout)
                if solved:
                    logger.warning("✓ CAPTCHA solved — continuing entry.")
                else:
                    return {"status": "captcha", "detail": "manual solve timed out"}
        else:
            # Always try the free audio challenge first for reCAPTCHA
            if captcha_type == "recaptcha":
                solved = await _solve_recaptcha_audio(page)

            # Fall back to configured external service if audio didn't work
            if not solved and captcha_solver and captcha_solver.enabled and sitekey:
                logger.info(
                    "Solving %s via %s (sitekey: %s…)",
                    captcha_type, captcha_solver.service, (sitekey or "")[:8],
                )
                if captcha_type == "hcaptcha":
                    token = await asyncio.get_running_loop().run_in_executor(
                        None, captcha_solver.solve_hcaptcha, sitekey, page.url
                    )
                else:
                    token = await asyncio.get_running_loop().run_in_executor(
                        None, captcha_solver.solve_recaptcha, sitekey, page.url
                    )
                if token:
                    await _inject_captcha_token(page, captcha_type, token)
                    solved = True
                else:
                    return {"status": "captcha", "detail": "solver returned no token"}

            if not solved:
                return {"status": "captcha"}

    # ── Multi-step form loop (max 8 steps) ────────────────────────────────────
    total_fields_filled = 0
    submitted = False

    for step in range(8):
        # Check for expiry/success on steps after the first
        if step > 0:
            if await _detect_success(page):
                return {"status": "entered", "steps": step}
            if await _is_expired(page):
                return {"status": "expired"}

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

        # Smart phone formatting (detect format from placeholder)
        phone = profile.get("phone", "")
        if phone:
            phone_filled = await _fill_phone_smart(page, phone)
            if phone_filled:
                fields_filled += 1

        # date-type DOB inputs and combined DOB text fields
        dob_filled = await _fill_dob_date_input(page, profile)
        fields_filled += dob_filled

        # Full name field (fallback when first+last fields not found)
        if fields_filled < 2:
            fn_filled = await _fill_full_name(page, profile)
            if fn_filled:
                fields_filled += 1

        # Gender field
        gender = profile.get("gender", "M")
        await _handle_gender(page, gender)

        # Dismiss any popup overlay that may have appeared after filling
        await _dismiss_popup_overlays(page)
        await _check_terms(page)

        total_fields_filled += fields_filled

        # Try submitting
        clicked = await _click_submit(page)
        if clicked:
            # Wait for SPA transitions or page loads after submit
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            except PlaywrightTimeout:
                await asyncio.sleep(2.0)
            # Check if we landed on a success page
            if await _detect_success(page):
                return {"status": "entered", "steps": step + 1}
            # Manual mode: a CAPTCHA often appears only AFTER clicking submit.
            # Only bother the user if we actually filled a real entry form (≥2
            # fields) — otherwise it's a newsletter/comment CAPTCHA, not an entry.
            if manual_captcha and not defer_captcha and (total_fields_filled >= 2):
                ct2, _sk2, ci2 = await _detect_captcha_wait(page, timeout=4.0)
                if ct2 and ci2 and not await _is_captcha_solved(page):
                    logger.warning(
                        "🔒 CAPTCHA appeared after submit on %s — please solve it. "
                        "Waiting up to %ds…", page.url, int(manual_captcha_timeout),
                    )
                    if await _wait_for_manual_captcha(page, manual_captcha_timeout):
                        logger.warning("✓ CAPTCHA solved — resubmitting.")
                        await _click_submit(page)
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                        except PlaywrightTimeout:
                            await asyncio.sleep(2.0)
                        if await _detect_success(page):
                            return {"status": "entered", "steps": step + 1}
                    else:
                        return {"status": "captcha", "detail": "manual solve timed out"}
            # Check if another step appeared
            next_clicked = await _try_next_step(page)
            if not next_clicked:
                # No next step, no clear success — assume entered
                submitted = True
                break
        else:
            # No submit found — try next/continue step button
            next_clicked = await _try_next_step(page)
            if not next_clicked:
                if fields_filled == 0:
                    # No fields, no nav buttons — try clicking through interstitials
                    clicked_through = await _try_click_through(page)
                    if not clicked_through:
                        break
                    # Clicked something; loop again to check new page
                else:
                    break

    if total_fields_filled == 0 and not submitted:
        return {"status": "no_form"}

    if not submitted:
        return {"status": "error", "message": "Could not find or complete submit"}

    return {"status": "entered"}


async def enter_sweepstake(
    page: Page,
    url: str,
    profile: dict[str, str],
    captcha_solver=None,
    *,
    manual_captcha: bool = False,
    manual_captcha_timeout: float = 180.0,
    defer_captcha: bool = False,
) -> dict[str, Any]:
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

            result = await fill_and_submit(
                page, profile, captcha_solver=captcha_solver,
                manual_captcha=manual_captcha,
                manual_captcha_timeout=manual_captcha_timeout,
                defer_captcha=defer_captcha,
            )

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
