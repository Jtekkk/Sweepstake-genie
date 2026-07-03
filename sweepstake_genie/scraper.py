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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Number of sources fetched concurrently. Each source hits a distinct host, so
# parallelising across sources does not hammer any single server (per-host
# politeness is preserved by the intra-source _DELAY between page fetches).
_MAX_WORKERS = 12

# ── URL normalisation ─────────────────────────────────────────────────────────

_TRACKING_PARAMS = frozenset([
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "msclkid", "ref", "source",
    "affiliate", "partner", "cid", "eid", "sid", "tid", "via", "mc_cid",
    "mc_eid", "_ga", "zanpid", "origin", "igshid",
])


def _normalize_url(url: str) -> str:
    """Strip tracking params, fragments, and normalize scheme/host for dedup."""
    try:
        p = urlparse(url)
        params = parse_qs(p.query, keep_blank_values=False)
        filtered = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        clean_query = urlencode(filtered, doseq=True)
        _netloc = p.netloc.lower()
        normalized = p._replace(
            scheme=p.scheme.lower(),
            netloc=_netloc[4:] if _netloc.startswith("www.") else _netloc,
            path=p.path.rstrip("/") or "/",
            query=clean_query,
            fragment="",
        )
        return normalized.geturl()
    except Exception:
        return url


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

# At least one of these must appear in the title OR URL path for a link to be
# kept. Prevents deal articles, product pages, and sponsored content from
# slipping through aggregators that mix sweepstakes with other content.
_SWEEP_KEYWORDS = frozenset([
    "sweepstakes", "sweepstake", "sweeps",
    "giveaway", "giveaways",
    "contest", "contests",
    "prize", "prizes",
    "drawing", "raffle",
    "enter to win", "enter now",
    "win a ", "win an ", "you could win",
    "instant win", "instant-win",
])

