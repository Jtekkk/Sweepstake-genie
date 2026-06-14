"""
Sweepstakes aggregator scrapers.

Each scraper returns a list of dicts::

    [{"url": "https://...", "title": "Win a Car!", "source": "sweepstakestoday"}, ...]

The main entry point is :func:`discover_all`, which calls every scraper,
deduplicates by URL, and returns the combined list.

Sources (17 total):
  Aggregators  : sweepstakestoday, online-sweepstakes, contestgirl,
                 contestbee, contestchest, sweepstakesadvantage,
                 theprizefinder, wincalendar, instantwinningwebsites,
                 sweepstakesfanatics, contestqueen
  RSS / Blogs  : sweetiessweeps, giveawayfrenzy, iheartgiveaways,
                 freesweepstakesonline, slickdeals, reddit_sweepstakes
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
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_TIMEOUT = 20   # seconds per HTTP request
_DELAY   = 1.0  # seconds between requests to the same host

# Domains we never want to enter as sweepstake URLs — social-media entries
# (like/share/comment giveaways) can't be handled by a form filler.
_SKIP_DOMAINS = {
    "twitter.com", "x.com",
    "instagram.com",
    "facebook.com", "fb.com",
    "tiktok.com",
    "youtube.com", "youtu.be",
    "pinterest.com",
    "reddit.com",
    "google.com", "google.co.uk",
    "t.co", "bit.ly", "ow.ly", "buff.ly",
}


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _get(url: str, session: requests.Session) -> BeautifulSoup | None:
    """Fetch *url* and return a BeautifulSoup, or None on failure."""
    try:
        resp = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _should_skip(url: str) -> bool:
    """Return True if *url* should be excluded (social media etc.)."""
    host = urlparse(url).netloc.lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in _SKIP_DOMAINS)


def _extract_external_links(
    soup: BeautifulSoup,
    page_url: str,
    base_host: str,
    seen: set[str],
    source: str,
    min_title_len: int = 5,
    container_selector: str | None = None,
) -> list[dict[str, Any]]:
    """
    Pull all outbound links from *soup* that leave *base_host*.
    Deduplicates against *seen* (modified in-place).
    """
    results: list[dict[str, Any]] = []

    root = soup
    if container_selector:
        found = soup.select_one(container_selector)
        if found:
            root = found  # type: ignore[assignment]

    for anchor in root.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue

        full_url = urljoin(page_url, href)
        parsed   = urlparse(full_url)

        if not parsed.netloc or parsed.netloc == base_host:
            continue
        if _should_skip(full_url):
            continue
        if full_url in seen:
            continue

        title = anchor.get_text(strip=True)
        if len(title) < min_title_len:
            continue

        seen.add(full_url)
        results.append({"url": full_url, "title": title, "source": source})

    return results


def _scrape_pages(
    base_url: str,
    page_paths: list[str],
    source: str,
    *,
    container_selector: str | None = None,
    min_title_len: int = 5,
) -> list[dict[str, Any]]:
    """
    Generic multi-page scraper: visits each path under *base_url* and
    collects all external outbound links.
    """
    session   = requests.Session()
    base_host = urlparse(base_url).netloc
    results:  list[dict[str, Any]] = []
    seen:     set[str] = set()

    for path in page_paths:
        page_url = path if path.startswith("http") else base_url + path
        soup = _get(page_url, session)
        if soup is None:
            continue
        found = _extract_external_links(
            soup, page_url, base_host, seen, source,
            min_title_len=min_title_len,
            container_selector=container_selector,
        )
        results.extend(found)
        time.sleep(_DELAY)

    logger.info("%s: discovered %d sweepstakes", source, len(results))
    return results


def _scrape_rss(feed_url: str, source: str) -> list[dict[str, Any]]:
    """
    Parse an RSS or Atom feed URL and return a list of entry dicts.
    Handles both <item> (RSS 2.0) and <entry> (Atom) elements.
    """
    session = requests.Session()
    results: list[dict[str, Any]] = []

    try:
        resp = session.get(feed_url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
        return results

    soup = BeautifulSoup(resp.text, "xml")

    # RSS 2.0
    for item in soup.find_all("item"):
        link_tag  = item.find("link")
        title_tag = item.find("title")
        if not (link_tag and title_tag):
            continue
        url   = (link_tag.string or link_tag.get_text()).strip()
        title = title_tag.get_text(strip=True)
        if url.startswith("http") and not _should_skip(url):
            results.append({"url": url, "title": title, "source": source})

    # Atom
    for entry in soup.find_all("entry"):
        link_tag  = entry.find("link")
        title_tag = entry.find("title")
        if not (link_tag and title_tag):
            continue
        url   = link_tag.get("href", "").strip()
        title = title_tag.get_text(strip=True)
        if url.startswith("http") and not _should_skip(url):
            results.append({"url": url, "title": title, "source": source})

    logger.info("%s (RSS): discovered %d sweepstakes", source, len(results))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Individual site scrapers
# ══════════════════════════════════════════════════════════════════════════════

def scrape_sweepstakestoday() -> list[dict[str, Any]]:
    """https://www.sweepstakestoday.com — large US sweepstakes listing site."""
    return _scrape_pages(
        "https://www.sweepstakestoday.com",
        ["/", "/sweepstakes/", "/contests/", "/sweepstakes/page/2/", "/sweepstakes/page/3/"],
        "sweepstakestoday",
    )


