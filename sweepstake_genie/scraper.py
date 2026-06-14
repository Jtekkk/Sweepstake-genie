"""
Sweepstakes aggregator scrapers — 50+ sources.

Sources are split into three categories:
  _PAGE_SOURCES  — aggregator/listing sites scraped by following external links
  _RSS_SOURCES   — WordPress/Atom/RSS blogs; fetched via feed URL
  _REDDIT_SUBS   — subreddits pulled from Reddit's JSON API

The main entry point is :func:`discover_all`.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Request config ────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection":      "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
_TIMEOUT = 20
_DELAY   = 1.0

# Domains we never want — social-media entries can't be form-filled.
_SKIP_DOMAINS = {
    "twitter.com", "x.com",
    "instagram.com",
    "facebook.com", "fb.com",
    "tiktok.com",
    "youtube.com", "youtu.be",
    "pinterest.com",
    "reddit.com",
    "google.com", "google.co.uk",
    "t.co", "bit.ly", "ow.ly", "buff.ly", "tinyurl.com",
    "linkedin.com",
    "snapchat.com",
}


# ══════════════════════════════════════════════════════════════════════════════
# Source configuration tables  ← add new sources here, no new functions needed
# ══════════════════════════════════════════════════════════════════════════════

# Each entry: name, base URL, list of page paths, optional CSS selector for
# the content area (narrows link search and reduces nav/footer noise).
_PAGE_SOURCES: list[dict[str, Any]] = [

    # ── Classic aggregators ──────────────────────────────────────────────────
    {
        "name": "sweepstakestoday",
        "base": "https://www.sweepstakestoday.com",
        "pages": ["/", "/sweepstakes/", "/contests/",
                  "/sweepstakes/page/2/", "/sweepstakes/page/3/",
                  "/sweepstakes/page/4/", "/sweepstakes/page/5/"],
    },
    {
        "name": "online-sweepstakes",
        "base": "https://www.online-sweepstakes.com",
        "pages": [f"/sweepstakes/pg/{n}/" for n in range(1, 16)],
    },
    {
        "name": "contestgirl",
        "base": "https://www.contestgirl.com",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/contests/page/2/", "/contests/page/3/"],
        "selector": "main, #content, .content",
    },
    {
        "name": "contestbee",
        "base": "https://www.contestbee.com",
        "pages": ["/sweepstakes/", "/sweepstakes/?page=2",
                  "/sweepstakes/?page=3", "/sweepstakes/?page=4",
                  "/sweepstakes/?page=5", "/instant-win/", "/cash/",
                  "/trip/", "/gift-card/"],
        "selector": ".sweepstakes-list, .contest-list, main",
    },
    {
        "name": "contestchest",
        "base": "https://www.contestchest.com",
        "pages": ["/", "/sweepstakes/", "/sweepstakes/page/2/",
                  "/sweepstakes/page/3/", "/instant-win-games/"],
        "selector": ".entry-content, main, #main",
    },
    {
        "name": "sweepstakesadvantage",
        "base": "https://www.sweepstakesadvantage.com",
        "pages": ["/sweepstakes/"] + [f"/sweepstakes/listing/{n}/" for n in range(1, 16)],
        "selector": ".listing, #listingTable, main",
    },
    {
        "name": "theprizefinder",
        "base": "https://www.theprizefinder.com",
        "pages": ["/", "/competitions/", "/competitions/page/2/",
                  "/competitions/page/3/", "/competitions/page/4/",
                  "/competitions/page/5/"],
        "selector": ".competition-list, main, #content",
    },
    {
        "name": "wincalendar",
        "base": "https://www.wincalendar.com",
        "pages": ["/", "/sweepstakes/", "/win/", "/winnings/",
                  "/sweepstakes/page/2/"],
        "selector": "table, .main-content, main",
    },
    {
        "name": "instantwinningwebsites",
        "base": "https://instantwinningwebsites.com",
        "pages": ["/", "/instant-win-games/",
                  "/?paged=2", "/?paged=3", "/?paged=4"],
        "selector": ".entry-content, main, .site-main",
    },
    {
        "name": "sweepstakesfanatics",
        "base": "https://sweepstakesfanatics.com",
        "pages": ["/", "/sweepstakes/", "/sweepstakes/page/2/",
                  "/sweepstakes/page/3/", "/sweepstakes/page/4/"],
        "selector": ".entry-content, main",
    },
    {
        "name": "contestqueen",
        "base": "https://contestqueen.com",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .site-content",
    },
    {
        "name": "winbigpicture",
        "base": "https://winbigpicture.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "freesweepstakesonline",
        "base": "https://www.freesweepstakesonline.com",
        "pages": ["/", "/cash-sweepstakes/", "/prize-sweepstakes/",
                  "/instant-win/", "/trip-sweepstakes/",
                  "/gift-card-sweepstakes/", "/car-sweepstakes/"],
        "selector": ".sweepstake-list, main, #content",
    },
    {
        "name": "slickdeals",
        "base": "https://slickdeals.net",
        "pages": ["/forums/f/freebies/",
                  "/forums/f/giveaways/",
                  "/forums/f/freebies/?page=2"],
        "selector": ".threadList, .dealList, main",
    },

    # ── More aggregators ─────────────────────────────────────────────────────
    {
        "name": "sweepstakeslovers",
        "base": "https://www.sweepstakeslovers.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thecontestguy",
        "base": "https://www.thecontestguy.com",
        "pages": ["/", "/sweepstakes/", "/contests/",
                  "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "contestcorner",
        "base": "https://www.contestcorner.ca",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweepstakeshunter",
        "base": "https://www.sweepstakeshunter.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesden",
        "base": "https://www.sweepstakesden.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweepstakescastle",
        "base": "https://www.sweepstakescastle.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "sweepstakescrazies",
        "base": "https://sweepstakescrazies.com",
        "pages": ["/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "winafreebee",
        "base": "https://www.winafreebee.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "1clickwin",
        "base": "https://www.1clickwin.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "supersweepstakes",
        "base": "https://www.supersweepstakes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesmania",
        "base": "https://sweepstakesmania.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "sweepslist",
        "base": "https://www.sweepslist.com",
        "pages": ["/", "/sweepstakes/", "/page/2/"],
        "selector": "main",
    },
    {
        "name": "giveawaymonkey",
        "base": "https://giveawaymonkey.com",
        "pages": ["/", "/giveaways/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "winningbecause",
        "base": "https://www.winningbecause.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "contestblogger",
        "base": "https://www.contestblogger.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "afullcup",
        "base": "https://www.afullcup.com",
        "pages": ["/forums/sweepstakes/", "/forums/sweepstakes/?page=2",
                  "/forums/sweepstakes/?page=3"],
        "selector": ".forum-content, main, #content",
    },
    {
        "name": "freestuff",
        "base": "https://www.freestuff.com",
        "pages": ["/sweepstakes/", "/contests/", "/page/2/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "hip2save_sweeps",
        "base": "https://hip2save.com",
        "pages": ["/category/sweepstakes/",
                  "/category/sweepstakes/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "freebies4mom",
        "base": "https://freebies4mom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "simplyfreestuff",
        "base": "https://www.simplyfreestuff.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "thefrugalfreebies",
        "base": "https://thefrugalfreebies.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/",
                  "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "pennypinchinmom",
        "base": "https://www.pennypinchinmom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/"],
        "selector": "main",
    },
    {
        "name": "thriftynorthwestmom",
        "base": "https://thriftynorthwestmom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/"],
        "selector": "main",
    },
    {
        "name": "missfrugalmommy",
        "base": "https://www.missfrugalmommy.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/"],
        "selector": "main",
    },
    {
        "name": "moneysavingmom",
        "base": "https://moneysavingmom.com",
        "pages": ["/category/giveaways/",
                  "/category/sweepstakes/",
                  "/category/giveaways/page/2/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "luckyattitudes",
        "base": "https://luckyattitudes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "gleam_explore",
        "base": "https://gleam.io",
        "pages": ["/explore/giveaways", "/explore/giveaways?page=2",
                  "/explore/giveaways?page=3"],
        "selector": ".CompetitionList, main",
    },
    {
        "name": "woobox_campaigns",
        "base": "https://woobox.com",
        "pages": ["/explore/", "/explore/?page=2"],
        "selector": "main, .campaigns",
    },
    {
        "name": "pch",
        "base": "https://www.pch.com",
        "pages": ["/sweepstakes/", "/lotto/", "/instant-win/"],
        "selector": "main, .content",
    },
    {
        "name": "hgtv_sweepstakes",
        "base": "https://www.hgtv.com",
        "pages": ["/sweepstakes", "/sweepstakes/all-sweepstakes"],
        "selector": "main, .content",
    },
    {
        "name": "foodnetwork_sweeps",
        "base": "https://www.foodnetwork.com",
        "pages": ["/sweepstakes", "/sweepstakes/all-sweepstakes"],
        "selector": "main",
    },
    {
        "name": "diynetwork_sweeps",
        "base": "https://www.diynetwork.com",
        "pages": ["/sweepstakes"],
        "selector": "main",
    },
    {
        "name": "hallmark_contests",
        "base": "https://www.hallmark.com",
        "pages": ["/contests/", "/sweepstakes/"],
        "selector": "main",
    },
    {
        "name": "bradsdeals",
        "base": "https://www.bradsdeals.com",
        "pages": ["/freebies", "/freebies/sweepstakes",
                  "/freebies?page=2"],
        "selector": "main, .deals-list",
    },
    {
        "name": "dealnews_freebies",
        "base": "https://dealnews.com",
        "pages": ["/c196/Freebies/", "/c196/Freebies/?page=2"],
        "selector": "main, .deal-list",
    },
    {
        "name": "commonkindness",
        "base": "https://www.commonkindness.com",
        "pages": ["/sweepstakes/", "/contests/"],
        "selector": "main",
    },
    {
        "name": "contestpipe",
        "base": "https://www.contestpipe.com",
        "pages": ["/", "/sweepstakes/", "/page/2/"],
        "selector": "main",
    },
    {
        "name": "sweepstakes_online",
        "base": "https://www.sweepstakes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/"],
        "selector": "main",
    },
]


# RSS/Atom feed sources — title + URL extracted from feed XML
_RSS_SOURCES: list[dict[str, str]] = [
    {"name": "sweetiessweeps",     "feed": "https://sweetiessweeps.com/feed/"},
    {"name": "giveawayfrenzy",     "feed": "https://giveawayfrenzy.com/feed/"},
    {"name": "iheartgiveaways",    "feed": "https://iheartgiveaways.net/feed/"},
    {"name": "thesweepstakesguy",  "feed": "https://www.thesweepstakesguy.com/feed/"},
    {"name": "luckyattitudes_rss", "feed": "https://luckyattitudes.com/feed/"},
    {"name": "thefrugalfreebies_rss", "feed": "https://thefrugalfreebies.com/feed/"},
    {"name": "hip2save_rss",       "feed": "https://hip2save.com/feed/"},
    {"name": "freebies4mom_rss",   "feed": "https://freebies4mom.com/feed/"},
    {"name": "simplyfreestuff_rss","feed": "https://www.simplyfreestuff.com/feed/"},
    {"name": "pennypinchinmom_rss","feed": "https://www.pennypinchinmom.com/feed/"},
    {"name": "thriftynorthwestmom_rss", "feed": "https://thriftynorthwestmom.com/feed/"},
    {"name": "missfrugalmommy_rss","feed": "https://www.missfrugalmommy.com/feed/"},
    {"name": "moneysavingmom_rss", "feed": "https://moneysavingmom.com/feed/"},
    {"name": "contestblogger_rss", "feed": "https://www.contestblogger.com/feed/"},
    {"name": "winbigpicture_rss",  "feed": "https://winbigpicture.com/feed/"},
    {"name": "sweepstakesfanatics_rss", "feed": "https://sweepstakesfanatics.com/feed/"},
    {"name": "contestqueen_rss",   "feed": "https://contestqueen.com/feed/"},
    {"name": "giveawaymonkey_rss", "feed": "https://giveawaymonkey.com/feed/"},
    {"name": "winningbecause_rss", "feed": "https://www.winningbecause.com/feed/"},
    {"name": "sweepstakescrazies_rss", "feed": "https://sweepstakescrazies.com/feed/"},
    {"name": "contestchest_rss",   "feed": "https://www.contestchest.com/feed/"},
    {"name": "contestgirl_rss",    "feed": "https://www.contestgirl.com/feed/"},
]

# Reddit subreddits — fetched via JSON API (no scraping, reliable)
_REDDIT_SUBS: list[str] = [
    "sweepstakes",
    "giveaways",
    "freebies",
    "contest",
    "instantwin",
    "WinStuff",
    "sweepstakesadvice",
    "free",
]


# ══════════════════════════════════════════════════════════════════════════════
# Generic scraper engine
# ══════════════════════════════════════════════════════════════════════════════

def _get(url: str, session: requests.Session) -> BeautifulSoup | None:
    try:
        resp = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _should_skip(url: str) -> bool:
    host = urlparse(url).netloc.lower().lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in _SKIP_DOMAINS)


def _extract_external_links(
    soup: BeautifulSoup,
    page_url: str,
    base_host: str,
    seen: set[str],
    source: str,
    min_title_len: int = 6,
    container_selector: str | None = None,
) -> list[dict[str, Any]]:
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
    min_title_len: int = 6,
) -> list[dict[str, Any]]:
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

    logger.info("%s: %d sweepstakes", source, len(results))
    return results


def _scrape_rss(feed_url: str, source: str) -> list[dict[str, Any]]:
    session = requests.Session()
    results: list[dict[str, Any]] = []
    try:
        resp = session.get(feed_url, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("RSS fetch failed %s: %s", feed_url, exc)
        return results

    soup = BeautifulSoup(resp.text, "xml")

    for item in soup.find_all("item"):
        link_tag  = item.find("link")
        title_tag = item.find("title")
        if not (link_tag and title_tag):
            continue
        url   = (link_tag.string or link_tag.get_text()).strip()
        title = title_tag.get_text(strip=True)
        if url.startswith("http") and not _should_skip(url):
            results.append({"url": url, "title": title, "source": source})

    for entry in soup.find_all("entry"):
        link_tag  = entry.find("link")
        title_tag = entry.find("title")
        if not (link_tag and title_tag):
            continue
        url   = link_tag.get("href", "").strip()
        title = title_tag.get_text(strip=True)
        if url.startswith("http") and not _should_skip(url):
            results.append({"url": url, "title": title, "source": source})

    logger.info("%s (RSS): %d sweepstakes", source, len(results))
    return results


def _scrape_reddit_subs(subreddits: list[str]) -> list[dict[str, Any]]:
    """Fetch new + hot posts from each subreddit via Reddit's JSON API."""
    session = requests.Session()
    results: list[dict[str, Any]] = []
    seen:    set[str] = set()
    headers = {**_HEADERS, "Accept": "application/json"}

    for sub in subreddits:
        for sort in ("new", "hot"):
            api_url = f"https://www.reddit.com/r/{sub}/{sort}.json?limit=100"
            try:
                resp = session.get(api_url, headers=headers, timeout=_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("Reddit r/%s/%s failed: %s", sub, sort, exc)
                time.sleep(_DELAY)
                continue

            posts = data.get("data", {}).get("children", [])
            for post in posts:
                pd  = post.get("data", {})
                url = pd.get("url", "")
                if not url or url in seen or pd.get("is_self"):
                    continue
                if _should_skip(url):
                    continue
                title = pd.get("title", "")
                if len(title) < 5:
                    continue
                seen.add(url)
                results.append({
                    "url":    url,
                    "title":  title,
                    "source": f"reddit_r_{sub}",
                })

            time.sleep(_DELAY)

    logger.info("reddit: %d sweepstakes across %d subs", len(results), len(subreddits))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Master discovery function
# ══════════════════════════════════════════════════════════════════════════════

def discover_all() -> list[dict[str, Any]]:
    """
    Run all scrapers, deduplicate by URL, and return the combined list.

    Returns
    -------
    list[dict]  — each dict has keys ``url``, ``title``, ``source``
    """
    combined:  list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def _add(entries: list[dict[str, Any]]) -> None:
        for e in entries:
            url = e.get("url", "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined.append(e)

    # ── Page sources ─────────────────────────────────────────────────────────
    for cfg in _PAGE_SOURCES:
        try:
            _add(_scrape_pages(
                cfg["base"],
                cfg["pages"],
                cfg["name"],
                container_selector=cfg.get("selector"),
            ))
        except Exception as exc:
            logger.error("Page scraper '%s' failed: %s", cfg["name"], exc)

    # ── RSS sources ──────────────────────────────────────────────────────────
    for cfg in _RSS_SOURCES:
        try:
            _add(_scrape_rss(cfg["feed"], cfg["name"]))
        except Exception as exc:
            logger.error("RSS scraper '%s' failed: %s", cfg["name"], exc)

    # ── Reddit ───────────────────────────────────────────────────────────────
    try:
        _add(_scrape_reddit_subs(_REDDIT_SUBS))
    except Exception as exc:
        logger.error("Reddit scraper failed: %s", exc)

    logger.info("discover_all: %d unique sweepstakes total", len(combined))
    return combined


def list_sources() -> list[str]:
    """Return the names of all registered sources."""
    names  = [cfg["name"] for cfg in _PAGE_SOURCES]
    names += [cfg["name"] for cfg in _RSS_SOURCES]
    names += [f"reddit_r_{s}" for s in _REDDIT_SUBS]
    return names