# Title patterns that indicate advice/news articles rather than sweepstake entries.
# These are common in RSS feeds from sweepstaking BLOGS that mix entry listings
# with tips, guides, and news posts.
_EXCLUDE_TITLE_PATTERNS = frozenset([
    # Instructional / how-to
    "quick tip", "top tip", "tips to ", "tips for ",
    "how to ", "how do ", "how i ", "how you ",
    "why you", "why i ", "why we ", "why track",
    "guide to", "guide for", "getting started",
    "what is ", "what are ", "best practice",
    # Events / webinars / podcasts
    "masterclass", "webinar", " meeting", "club meeting",
    "virtual contest", "podcast", "listen & learn", "listen and learn",
    # Site or tool news
    " upgrade", "just launched", "new feature", "website upgrade",
    "enewsletter", "newsletter", "subscribe to ",
    # Explicit non-entry signals
    "leave a review", "write a review",
    "follow us on", "join us on",
    "sweepstakes posts",   # category/tag page link
    "fake contest", "scam alert",
    "i win every",         # personal story/advice article
    # Blog/article phrasings (seen polluting RSS feeds from sweeps blogs)
    "comments",            # "0 Comments" / "3 Comments" comment-link titles
    "convention", "paying taxes", "clean your",
    "finding sweepstakes", "are changing", "with me",
    "roboform", "in the wild", "voting contest",
    "how sweepstakes", "still my", " scams",
    "sweepstaking", "contesting",   # category names, not entries
    "what really happens", "spring into", "master winning",
    "with me for free",
])


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
                  "/sweepstakes/page/3/", "/sweepstakes/page/4/",
                  "/sweepstakes/page/5/", "/instant-win-games/"],
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
                  "/sweepstakes/page/3/", "/sweepstakes/page/4/",
                  "/sweepstakes/page/5/", "/sweepstakes/page/6/"],
        "selector": ".entry-content, main",
    },
    {
        "name": "contestqueen",
        "base": "https://contestqueen.com",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/page/2/", "/page/3/", "/page/4/", "/page/5/", "/page/6/"],
        "selector": "main, .site-content",
    },
    {
        "name": "winbigpicture",
        "base": "https://winbigpicture.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
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
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thecontestguy",
        "base": "https://www.thecontestguy.com",
        "pages": ["/", "/sweepstakes/", "/contests/",
                  "/page/2/", "/page/3/", "/page/4/", "/page/5/", "/page/6/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "contestcorner",
        "base": "https://www.contestcorner.ca",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/page/2/", "/page/3/", "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweepstakeshunter",
        "base": "https://www.sweepstakeshunter.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesden",
        "base": "https://www.sweepstakesden.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweepstakescastle",
        "base": "https://www.sweepstakescastle.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "sweepstakescrazies",
        "base": "https://sweepstakescrazies.com",
        "pages": ["/", "/page/2/", "/page/3/", "/page/4/",
                  "/page/5/", "/page/6/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "winafreebee",
        "base": "https://www.winafreebee.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "1clickwin",
        "base": "https://www.1clickwin.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "supersweepstakes",
        "base": "https://www.supersweepstakes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesmania",
        "base": "https://sweepstakesmania.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "sweepslist",
        "base": "https://www.sweepslist.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "giveawaymonkey",
        "base": "https://giveawaymonkey.com",
        "pages": ["/", "/giveaways/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "winningbecause",
        "base": "https://www.winningbecause.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "contestblogger",
        "base": "https://www.contestblogger.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
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
                  "/category/sweepstakes/page/3/",
                  "/category/sweepstakes/page/4/",
                  "/category/sweepstakes/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "freebies4mom",
        "base": "https://freebies4mom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "simplyfreestuff",
        "base": "https://www.simplyfreestuff.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "thefrugalfreebies",
        "base": "https://thefrugalfreebies.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/",
                  "/page/2/", "/page/3/", "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "pennypinchinmom",
        "base": "https://www.pennypinchinmom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main",
    },
    {
        "name": "thriftynorthwestmom",
        "base": "https://thriftynorthwestmom.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main",
    },
    {
        "name": "missfrugalmommy",
        "base": "https://www.missfrugalmommy.com",
        "pages": ["/category/sweepstakes/",
                  "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main",
    },
    {
        "name": "moneysavingmom",
        "base": "https://moneysavingmom.com",
        "pages": ["/category/giveaways/",
                  "/category/sweepstakes/",
                  "/category/giveaways/page/2/",
                  "/category/giveaways/page/3/",
                  "/category/sweepstakes/page/2/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "luckyattitudes",
        "base": "https://luckyattitudes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "gleam_explore",
        "base": "https://gleam.io",
        "pages": ["/explore/giveaways", "/explore/giveaways?page=2",
                  "/explore/giveaways?page=3", "/explore/giveaways?page=4",
                  "/explore/giveaways?page=5"],
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
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "sweepstakes_online",
        "base": "https://www.sweepstakes.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },

    # ── Additional aggregators & blogs ───────────────────────────────────────
    {
        "name": "prizegrab",
        "base": "https://www.prizegrab.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "contestalley",
        "base": "https://www.contestalley.com",
        "pages": ["/", "/sweepstakes/", "/contests/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "nationalfamilyfun",
        "base": "https://www.nationalfamilyfun.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thewinningmom",
        "base": "https://thewinningmom.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesinsider",
        "base": "https://www.sweepstakesinsider.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thegiveawaygeek",
        "base": "https://thegiveawaygeek.com",
        "pages": ["/", "/giveaways/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "budgetsavvydiva",
        "base": "https://www.budgetsavvydiva.com",
        "pages": ["/category/sweepstakes/", "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thriftyjinxy",
        "base": "https://thriftyjinxy.com",
        "pages": ["/category/sweepstakes/", "/category/giveaways/",
                  "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "singlemomsincome",
        "base": "https://singlemomsincome.com",
        "pages": ["/category/giveaways/", "/category/sweepstakes/",
                  "/category/giveaways/page/2/",
                  "/category/giveaways/page/3/",
                  "/category/giveaways/page/4/"],
        "selector": "main",
    },
    {
        "name": "dailycheapskate",
        "base": "https://www.dailycheapskate.com",
        "pages": ["/category/sweepstakes/", "/category/giveaways/",
                  "/category/contests/", "/category/sweepstakes/page/2/",
                  "/category/giveaways/page/2/",
                  "/category/sweepstakes/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "freebieshark",
        "base": "https://freebieshark.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/", "/page/2/",
                  "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "bestfreestuff",
        "base": "https://bestfreestuff.com",
        "pages": ["/", "/sweepstakes/", "/contests/", "/page/2/",
                  "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweetfreestuff",
        "base": "https://www.sweetfreestuff.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "totallyfreestuff",
        "base": "https://www.totallyfreestuff.com",
        "pages": ["/", "/sweepstakes/", "/contests/", "/page/2/",
                  "/page/3/", "/page/4/"],
        "selector": "main, #content",
    },
    {
        "name": "freebiemom",
        "base": "https://www.freebiemom.com",
        "pages": ["/sweepstakes/", "/giveaways/",
                  "/sweepstakes/page/2/",
                  "/giveaways/page/2/",
                  "/sweepstakes/page/3/"],
        "selector": "main",
    },
    {
        "name": "giveawaybase",
        "base": "https://giveawaybase.com",
        "pages": ["/", "/giveaways/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "contestworld",
        "base": "https://www.contestworld.co.uk",
        "pages": ["/", "/competitions/", "/sweepstakes/", "/page/2/",
                  "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "prizespy",
        "base": "https://www.prizespy.co.uk",
        "pages": ["/", "/competitions/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "contestcanada",
        "base": "https://www.contestcanada.net",
        "pages": ["/", "/contests/", "/sweepstakes/", "/page/2/",
                  "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "bountysweeps",
        "base": "https://www.bountysweeps.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "sweepstakeswithkathy",
        "base": "https://www.sweepstakeswithkathy.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/",
                  "/page/4/", "/page/5/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "mysweeps",
        "base": "https://www.mysweeps.net",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main",
    },
    {
        "name": "viralsweep_explore",
        "base": "https://viralsweep.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main, .sweepstakes-list",
    },
    {
        "name": "travel_channel_sweeps",
        "base": "https://www.travelchannel.com",
        "pages": ["/sweepstakes", "/sweepstakes/all-sweepstakes"],
        "selector": "main",
    },
    {
        "name": "magnolia_sweeps",
        "base": "https://www.magnolia.com",
        "pages": ["/sweepstakes/", "/contests/"],
        "selector": "main",
    },
    {
        "name": "countryliving_sweeps",
        "base": "https://www.countryliving.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "goodhousekeeping_sweeps",
        "base": "https://www.goodhousekeeping.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "womansday_sweeps",
        "base": "https://www.womansday.com",
        "pages": ["/sweepstakes/", "/contests/"],
        "selector": "main",
    },
    {
        "name": "redbook_sweeps",
        "base": "https://www.redbookmag.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "delish_sweeps",
        "base": "https://www.delish.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "bhg_sweeps",
        "base": "https://www.bhg.com",
        "pages": ["/sweepstakes/", "/giveaways/",
                  "/giveaways/sweepstakes/"],
        "selector": "main",
    },
    {
        "name": "thepioneerwoman_sweeps",
        "base": "https://thepioneerwoman.com",
        "pages": ["/sweepstakes/", "/contests/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "taste_of_home_sweeps",
        "base": "https://www.tasteofhome.com",
        "pages": ["/contests-sweepstakes/",
                  "/collection/sweepstakes/"],
        "selector": "main",
    },

    # ── New sources ──────────────────────────────────────────────────────────
    {
        "name": "iheartgiveaways_pages",
        "base": "https://iheartgiveaways.net",
        "pages": ["/", "/giveaways/", "/sweepstakes/",
                  "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "giveawayfrenzy_pages",
        "base": "https://giveawayfrenzy.com",
        "pages": ["/", "/giveaways/", "/sweepstakes/",
                  "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "sweetiessweeps_pages",
        "base": "https://sweetiessweeps.com",
        "pages": ["/", "/sweepstakes/", "/contests/", "/instant-win/",
                  "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "thesweepstakesguy_pages",
        "base": "https://www.thesweepstakesguy.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/",
                  "/page/2/", "/page/3/", "/page/4/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "real_simple_sweeps",
        "base": "https://www.realsimple.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "rachael_ray_sweeps",
        "base": "https://www.rachaelraymag.com",
        "pages": ["/sweepstakes/", "/contests/"],
        "selector": "main",
    },
    {
        "name": "southern_living_sweeps",
        "base": "https://www.southernliving.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "people_sweeps",
        "base": "https://people.com",
        "pages": ["/sweepstakes/", "/contests/"],
        "selector": "main",
    },
    {
        "name": "instyle_sweeps",
        "base": "https://www.instyle.com",
        "pages": ["/sweepstakes/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "parade_sweeps",
        "base": "https://parade.com",
        "pages": ["/sweepstakes/", "/contests/", "/giveaways/"],
        "selector": "main",
    },
    {
        "name": "contestguru",
        "base": "https://www.contestguru.com",
        "pages": ["/", "/contests/", "/sweepstakes/",
                  "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "winnerscircle",
        "base": "https://winnerscircle.com",
        "pages": ["/", "/sweepstakes/", "/giveaways/",
                  "/page/2/", "/page/3/"],
        "selector": "main",
    },
    {
        "name": "sweepstakesdailynews",
        "base": "https://www.sweepstakesdailynews.com",
        "pages": ["/", "/sweepstakes/", "/page/2/", "/page/3/"],
        "selector": "main, .entry-content",
    },
    {
        "name": "afullcup_instant",
        "base": "https://www.afullcup.com",
        "pages": ["/forums/instant-win-games/",
                  "/forums/instant-win-games/?page=2",
                  "/forums/instant-win-games/?page=3"],
        "selector": ".forum-content, main, #content",
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
    {"name": "contestchest_rss",       "feed": "https://www.contestchest.com/feed/"},
    {"name": "contestgirl_rss",        "feed": "https://www.contestgirl.com/feed/"},
    {"name": "thegiveawaygeek_rss",    "feed": "https://thegiveawaygeek.com/feed/"},
    {"name": "contestalley_rss",       "feed": "https://www.contestalley.com/feed/"},
    {"name": "nationalfamilyfun_rss",  "feed": "https://www.nationalfamilyfun.com/feed/"},
    {"name": "thewinningmom_rss",      "feed": "https://thewinningmom.com/feed/"},
    {"name": "sweepstakesinsider_rss", "feed": "https://www.sweepstakesinsider.com/feed/"},
    {"name": "singlemomsincome_rss",   "feed": "https://singlemomsincome.com/feed/"},
    {"name": "dailycheapskate_rss",    "feed": "https://www.dailycheapskate.com/feed/"},
    {"name": "freebieshark_rss",       "feed": "https://freebieshark.com/feed/"},
    {"name": "budgetsavvydiva_rss",    "feed": "https://www.budgetsavvydiva.com/feed/"},
    {"name": "thriftyjinxy_rss",       "feed": "https://thriftyjinxy.com/feed/"},
    {"name": "sweepstakeswithkathy_rss","feed": "https://www.sweepstakeswithkathy.com/feed/"},
    {"name": "giveawaybase_rss",       "feed": "https://giveawaybase.com/feed/"},
    # Feeds for sites already in _PAGE_SOURCES — doubles discovery coverage
    {"name": "sweepstakeslovers_rss",  "feed": "https://www.sweepstakeslovers.com/feed/"},
    {"name": "thecontestguy_rss",      "feed": "https://www.thecontestguy.com/feed/"},
    {"name": "sweepstakeshunter_rss",  "feed": "https://www.sweepstakeshunter.com/feed/"},
    {"name": "sweepstakesden_rss",     "feed": "https://www.sweepstakesden.com/feed/"},
    {"name": "prizegrab_rss",          "feed": "https://www.prizegrab.com/feed/"},
    {"name": "winafreebee_rss",        "feed": "https://www.winafreebee.com/feed/"},
    {"name": "bestfreestuff_rss",      "feed": "https://bestfreestuff.com/feed/"},
    {"name": "bountysweeps_rss",       "feed": "https://www.bountysweeps.com/feed/"},
    {"name": "1clickwin_rss",          "feed": "https://www.1clickwin.com/feed/"},
    {"name": "freebiemom_rss",         "feed": "https://www.freebiemom.com/feed/"},
    {"name": "sweetfreestuff_rss",     "feed": "https://www.sweetfreestuff.com/feed/"},
    {"name": "totallyfreestuff_rss",   "feed": "https://www.totallyfreestuff.com/feed/"},
    {"name": "contestcanada_rss",      "feed": "https://www.contestcanada.net/feed/"},
    {"name": "prizespy_rss",           "feed": "https://www.prizespy.co.uk/feed/"},
    {"name": "mysweeps_rss",           "feed": "https://www.mysweeps.net/feed/"},
    {"name": "sweepstakescastle_rss",  "feed": "https://www.sweepstakescastle.com/feed/"},
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
    "Prizes",
    "GiveawayExchange",
    "FreeStuff",
    "sweepstakesforum",
    "ContestTime",
    "winit",
    "sweeping",
    "sweeper",
    "Winnings",
    "FreeGiveaways",
    "GameSweepstakes",
    "sweepstakeshunters",
]


# ══════════════════════════════════════════════════════════════════════════════
# Generic scraper engine
# ══════════════════════════════════════════════════════════════════════════════

def _get(url: str, session: requests.Session, *, retries: int = 2) -> BeautifulSoup | None:
    """Fetch *url* and parse to soup, retrying transient failures with backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as exc:
            last_exc = exc
            # Don't retry on 4xx client errors — they won't succeed on retry
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                break
            if attempt < retries:
                time.sleep(_DELAY * (attempt + 1))  # linear backoff
    logger.warning("Failed to fetch %s: %s", url, last_exc)
    return None


def _should_skip(url: str) -> bool:
    host = urlparse(url).netloc.lower().lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in _SKIP_DOMAINS)


def _looks_like_sweepstake(title: str, url: str) -> bool:
    """Return True if the title or URL path contains a sweepstakes keyword."""
    title_stripped = title.strip()
    # Very short titles are navigation items / category labels, not sweepstakes
    if len(title_stripped) < 10:
        return False
    title_lower = title_stripped.lower()
    # Filter out advice/news/event articles from sweepstaking blogs
    if any(pat in title_lower for pat in _EXCLUDE_TITLE_PATTERNS):
        return False
    combined = (title_lower + " " + urlparse(url).path).lower()
    return any(kw in combined for kw in _SWEEP_KEYWORDS)


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
        normalized = _normalize_url(full_url)
        if normalized in seen:
            continue
        title = anchor.get_text(strip=True)
        if len(title) < min_title_len:
            continue
        if not _looks_like_sweepstake(title, full_url):
            continue
        seen.add(normalized)
        results.append({"url": normalized, "title": title, "source": source})

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
        time.sleep(_DELAY)  # rate-limit regardless of success/failure
        if soup is None:
            continue
        found = _extract_external_links(
            soup, page_url, base_host, seen, source,
            min_title_len=min_title_len,
            container_selector=container_selector,
        )
        results.extend(found)

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
        if url.startswith("http") and not _should_skip(url) and _looks_like_sweepstake(title, url):
            results.append({"url": _normalize_url(url), "title": title, "source": source})

    for entry in soup.find_all("entry"):
        link_tag  = entry.find("link")
        title_tag = entry.find("title")
        if not (link_tag and title_tag):
            continue
        url   = link_tag.get("href", "").strip()
        title = title_tag.get_text(strip=True)
        if url.startswith("http") and not _should_skip(url) and _looks_like_sweepstake(title, url):
            results.append({"url": _normalize_url(url), "title": title, "source": source})

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
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning("Reddit rate-limited; sleeping %ds", wait)
                    time.sleep(wait)
                    continue
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
                if not url or pd.get("is_self"):
                    continue
                if _should_skip(url):
                    continue
                normalized = _normalize_url(url)
                if normalized in seen:
                    continue
                title = pd.get("title", "")
                if len(title) < 5:
                    continue
                seen.add(normalized)
                results.append({
                    "url":    normalized,
                    "title":  title,
                    "source": f"reddit_r_{sub}",
                })

            time.sleep(_DELAY)

    logger.info("reddit: %d sweepstakes across %d subs", len(results), len(subreddits))
    return results


# ══════════════════════════════════════════════════════════════════════════════
# FreebieMom sweepstakes database (custom — it's a paginated card grid where each
# card links out to the real entry via an "Enter Here"/"Enter Daily" button)
# ══════════════════════════════════════════════════════════════════════════════

# Anchor text that marks a "go to the actual entry" button on a FreebieMom card.
_FMSDB_ENTER_TEXTS = (
    "enter here", "enter daily", "enter now", "enter to win", "official link",
)


def _fmsdb_card_title(anchor) -> str | None:
    """Walk up from an Enter button to find the sweepstakes card's heading."""
    node = anchor
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None:
            break
        heading = node.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        if heading:
            t = heading.get_text(strip=True)
            if t and len(t) >= 5:
                return t[:150]
    return None


def _scrape_freebiemom_db(max_pages: int = 12) -> list[dict[str, Any]]:
    """
    Scrape FreebieMom's sweepstakes database. Each card carries an
    "Enter Here"/"Enter Daily"/"Official link" anchor that points to the actual
    entry (an external sponsor page, or a freebiemom redirect that 302s to one).
    We record those per-card links — internal ones included, since navigating to
    a freebiemom redirect lands on the real entry.
    """
    session = requests.Session()
    base = "https://freebiemom.com/sweepstakes-database/"
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for pg in range(1, max_pages + 1):
        page_url = f"{base}?fmsdb_page={pg}"
        soup = _get(page_url, session)
        time.sleep(_DELAY)
        if soup is None:
            continue

        found_here = 0
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            text = anchor.get_text(strip=True).lower()
            cls = " ".join(anchor.get("class") or []).lower()
            is_enter = any(t in text for t in _FMSDB_ENTER_TEXTS) or (
                "enter" in cls and "search" not in cls
            )
            if not is_enter:
                continue

            full_url = urljoin(page_url, href)
            # Drop social/share links; keep sponsor + freebiemom redirect links.
            if _should_skip(full_url):
                continue
            normalized = _normalize_url(full_url)
            if normalized in seen:
                continue
            seen.add(normalized)

            title = _fmsdb_card_title(anchor) or urlparse(full_url).netloc
            results.append({
                "url": normalized,
                "title": title,
                "source": "freebiemom_db",
            })
            found_here += 1

        # Stop paginating once a page past the first yields nothing new.
        if found_here == 0 and pg > 1:
            break

    logger.info("freebiemom_db: %d sweepstakes across %d pages", len(results), pg)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Master discovery function
# ══════════════════════════════════════════════════════════════════════════════

def discover_all(
    *,
    max_workers: int = _MAX_WORKERS,
    progress: Callable[[int, int, str], None] | None = None,
    on_source: Callable[[str, int], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Run all scrapers concurrently, deduplicate by URL, and return the combined list.

    Sources are fetched in parallel across a thread pool — each source targets a
    distinct host, so per-host rate-limiting (the intra-source delay) is preserved
    while overall wall-clock time drops dramatically versus serial scraping.

    Parameters
    ----------
    max_workers:
        Maximum number of sources fetched concurrently.
    progress:
        Optional callback invoked as ``progress(done, total, source_name)`` after
        each source completes — useful for driving a progress bar.

    Returns
    -------
    list[dict]  — each dict has keys ``url``, ``title``, ``source``
    """
    combined:  list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def _add(entries: list[dict[str, Any]]) -> None:
        for e in entries:
            url = e.get("url", "").strip()
            normalized = _normalize_url(url)
            if url and normalized not in seen_urls:
                seen_urls.add(normalized)
                combined.append(e)

    # Build a flat list of (name, callable) tasks. Each callable returns a list
    # of result dicts and isolates its own failures.
    tasks: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = []

    for cfg in _PAGE_SOURCES:
        tasks.append((
            cfg["name"],
            lambda c=cfg: _scrape_pages(
                c["base"], c["pages"], c["name"],
                container_selector=c.get("selector"),
            ),
        ))
    for cfg in _RSS_SOURCES:
        tasks.append((cfg["name"], lambda c=cfg: _scrape_rss(c["feed"], c["name"])))
    tasks.append(("reddit", lambda: _scrape_reddit_subs(_REDDIT_SUBS)))
    tasks.append(("freebiemom_db", lambda: _scrape_freebiemom_db()))

    total = len(tasks)
    done = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in tasks}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            count = 0
            try:
                res = future.result()
                count = len(res)
                _add(res)
            except Exception as exc:
                logger.error("Scraper '%s' failed: %s", name, exc)
            if on_source is not None:
                try:
                    on_source(name, count)
                except Exception:
                    pass  # never let a callback break discovery
            done += 1
            if progress is not None:
                try:
                    progress(done, total, name)
                except Exception:
                    pass  # never let a progress callback break discovery

    logger.info("discover_all: %d unique sweepstakes total", len(combined))
    return combined


def list_sources() -> list[str]:
    """Return the names of all registered sources."""
    names  = [cfg["name"] for cfg in _PAGE_SOURCES]
    names += [cfg["name"] for cfg in _RSS_SOURCES]
    names += [f"reddit_r_{s}" for s in _REDDIT_SUBS]
    names += ["freebiemom_db"]
    return names