def scrape_online_sweepstakes() -> list[dict[str, Any]]:
    """https://www.online-sweepstakes.com — paginated sweepstakes database."""
    pages = [f"/sweepstakes/pg/{n}/" for n in range(1, 11)]
    return _scrape_pages(
        "https://www.online-sweepstakes.com",
        pages,
        "online-sweepstakes",
    )


def scrape_contestgirl() -> list[dict[str, Any]]:
    """https://www.contestgirl.com — long-running sweepstakes aggregator."""
    return _scrape_pages(
        "https://www.contestgirl.com",
        ["/", "/contests/", "/sweepstakes/", "/contests/page/2/"],
        "contestgirl",
        container_selector="main, #content, .content",
    )


def scrape_contestbee() -> list[dict[str, Any]]:
    """https://www.contestbee.com — active sweepstakes database with many categories."""
    return _scrape_pages(
        "https://www.contestbee.com",
        [
            "/sweepstakes/",
            "/sweepstakes/?page=2",
            "/sweepstakes/?page=3",
            "/sweepstakes/?page=4",
            "/sweepstakes/?page=5",
            "/instant-win/",
            "/cash/",
        ],
        "contestbee",
        container_selector=".sweepstakes-list, .contest-list, main",
    )


def scrape_contestchest() -> list[dict[str, Any]]:
    """https://www.contestchest.com — daily updated sweepstakes listings."""
    return _scrape_pages(
        "https://www.contestchest.com",
        [
            "/",
            "/sweepstakes/",
            "/sweepstakes/page/2/",
            "/sweepstakes/page/3/",
            "/instant-win-games/",
        ],
        "contestchest",
        container_selector=".entry-content, main, #main",
    )


def scrape_sweepstakesadvantage() -> list[dict[str, Any]]:
    """https://www.sweepstakesadvantage.com — one of the largest sweepstakes databases."""
    pages = [f"/sweepstakes/listing/{n}/" for n in range(1, 9)]
    pages += ["/", "/sweepstakes/"]
    return _scrape_pages(
        "https://www.sweepstakesadvantage.com",
        pages,
        "sweepstakesadvantage",
        container_selector=".listing, #listingTable, main",
    )


