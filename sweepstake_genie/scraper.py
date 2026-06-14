"""
Sweepstakes aggregator scrapers.

Each scraper returns a list of dicts::

    [{"url": "https://...", "title": "Win a Car!", "source": "sweepstakestoday"}, ...]

The main entry point is :func:`discover_all`, which calls every scraper,
deduplicates by URL, and returns the combined list.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Shared request config ─────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_TIMEOUT = 20  # seconds per HTTP request
_DELAY   = 1.5  # seconds between requests to the same host


# ── Helper ────────────────────────────────────────────────────────────────────

def _get(url: str, session: requests.Session) -> BeautifulSoup | None:
    """Fetch *url* and return a BeautifulSoup, or None on failure."""
    try:
        resp = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _clean_url(href: str, base: str) -> str:
    """Resolve *href* relative to *base* and strip query/fragment noise."""
    return urljoin(base, href).split("?")[0].rstrip("/")


def _is_external(url: str, base_host: str) -> bool:
    """Return True if *url* belongs to a different host than *base_host*."""
    return urlparse(url).netloc not in ("", base_host)


# ── Scraper: SweepstakesToday ─────────────────────────────────────────────────

def scrape_sweepstakestoday() -> list[dict[str, Any]]:
    """
    Scrape https://www.sweepstakestoday.com for active sweepstake listings.

    The site lists contests in ``<div class="contest-list">`` blocks with
    anchor tags containing the sweepstake title and external URL.
    """
    base_url = "https://www.sweepstakestoday.com"
    source   = "sweepstakestoday"
    results: list[dict[str, Any]] = []

    session = requests.Session()

    # Try the main listing pages
    pages_to_try = [
        base_url + "/",
        base_url + "/sweepstakes/",
        base_url + "/contests/",
    ]

    seen: set[str] = set()

    for page_url in pages_to_try:
        soup = _get(page_url, session)
        if soup is None:
            continue

        # Look for links that point away from the aggregator (the real contests)
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith("#"):
                continue

            full_url = urljoin(base_url, href)
            parsed   = urlparse(full_url)

            # We want outbound links (external sweepstake sites)
            if parsed.netloc and parsed.netloc not in urlparse(base_url).netloc:
                if full_url in seen:
                    continue
                seen.add(full_url)
                title = anchor.get_text(strip=True) or "Unknown"
                if len(title) > 3:  # skip nav/footer noise
                    results.append({"url": full_url, "title": title, "source": source})

        time.sleep(_DELAY)

    logger.info("sweepstakestoday: discovered %d sweepstakes", len(results))
    return results


# ── Scraper: Online-Sweepstakes ───────────────────────────────────────────────

def scrape_online_sweepstakes() -> list[dict[str, Any]]:
    """
    Scrape https://www.online-sweepstakes.com for active sweepstake listings.

    The site presents sweepstakes inside ``<div class="sweepstakes-list">`` or
    similar containers with title links.
    """
    base_url = "https://www.online-sweepstakes.com"
    source   = "online-sweepstakes"
    results: list[dict[str, Any]] = []

    session = requests.Session()

    pages_to_try = [
        base_url + "/",
        base_url + "/sweepstakes/pg/1/",
        base_url + "/sweepstakes/pg/2/",
        base_url + "/sweepstakes/pg/3/",
    ]

    seen: set[str] = set()

    for page_url in pages_to_try:
        soup = _get(page_url, session)
        if soup is None:
            continue

        # Primary: look for internal listing links, then follow them to get
        # the external destination URL stored in meta or a redirect anchor.
        for anchor in soup.find_all("a", href=True):
            href  = anchor["href"].strip()
            if not href or href.startswith(("#", "javascript")):
                continue

            full_url = urljoin(base_url, href)
            parsed   = urlparse(full_url)

            # Collect external links directly listed on the page
            if parsed.netloc and parsed.netloc not in urlparse(base_url).netloc:
                if full_url in seen:
                    continue
                seen.add(full_url)
                title = anchor.get_text(strip=True) or "Unknown"
                if len(title) > 3:
                    results.append({"url": full_url, "title": title, "source": source})

        time.sleep(_DELAY)

    logger.info("online-sweepstakes: discovered %d sweepstakes", len(results))
    return results


# ── Scraper: ContestGirl ──────────────────────────────────────────────────────

def scrape_contestgirl() -> list[dict[str, Any]]:
    """
    Scrape https://www.contestgirl.com for active sweepstake listings.

    ContestGirl organises contests by category; the front page lists recent
    additions.  We look for outbound anchor tags in content areas.
    """
    base_url = "https://www.contestgirl.com"
    source   = "contestgirl"
    results: list[dict[str, Any]] = []

    session = requests.Session()

    pages_to_try = [
        base_url + "/",
        base_url + "/contests/",
        base_url + "/sweepstakes/",
    ]

    seen: set[str] = set()

    for page_url in pages_to_try:
        soup = _get(page_url, session)
        if soup is None:
            continue

        # ContestGirl wraps each entry in a container; grab all external links
        content_area = (
            soup.find("div", class_=lambda c: c and "content" in c)
            or soup.find("main")
            or soup.find("body")
        )
        if content_area is None:
            continue

        for anchor in content_area.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("#", "javascript")):
                continue

            full_url = urljoin(base_url, href)
            parsed   = urlparse(full_url)

            if parsed.netloc and parsed.netloc not in urlparse(base_url).netloc:
                if full_url in seen:
                    continue
                seen.add(full_url)
                title = anchor.get_text(strip=True) or "Unknown"
                if len(title) > 3:
                    results.append({"url": full_url, "title": title, "source": source})

        time.sleep(_DELAY)

    logger.info("contestgirl: discovered %d sweepstakes", len(results))
    return results


# ── Master discovery function ─────────────────────────────────────────────────

def discover_all() -> list[dict[str, Any]]:
    """
    Call every scraper, deduplicate results by URL, and return the combined list.

    Returns
    -------
    list[dict]
        Each dict has keys ``url``, ``title``, and ``source``.
    """
    combined: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    scrapers = [
        ("sweepstakestoday",   scrape_sweepstakestoday),
        ("online-sweepstakes", scrape_online_sweepstakes),
        ("contestgirl",        scrape_contestgirl),
    ]

    for name, scraper_fn in scrapers:
        try:
            entries = scraper_fn()
        except Exception as exc:
            logger.error("Scraper '%s' failed: %s", name, exc)
            entries = []

        for entry in entries:
            url = entry.get("url", "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined.append(entry)

    logger.info("discover_all: %d unique sweepstakes found", len(combined))
    return combined