def scrape_theprizefinder() -> list[dict[str, Any]]:
    """https://www.theprizefinder.com — UK-origin aggregator with many US sweepstakes."""
    return _scrape_pages(
        "https://www.theprizefinder.com",
        [
            "/",
            "/competitions/",
            "/competitions/page/2/",
            "/competitions/page/3/",
            "/competitions/page/4/",
        ],
        "theprizefinder",
        container_selector=".competition-list, main, #content",
    )


def scrape_wincalendar() -> list[dict[str, Any]]:
    """https://www.wincalendar.com — sweepstakes calendar organized by deadline."""
    return _scrape_pages(
        "https://www.wincalendar.com",
        ["/", "/sweepstakes/", "/win/", "/winnings/"],
        "wincalendar",
        container_selector="table, .main-content, main",
    )


def scrape_instantwinningwebsites() -> list[dict[str, Any]]:
    """https://instantwinningwebsites.com — specializes in instant-win sweepstakes."""
    return _scrape_pages(
        "https://instantwinningwebsites.com",
        ["/", "/instant-win-games/", "/?paged=2", "/?paged=3"],
        "instantwinningwebsites",
        container_selector=".entry-content, main, .site-main",
    )


def scrape_sweepstakesfanatics() -> list[dict[str, Any]]:
    """https://sweepstakesfanatics.com — community-curated sweepstakes."""
    return _scrape_pages(
        "https://sweepstakesfanatics.com",
        ["/", "/sweepstakes/", "/sweepstakes/page/2/", "/sweepstakes/page/3/"],
        "sweepstakesfanatics",
        container_selector=".entry-content, main",
    )


def scrape_contestqueen() -> list[dict[str, Any]]:
    """https://contestqueen.com — sweepstakes and contest aggregator."""
    return _scrape_pages(
        "https://contestqueen.com",
        ["/", "/contests/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "contestqueen",
        container_selector="main, .site-content",
    )


def scrape_winbigpicture() -> list[dict[str, Any]]:
    """https://winbigpicture.com — daily sweepstakes and giveaway listings."""
    return _scrape_pages(
        "https://winbigpicture.com",
        ["/", "/sweepstakes/", "/page/2/"],
        "winbigpicture",
        container_selector="main, .entry-content",
    )


def scrape_sweetiessweeps() -> list[dict[str, Any]]:
    """https://sweetiessweeps.com — popular sweepstakes blog (RSS + pages)."""
    results: list[dict[str, Any]] = []
    results.extend(_scrape_rss("https://sweetiessweeps.com/feed/", "sweetiessweeps"))
    results.extend(
        _scrape_pages(
            "https://sweetiessweeps.com",
            ["/", "/page/2/", "/page/3/"],
            "sweetiessweeps",
            container_selector=".entry-content, article",
        )
    )
    # Deduplicate
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    return deduped


def scrape_giveawayfrenzy() -> list[dict[str, Any]]:
    """https://giveawayfrenzy.com — high-volume daily giveaway listings."""
    results: list[dict[str, Any]] = []
    results.extend(_scrape_rss("https://giveawayfrenzy.com/feed/", "giveawayfrenzy"))
    results.extend(
        _scrape_pages(
            "https://giveawayfrenzy.com",
            ["/", "/page/2/", "/page/3/", "/page/4/"],
            "giveawayfrenzy",
            container_selector="main, .site-main",
        )
    )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    return deduped


def scrape_iheartgiveaways() -> list[dict[str, Any]]:
    """https://iheartgiveaways.net — giveaway and sweepstakes blog with RSS."""
    results: list[dict[str, Any]] = []
    results.extend(_scrape_rss("https://iheartgiveaways.net/feed/", "iheartgiveaways"))
    results.extend(
        _scrape_pages(
            "https://iheartgiveaways.net",
            ["/", "/page/2/", "/page/3/"],
            "iheartgiveaways",
            container_selector="main, .entry-content",
        )
    )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    return deduped


def scrape_freesweepstakesonline() -> list[dict[str, Any]]:
    """https://www.freesweepstakesonline.com — categorized free sweepstakes."""
    return _scrape_pages(
        "https://www.freesweepstakesonline.com",
        [
            "/",
            "/cash-sweepstakes/",
            "/prize-sweepstakes/",
            "/instant-win/",
            "/trip-sweepstakes/",
        ],
        "freesweepstakesonline",
        container_selector=".sweepstake-list, main, #content",
    )


def scrape_slickdeals() -> list[dict[str, Any]]:
    """https://slickdeals.net — freebies and sweepstakes section of the deal forum."""
    return _scrape_pages(
        "https://slickdeals.net",
        [
            "/forums/f/freebies/",
            "/forums/f/giveaways/",
        ],
        "slickdeals",
        container_selector=".threadList, .dealList, main",
        min_title_len=8,
    )


def scrape_reddit_sweepstakes() -> list[dict[str, Any]]:
    """https://www.reddit.com/r/sweepstakes — community-posted sweepstakes (JSON API)."""
    source  = "reddit_sweepstakes"
    results: list[dict[str, Any]] = []
    session = requests.Session()

    urls = [
        "https://www.reddit.com/r/sweepstakes/new.json?limit=100",
        "https://www.reddit.com/r/giveaways/new.json?limit=100",
        "https://www.reddit.com/r/sweepstakes/hot.json?limit=100",
    ]

    headers = {**_HEADERS, "Accept": "application/json"}
    seen: set[str] = set()

    for api_url in urls:
        try:
            resp = session.get(api_url, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Reddit fetch failed for %s: %s", api_url, exc)
            time.sleep(_DELAY)
            continue

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            pd  = post.get("data", {})
            url = pd.get("url", "")
            if not url or url in seen:
                continue
            if _should_skip(url):
                continue
            # Skip self-posts (no external link)
            if pd.get("is_self"):
                continue
            title = pd.get("title", "")
            if len(title) < 5:
                continue
            seen.add(url)
            results.append({"url": url, "title": title, "source": source})

        time.sleep(_DELAY)

    logger.info("%s: discovered %d sweepstakes", source, len(results))
    return results


# ── Master discovery function ─────────────────────────────────────────────────

#: All scrapers in priority order.  Each entry is (display_name, callable).
_SCRAPERS: list[tuple[str, Any]] = [
    ("sweepstakestoday",        scrape_sweepstakestoday),
    ("online-sweepstakes",      scrape_online_sweepstakes),
    ("contestgirl",             scrape_contestgirl),
    ("contestbee",              scrape_contestbee),
    ("contestchest",            scrape_contestchest),
    ("sweepstakesadvantage",    scrape_sweepstakesadvantage),
    ("theprizefinder",          scrape_theprizefinder),
    ("wincalendar",             scrape_wincalendar),
    ("instantwinningwebsites",  scrape_instantwinningwebsites),
    ("sweepstakesfanatics",     scrape_sweepstakesfanatics),
    ("contestqueen",            scrape_contestqueen),
    ("winbigpicture",           scrape_winbigpicture),
    ("sweetiessweeps",          scrape_sweetiessweeps),
    ("giveawayfrenzy",          scrape_giveawayfrenzy),
    ("iheartgiveaways",         scrape_iheartgiveaways),
    ("freesweepstakesonline",   scrape_freesweepstakesonline),
    ("slickdeals",              scrape_slickdeals),
    ("reddit_sweepstakes",      scrape_reddit_sweepstakes),
]


def discover_all() -> list[dict[str, Any]]:
    """
    Run every scraper, deduplicate results by URL, and return the combined list.

    Returns
    -------
    list[dict]
        Each dict has keys ``url``, ``title``, and ``source``.
    """
    combined:  list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for name, scraper_fn in _SCRAPERS:
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

    logger.info("discover_all: %d unique sweepstakes found total", len(combined))
    return combined


def list_sources() -> list[str]:
    """Return the names of all registered scrapers."""
    return [name for name, _ in _SCRAPERS]
