#!/usr/bin/env python3
"""
Comprehensive automated test suite for Sweepstake Genie.
Runs all tests and reports PASS/FAIL with a summary table at the end.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ── Ensure project root is on path ───────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

results: list[dict[str, Any]] = []  # {"name": str, "status": "PASS"|"FAIL", "error": str}


def record(name: str, status: str, error: str = "") -> None:
    tag = f"{GREEN}PASS{RESET}" if status == "PASS" else f"{RED}FAIL{RESET}"
    print(f"  {tag}  {name}")
    if error:
        for line in error.strip().splitlines():
            print(f"       {YELLOW}{line}{RESET}")
    results.append({"name": name, "status": status, "error": error})


def run_test(name: str, fn):
    """Run a synchronous or asynchronous test function and record result."""
    try:
        if asyncio.iscoroutinefunction(fn):
            asyncio.run(fn())
        else:
            fn()
        record(name, "PASS")
    except Exception as exc:
        tb = traceback.format_exc()
        record(name, "FAIL", f"{type(exc).__name__}: {exc}\n{tb}")


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — Import test
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 1. Import Tests ==={RESET}")


def test_import_config():
    from sweepstake_genie.config import Config  # noqa: F401


def test_import_database():
    from sweepstake_genie.database import Database  # noqa: F401


def test_import_browser():
    from sweepstake_genie.browser import BrowserManager  # noqa: F401


def test_import_captcha_solver():
    from sweepstake_genie.captcha_solver import CaptchaSolver  # noqa: F401


def test_import_form_filler():
    from sweepstake_genie.form_filler import enter_sweepstake  # noqa: F401


def test_import_scraper():
    from sweepstake_genie.scraper import discover_all  # noqa: F401


def test_import_runner():
    from sweepstake_genie.runner import run_discover, run_enter, run_status  # noqa: F401


for fn in [
    test_import_config,
    test_import_database,
    test_import_browser,
    test_import_captcha_solver,
    test_import_form_filler,
    test_import_scraper,
    test_import_runner,
]:
    run_test(f"import: {fn.__name__.replace('test_import_', '')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — Config tests
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 2. Config Tests ==={RESET}")


def test_config_loads():
    from sweepstake_genie.config import Config
    cfg = Config(str(PROJECT_ROOT / "profile.yaml"))
    assert cfg.profile, "profile dict should not be empty"
    assert cfg.settings, "settings dict should not be empty"


def test_config_profile_fields():
    from sweepstake_genie.config import Config
    cfg = Config(str(PROJECT_ROOT / "profile.yaml"))
    required = ["first_name", "last_name", "email", "phone"]
    missing = [f for f in required if f not in cfg.profile]
    assert not missing, f"Missing profile fields: {missing}"


def test_config_settings_fields():
    from sweepstake_genie.config import Config
    cfg = Config(str(PROJECT_ROOT / "profile.yaml"))
    required = ["headless", "delay_between_entries"]
    missing = [f for f in required if f not in cfg.settings]
    assert not missing, f"Missing settings fields: {missing}"


def test_config_missing_file():
    from sweepstake_genie.config import Config
    try:
        Config("/nonexistent/path/profile.yaml")
        raise AssertionError("Should have raised FileNotFoundError")
    except FileNotFoundError:
        pass  # expected


def test_captcha_solver_from_config():
    from sweepstake_genie.config import Config
    from sweepstake_genie.captcha_solver import CaptchaSolver
    cfg = Config(str(PROJECT_ROOT / "profile.yaml"))
    solver = CaptchaSolver(service=cfg.captcha_service, api_key=cfg.captcha_api_key)
    # Should not crash; service=none means disabled
    assert not solver.enabled, "solver should be disabled when service='none'"


def test_config_properties():
    from sweepstake_genie.config import Config
    cfg = Config(str(PROJECT_ROOT / "profile.yaml"))
    # Test property types
    assert isinstance(cfg.headless, bool)
    assert isinstance(cfg.delay_between_entries, (int, float))
    assert isinstance(cfg.max_entries_per_run, int)
    assert isinstance(cfg.concurrency, int)
    assert isinstance(cfg.skip_captcha, bool)
    assert isinstance(cfg.log_file, str)
    assert isinstance(cfg.database, str)
    assert isinstance(cfg.captcha_service, str)
    assert isinstance(cfg.captcha_api_key, str)


for fn in [
    test_config_loads,
    test_config_profile_fields,
    test_config_settings_fields,
    test_config_missing_file,
    test_captcha_solver_from_config,
    test_config_properties,
]:
    run_test(f"config: {fn.__name__.replace('test_config_', '').replace('test_captcha_', 'captcha_')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Database tests
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 3. Database Tests ==={RESET}")


def _make_db() -> "Database":
    """Create a fresh in-memory (temp file) database for each test."""
    from sweepstake_genie.database import Database
    tmp = tempfile.mktemp(suffix=".db")
    return Database(tmp)


def test_db_add_sweepstake():
    db = _make_db()
    added = db.add_sweepstake("https://example.com/sweep", "Test Sweep", "test")
    assert added is True, "First insert should return True"


def test_db_duplicate_insert():
    db = _make_db()
    db.add_sweepstake("https://example.com/sweep", "Test Sweep", "test")
    added_again = db.add_sweepstake("https://example.com/sweep", "Test Sweep", "test")
    assert added_again is False, "Duplicate insert should return False"


def test_db_get_pending():
    db = _make_db()
    db.add_sweepstake("https://a.com", "A", "test")
    db.add_sweepstake("https://b.com", "B", "test")
    pending = db.get_pending()
    assert len(pending) == 2, f"Expected 2 pending, got {len(pending)}"
    assert all(r["status"] == "pending" for r in pending)


def test_db_mark_entered():
    db = _make_db()
    url = "https://example.com/sweep"
    db.add_sweepstake(url, "Test", "test")
    db.mark_entered(url)
    pending = db.get_pending()
    assert len(pending) == 0, "After mark_entered, should not be pending"
    stats = db.get_stats()
    assert stats["entered"] == 1


def test_db_mark_captcha():
    db = _make_db()
    url = "https://example.com/sweep"
    db.add_sweepstake(url, "Test", "test")
    db.mark_captcha(url)
    stats = db.get_stats()
    assert stats["captcha"] == 1


def test_db_mark_error():
    db = _make_db()
    url = "https://example.com/sweep"
    db.add_sweepstake(url, "Test", "test")
    db.mark_error(url, "timeout")
    stats = db.get_stats()
    assert stats["error"] == 1


def test_db_mark_skipped():
    db = _make_db()
    url = "https://example.com/sweep"
    db.add_sweepstake(url, "Test", "test")
    db.mark_skipped(url, "no form")
    stats = db.get_stats()
    assert stats["skipped"] == 1


def test_db_mark_allows_daily():
    db = _make_db()
    url = "https://example.com/sweep"
    db.add_sweepstake(url, "Test", "test")
    db.mark_entered(url)
    db.mark_allows_daily(url)
    # Backdate to yesterday to make it due for re-entry
    import sqlite3
    conn = sqlite3.connect(str(db.db_path))
    conn.execute(
        "UPDATE sweepstakes SET entered_at = datetime('now', '-1 day') WHERE url = ?",
        (url,)
    )
    conn.commit()
    conn.close()
    due = db.get_due_for_reentry()
    assert len(due) == 1, f"Expected 1 due for re-entry, got {len(due)}"


def test_db_get_stats():
    db = _make_db()
    db.add_sweepstake("https://a.com", "A", "t")
    db.add_sweepstake("https://b.com", "B", "t")
    db.add_sweepstake("https://c.com", "C", "t")
    db.mark_entered("https://a.com")
    db.mark_captcha("https://b.com")
    # c stays pending
    stats = db.get_stats()
    assert stats["total"] == 3
    assert stats["entered"] == 1
    assert stats["captcha"] == 1
    assert stats["pending"] == 1


def test_db_get_retryable_without_captcha():
    db = _make_db()
    db.add_sweepstake("https://err.com", "E", "t")
    db.add_sweepstake("https://cap.com", "C", "t")
    db.mark_error("https://err.com", "timeout")
    db.mark_captcha("https://cap.com")
    retryable = db.get_retryable(include_captcha=False)
    urls = [r["url"] for r in retryable]
    assert "https://err.com" in urls
    assert "https://cap.com" not in urls


def test_db_get_retryable_with_captcha():
    db = _make_db()
    db.add_sweepstake("https://err.com", "E", "t")
    db.add_sweepstake("https://cap.com", "C", "t")
    db.mark_error("https://err.com", "timeout")
    db.mark_captcha("https://cap.com")
    retryable = db.get_retryable(include_captcha=True)
    urls = [r["url"] for r in retryable]
    assert "https://err.com" in urls
    assert "https://cap.com" in urls


def test_db_get_due_for_reentry_today():
    """Entry done today should NOT be due for re-entry."""
    db = _make_db()
    url = "https://daily.com"
    db.add_sweepstake(url, "Daily", "t")
    db.mark_entered(url)
    db.mark_allows_daily(url)
    # entered_at defaults to today (CURRENT_TIMESTAMP set by mark_entered)
    due = db.get_due_for_reentry()
    assert len(due) == 0, f"Entry done today should not be due (got {len(due)})"


def test_db_get_due_for_reentry_yesterday():
    """Entry done yesterday SHOULD be due for re-entry."""
    import sqlite3
    db = _make_db()
    url = "https://daily.com"
    db.add_sweepstake(url, "Daily", "t")
    db.mark_entered(url)
    db.mark_allows_daily(url)
    # Backdate
    conn = sqlite3.connect(str(db.db_path))
    conn.execute(
        "UPDATE sweepstakes SET entered_at = datetime('now', '-1 day') WHERE url = ?",
        (url,)
    )
    conn.commit()
    conn.close()
    due = db.get_due_for_reentry()
    assert len(due) == 1, f"Entry from yesterday should be due (got {len(due)})"


def test_db_url_exists():
    db = _make_db()
    url = "https://example.com"
    assert not db.url_exists(url)
    db.add_sweepstake(url, "T", "t")
    assert db.url_exists(url)


def test_db_add_sweepstakes_bulk():
    db = _make_db()
    entries = [
        {"url": "https://a.com", "title": "A", "source": "t"},
        {"url": "https://b.com", "title": "B", "source": "t"},
        {"url": "https://c.com", "title": "C", "source": "t"},
    ]
    new_count = db.add_sweepstakes_bulk(entries)
    assert new_count == 3, f"Expected 3 new, got {new_count}"
    assert db.url_exists("https://a.com")
    assert db.url_exists("https://c.com")


def test_db_add_sweepstakes_bulk_dedup():
    db = _make_db()
    db.add_sweepstake("https://a.com", "A", "t")  # pre-existing
    entries = [
        {"url": "https://a.com", "title": "A", "source": "t"},  # dup
        {"url": "https://b.com", "title": "B", "source": "t"},  # new
    ]
    new_count = db.add_sweepstakes_bulk(entries)
    assert new_count == 1, f"Expected 1 new (one dup), got {new_count}"


def test_db_add_sweepstakes_bulk_empty():
    db = _make_db()
    assert db.add_sweepstakes_bulk([]) == 0
    # entries without a url are skipped
    assert db.add_sweepstakes_bulk([{"title": "no url"}]) == 0


for fn in [
    test_db_add_sweepstake,
    test_db_duplicate_insert,
    test_db_get_pending,
    test_db_mark_entered,
    test_db_mark_captcha,
    test_db_mark_error,
    test_db_mark_skipped,
    test_db_mark_allows_daily,
    test_db_get_stats,
    test_db_get_retryable_without_captcha,
    test_db_get_retryable_with_captcha,
    test_db_get_due_for_reentry_today,
    test_db_get_due_for_reentry_yesterday,
    test_db_url_exists,
    test_db_add_sweepstakes_bulk,
    test_db_add_sweepstakes_bulk_dedup,
    test_db_add_sweepstakes_bulk_empty,
]:
    run_test(f"db: {fn.__name__.replace('test_db_', '')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — Scraper tests (mock HTTP)
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 4. Scraper Tests (mocked HTTP) ==={RESET}")

_VALID_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Win a Trip Sweepstakes Enter Now!</title>
      <link>https://example-sweepstake.com/win-a-trip</link>
    </item>
    <item>
      <title>Just a normal blog post</title>
      <link>https://example-blog.com/post</link>
    </item>
    <item>
      <title>Enter This Amazing Giveaway Contest</title>
      <link>https://another-sweep.com/giveaway</link>
    </item>
  </channel>
</rss>"""

_MOCK_HTML = """<html>
<body>
<main>
  <a href="https://external-sweep.com/enter-sweepstakes-to-win-prize">
    Win Big Sweepstakes Giveaway Contest Enter Now
  </a>
  <a href="https://another-site.com/some-giveaway-contest">
    Another Sweepstakes Giveaway
  </a>
  <a href="https://twitter.com/someuser">Twitter (should be skipped)</a>
  <a href="/internal-link">Internal link (skip)</a>
  <a href="#">Fragment (skip)</a>
</main>
</body>
</html>"""


def test_scrape_rss_valid():
    from sweepstake_genie.scraper import _scrape_rss
    import requests

    mock_resp = MagicMock()
    mock_resp.text = _VALID_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch.object(requests.Session, "get", return_value=mock_resp):
        results_list = _scrape_rss("https://fake-feed.com/feed/", "test_source")

    assert len(results_list) >= 1, f"Expected at least 1 result, got {len(results_list)}"
    assert all("url" in r for r in results_list)
    assert all("title" in r for r in results_list)
    assert all("source" in r for r in results_list)
    # "win a trip sweepstakes" should match
    urls = [r["url"] for r in results_list]
    assert any("win-a-trip" in u or "sweepstake" in u.lower() or "giveaway" in u.lower() for u in urls), \
        f"No sweepstake URL found in: {urls}"


def test_scrape_rss_filters_non_sweeps():
    from sweepstake_genie.scraper import _scrape_rss
    import requests

    mock_resp = MagicMock()
    mock_resp.text = _VALID_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch.object(requests.Session, "get", return_value=mock_resp):
        results_list = _scrape_rss("https://fake-feed.com/feed/", "test_source")

    titles = [r["title"] for r in results_list]
    assert "Just a normal blog post" not in titles, "Non-sweepstake posts should be filtered"


def test_scrape_pages_basic():
    from sweepstake_genie.scraper import _scrape_pages
    import requests

    mock_resp = MagicMock()
    mock_resp.text = _MOCK_HTML
    mock_resp.raise_for_status = MagicMock()

    with patch.object(requests.Session, "get", return_value=mock_resp), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        results_list = _scrape_pages(
            "https://aggregator.com",
            ["/sweepstakes/"],
            "test_source",
        )

    assert isinstance(results_list, list)
    # Results should have correct shape
    for r in results_list:
        assert "url" in r
        assert "title" in r
        assert "source" in r


def test_scrape_pages_skips_twitter():
    from sweepstake_genie.scraper import _scrape_pages
    import requests

    mock_resp = MagicMock()
    mock_resp.text = _MOCK_HTML
    mock_resp.raise_for_status = MagicMock()

    with patch.object(requests.Session, "get", return_value=mock_resp), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        results_list = _scrape_pages(
            "https://aggregator.com",
            ["/sweepstakes/"],
            "test_source",
        )

    urls = [r["url"] for r in results_list]
    assert not any("twitter.com" in u for u in urls), "Twitter URLs should be filtered"


def test_discover_all_mocked():
    """discover_all with mocked HTTP should run to completion and return a list."""
    from sweepstake_genie import scraper
    import requests

    mock_resp = MagicMock()
    mock_resp.text = _VALID_RSS
    mock_resp.raise_for_status = MagicMock()
    # For non-XML (page sources) also return valid HTML
    mock_resp_html = MagicMock()
    mock_resp_html.text = _MOCK_HTML
    mock_resp_html.raise_for_status = MagicMock()

    def mock_get(url, *args, **kwargs):
        if "feed" in url:
            return mock_resp
        # Reddit JSON
        if "reddit.com" in url:
            r = MagicMock()
            r.json.return_value = {"data": {"children": []}}
            r.raise_for_status = MagicMock()
            return r
        return mock_resp_html

    with patch.object(requests.Session, "get", side_effect=mock_get), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        all_results = scraper.discover_all()

    assert isinstance(all_results, list), "discover_all should return a list"
    # Each entry should have required keys
    for r in all_results:
        assert "url" in r, f"Missing 'url' key in {r}"
        assert "title" in r, f"Missing 'title' key in {r}"
        assert "source" in r, f"Missing 'source' key in {r}"


def test_looks_like_sweepstake():
    from sweepstake_genie.scraper import _looks_like_sweepstake
    assert _looks_like_sweepstake("Win a Car Sweepstakes!", "https://example.com") is True
    assert _looks_like_sweepstake("Enter to Win a Prize", "https://example.com") is True
    assert _looks_like_sweepstake("Random blog post", "https://example.com/giveaway") is True
    assert _looks_like_sweepstake("Random blog post", "https://example.com/recipe") is False


def test_normalize_url():
    from sweepstake_genie.scraper import _normalize_url
    url = "https://example.com/page/?utm_source=facebook&utm_medium=social#section"
    normalized = _normalize_url(url)
    assert "utm_source" not in normalized, "utm_source should be stripped"
    assert "#section" not in normalized, "fragment should be stripped"
    assert "example.com" in normalized


def test_should_skip():
    from sweepstake_genie.scraper import _should_skip
    assert _should_skip("https://twitter.com/user") is True
    assert _should_skip("https://instagram.com/user") is True
    assert _should_skip("https://facebook.com/page") is True
    assert _should_skip("https://example-sweeps.com/enter") is False
    assert _should_skip("https://contestbee.com/sweepstakes/") is False


def test_discover_all_progress_callback():
    """discover_all should invoke the progress callback for every source."""
    from sweepstake_genie import scraper
    import requests

    mock_resp_html = MagicMock()
    mock_resp_html.text = _MOCK_HTML
    mock_resp_html.raise_for_status = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = _VALID_RSS
    mock_resp.raise_for_status = MagicMock()

    def mock_get(url, *args, **kwargs):
        if "feed" in url:
            return mock_resp
        if "reddit.com" in url:
            r = MagicMock()
            r.json.return_value = {"data": {"children": []}}
            r.raise_for_status = MagicMock()
            return r
        return mock_resp_html

    calls: list[tuple[int, int, str]] = []

    def on_progress(done, total, name):
        calls.append((done, total, name))

    with patch.object(requests.Session, "get", side_effect=mock_get), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        scraper.discover_all(max_workers=4, progress=on_progress)

    assert calls, "progress callback was never invoked"
    # Final call's 'done' should equal 'total' (all sources accounted for)
    final_done, final_total, _ = calls[-1]
    assert final_done == final_total, f"progress ended at {final_done}/{final_total}"


def test_discover_all_on_source_callback():
    """discover_all reports a per-source count via on_source for every source."""
    from sweepstake_genie import scraper
    import requests

    mock_resp_html = MagicMock()
    mock_resp_html.text = _MOCK_HTML
    mock_resp_html.raise_for_status = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = _VALID_RSS
    mock_resp.raise_for_status = MagicMock()

    def mock_get(url, *args, **kwargs):
        if "feed" in url:
            return mock_resp
        if "reddit.com" in url:
            r = MagicMock()
            r.json.return_value = {"data": {"children": []}}
            r.raise_for_status = MagicMock()
            return r
        return mock_resp_html

    counts = {}

    def on_source(name, count):
        counts[name] = count

    with patch.object(requests.Session, "get", side_effect=mock_get), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        scraper.discover_all(max_workers=4, on_source=on_source)

    assert counts, "on_source should have been called at least once"
    assert "freebiemom_db" in counts, "every registered source should report a count"
    assert all(isinstance(v, int) for v in counts.values())


def test_get_retries_on_failure():
    """_get should retry transient failures and eventually return None."""
    from sweepstake_genie import scraper
    import requests

    session = MagicMock()
    session.get.side_effect = requests.RequestException("boom")

    with patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        result = scraper._get("https://x.com", session, retries=2)

    assert result is None
    assert session.get.call_count == 3, f"Expected 3 attempts, got {session.get.call_count}"


def test_scrape_freebiemom_db():
    """FreebieMom DB scraper extracts per-card Enter Here links with titles."""
    from sweepstake_genie import scraper
    from bs4 import BeautifulSoup

    page1 = """<html><body>
      <input type="text" name="s" placeholder="Search freebies, sweeps, brands">
      <div class="fmsdb-card">
        <h3>Amazon Spider-Man Premiere Sweepstakes</h3>
        <a class="enter-btn" href="https://amazon.com/sweeps/spiderman">Enter Here</a>
        <a href="/sweepstakes-database/amazon-spiderman/">More Info</a>
      </div>
      <div class="fmsdb-card">
        <h3>Colgate Optic Obsessed Sweepstakes</h3>
        <a href="https://colgate.com/win">Enter Daily</a>
        <a href="https://twitter.com/share">Share</a>
      </div>
    </body></html>"""

    calls = {"n": 0}

    def fake_get(url, session, **kwargs):
        calls["n"] += 1
        if "fmsdb_page=1" in url:
            return BeautifulSoup(page1, "html.parser")
        return None  # page 2+ empty -> pagination stops

    with patch("sweepstake_genie.scraper._get", side_effect=fake_get), \
         patch("sweepstake_genie.scraper.time") as mock_time:
        mock_time.sleep = MagicMock()
        results = scraper._scrape_freebiemom_db(max_pages=5)

    urls = {r["url"] for r in results}
    titles = {r["title"] for r in results}
    assert any("amazon.com/sweeps/spiderman" in u for u in urls), \
        f"should capture the Amazon Enter Here link, got {urls}"
    assert any("colgate.com/win" in u for u in urls), \
        f"should capture the Colgate Enter Daily link, got {urls}"
    assert not any("twitter.com" in u for u in urls), "should skip social share links"
    assert not any("/sweepstakes-database/amazon-spiderman" in u for u in urls), \
        "should skip 'More Info' links, only follow entry buttons"
    assert "Amazon Spider-Man Premiere Sweepstakes" in titles, \
        f"should pull the card heading as title, got {titles}"
    assert all(r["source"] == "freebiemom_db" for r in results)


for fn in [
    test_scrape_rss_valid,
    test_scrape_rss_filters_non_sweeps,
    test_scrape_pages_basic,
    test_scrape_pages_skips_twitter,
    test_scrape_freebiemom_db,
    test_discover_all_on_source_callback,
    test_discover_all_mocked,
    test_discover_all_progress_callback,
    test_get_retries_on_failure,
    test_looks_like_sweepstake,
    test_normalize_url,
    test_should_skip,
]:
    run_test(f"scraper: {fn.__name__.replace('test_scrape_', '').replace('test_', '')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Browser tests (headless Playwright)
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 5. Browser Tests (headless Playwright) ==={RESET}")


async def test_browser_launch_and_close():
    from sweepstake_genie.browser import BrowserManager
    async with BrowserManager(headless=True) as bm:
        assert bm._browser is not None, "Browser should be running"
        assert bm._context is not None, "Context should be created"
    # After exit, resources should be cleaned up (no exception)


async def test_browser_new_page():
    from sweepstake_genie.browser import BrowserManager
    from playwright.async_api import Page
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        assert isinstance(page, Page), "new_page() should return a Playwright Page"
        await page.close()


async def test_browser_stealth_absence_handled():
    """playwright_stealth not installed — new_page should still succeed."""
    from sweepstake_genie.browser import BrowserManager
    async with BrowserManager(headless=True) as bm:
        # stealth import failure is silently swallowed — just check page works
        page = await bm.new_page()
        await page.goto("about:blank")
        assert page.url == "about:blank"
        await page.close()


async def test_browser_double_exit():
    """Calling __aexit__ twice should not raise."""
    from sweepstake_genie.browser import BrowserManager
    bm = BrowserManager(headless=True)
    await bm.__aenter__()
    await bm.__aexit__(None, None, None)
    # Second __aexit__ — all internal refs already closed
    await bm.__aexit__(None, None, None)


async def test_browser_new_page_outside_context():
    """new_page() without context manager should raise RuntimeError."""
    from sweepstake_genie.browser import BrowserManager
    bm = BrowserManager(headless=True)
    try:
        await bm.new_page()
        raise AssertionError("Should have raised RuntimeError")
    except RuntimeError:
        pass  # expected


for fn in [
    test_browser_launch_and_close,
    test_browser_new_page,
    test_browser_stealth_absence_handled,
    test_browser_double_exit,
    test_browser_new_page_outside_context,
]:
    run_test(f"browser: {fn.__name__.replace('test_browser_', '')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — Form filler tests (with browser)
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 6. Form Filler Tests ==={RESET}")

_PROFILE = {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "janedoe@example.com",
    "phone": "555-555-5555",
    "address1": "123 Main St",
    "city": "Springfield",
    "state": "IL",
    "zip": "62701",
}

_EXPIRED_HTML = """<html><body>
<h1>This sweepstakes has ended</h1>
<p>Thank you for your interest. The sweepstakes is now closed.</p>
</body></html>"""


async def test_form_filler_about_blank():
    """about:blank has no form — should return no_form."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import enter_sweepstake
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        result = await enter_sweepstake(page, "about:blank", _PROFILE)
        await page.close()
    assert result["status"] == "no_form", \
        f"Expected 'no_form' for about:blank, got: {result}"


async def test_form_filler_expired_page():
    """Page with expired text should return status='expired'.

    We call fill_and_submit directly (not enter_sweepstake) so that no
    navigation overwrites the content we inject via set_content.
    """
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(_EXPIRED_HTML)
        result = await fill_and_submit(page, _PROFILE)
        await page.close()
    assert result["status"] == "expired", \
        f"Expected 'expired' for closed sweepstake, got: {result}"


def test_platform_handlers_are_coroutines():
    """All platform-specific _enter_* functions must be async coroutines."""
    from sweepstake_genie import form_filler
    handlers = [
        "_enter_rafflecopter",
        "_enter_gleam",
        "_enter_viralsweep",
        "_enter_woobox",
        "_enter_kingsumo",
        "_enter_promosimple",
        "_enter_shortstack",
        "_enter_typeform",
        "_enter_jotform",
        "_enter_sweepwidget",
        "_enter_wishpond",
        "_enter_mailchimp",
        "_enter_wordpress_forms",
        "_enter_secondstreet",
        "_enter_easypromos",
        "_enter_kickofflabs",
        "_enter_vyper",
        "_enter_generic_iframe",
    ]
    non_async = []
    for name in handlers:
        fn = getattr(form_filler, name, None)
        if fn is None:
            non_async.append(f"{name} (not found)")
        elif not asyncio.iscoroutinefunction(fn):
            non_async.append(f"{name} (not async)")
    assert not non_async, f"Non-async handlers: {non_async}"


async def test_form_filler_no_crash_on_complex_page():
    """enter_sweepstake on a page with inputs but no form should return gracefully."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import enter_sweepstake
    html = """<html><body>
    <p>Welcome to this page</p>
    <input type="text" placeholder="Search..." />
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await enter_sweepstake(page, "about:blank", _PROFILE)
        await page.close()
    assert "status" in result, "Result must always have a 'status' key"
    assert result["status"] in ("no_form", "entered", "captcha", "error", "expired"), \
        f"Unexpected status: {result['status']}"


async def test_manual_captcha_detects_solved_token():
    """_is_captcha_solved should see a filled g-recaptcha-response textarea."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _is_captcha_solved, _wait_for_manual_captcha
    html = """<html><body>
    <textarea name="g-recaptcha-response"></textarea>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        # Unsolved: textarea is empty
        assert await _is_captcha_solved(page) is False
        # Fill it as a human solve would, then it should read as solved
        await page.eval_on_selector(
            'textarea[name="g-recaptcha-response"]',
            "el => el.value = 'FAKE_TOKEN_VALUE'",
        )
        assert await _is_captcha_solved(page) is True
        # wait helper should return True quickly since it's already solved
        assert await _wait_for_manual_captcha(page, timeout=5) is True
        await page.close()


async def test_manual_captcha_times_out():
    """_wait_for_manual_captcha should return False if never solved."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _wait_for_manual_captcha
    html = """<html><body>
    <textarea name="g-recaptcha-response"></textarea>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        # Short timeout; token never filled -> should time out to False
        assert await _wait_for_manual_captcha(page, timeout=1) is False
        await page.close()


async def test_detect_captcha_interactive_v2():
    """A visible, normal-size reCAPTCHA container is reported as interactive."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _detect_captcha
    html = """<html><body>
    <div class="g-recaptcha" data-sitekey="ABC123"
         style="width:304px;height:78px;background:#eee"></div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        ctype, sitekey, interactive = await _detect_captcha(page)
        await page.close()
    assert ctype == "recaptcha", f"Expected recaptcha, got {ctype}"
    assert sitekey == "ABC123", f"Expected sitekey ABC123, got {sitekey}"
    assert interactive is True, "Normal-size v2 widget should be interactive"


async def test_detect_captcha_v2_zero_height_still_interactive():
    """A v2 container that hasn't rendered its iframe yet (height 0) is still
    treated as interactive — this was the bug that skipped real CAPTCHAs."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _detect_captcha
    html = """<html><body>
    <div class="g-recaptcha" data-sitekey="ABC"
         style="width:304px;height:0px"></div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        ctype, sitekey, interactive = await _detect_captcha(page)
        await page.close()
    assert ctype == "recaptcha"
    assert interactive is True, \
        "A not-yet-rendered v2 widget must still be interactive (not skipped)"


async def test_detect_captcha_invisible_v3_not_interactive():
    """An invisible reCAPTCHA v3 (data-size=invisible) is NOT interactive."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _detect_captcha
    html = """<html><body>
    <div class="g-recaptcha" data-sitekey="V3KEY" data-size="invisible"></div>
    <div class="grecaptcha-badge" style="width:256px;height:60px"></div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        ctype, sitekey, interactive = await _detect_captcha(page)
        await page.close()
    assert ctype == "recaptcha", f"Expected recaptcha, got {ctype}"
    assert interactive is False, "Invisible v3 must not be interactive"


async def test_detect_captcha_none_on_plain_page():
    """A plain entry page with no CAPTCHA returns (None, None, False)."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _detect_captcha
    html = """<html><body>
    <form><input name="email" placeholder="Email"><button>Enter</button></form>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        ctype, sitekey, interactive = await _detect_captcha(page)
        await page.close()
    assert ctype is None, f"Plain page should have no captcha, got {ctype}"
    assert interactive is False


async def test_defer_captcha_returns_needs_captcha():
    """defer_captcha=True on an interactive v2 page returns needs_captcha."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <form><input name="email" placeholder="Email"></form>
    <div class="g-recaptcha" data-sitekey="XYZ"
         style="width:304px;height:78px;background:#eee"></div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, defer_captcha=True
        )
        await page.close()
    assert result["status"] == "needs_captcha", \
        f"Interactive captcha with defer should return needs_captcha, got {result}"


async def test_defer_captcha_ignores_invisible_v3():
    """defer_captcha=True must NOT queue a page whose captcha is invisible v3."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <form><input name="email" placeholder="Email"></form>
    <div class="g-recaptcha" data-sitekey="V3" data-size="invisible"></div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, defer_captcha=True
        )
        await page.close()
    assert result["status"] != "needs_captcha", \
        f"Invisible v3 must not be queued for manual solve, got {result}"


async def test_fill_required_controls():
    """Required dropdowns, radio groups, and eligibility checkboxes get filled."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _fill_required_controls
    html = """<html><body><form>
      <select name="country">
        <option value="">Select</option>
        <option value="US">United States</option>
        <option value="CA">Canada</option>
      </select>
      <select name="prize">
        <option value="">Choose a prize</option>
        <option value="a">Prize A</option>
        <option value="b">Prize B</option>
      </select>
      <input type="checkbox" name="agree_rules" required>
      <label><input type="radio" name="eligible" value="no"> No</label>
      <label><input type="radio" name="eligible" value="yes"> Yes</label>
    </form></body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        await _fill_required_controls(page, {"country": "US"})
        country = await page.eval_on_selector("select[name=country]", "e => e.value")
        prize = await page.eval_on_selector("select[name=prize]", "e => e.value")
        rules = await page.eval_on_selector("input[name=agree_rules]", "e => e.checked")
        yes = await page.eval_on_selector("input[name=eligible][value=yes]", "e => e.checked")
        await page.close()
    assert country == "US", f"country should be US, got {country!r}"
    assert prize == "a", f"prize should pick first real option 'a', got {prize!r}"
    assert rules is True, "required rules checkbox should be checked"
    assert yes is True, "yes/no eligibility radio should pick 'yes'"


async def test_detect_blocking_wall():
    """Geo blocks and form-less login walls are recognised; normal eligibility
    text (age rules) and pages with an entry form are NOT skipped."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _detect_blocking_wall
    cases = [
        # Geo block — always a wall
        ("<body>This promotion is not available in your country.</body>", "region"),
        # Login text with NO form — a wall
        ("<body>Please sign in to enter.<a href='/login'>Sign In</a></body>", "login"),
        # Login text but WITH an entry form — NOT a wall (attempt it)
        ("<body>Sign in to enter faster, or enter below.<form>"
         "<input name=first_name><input name=last_name><input name=city>"
         "<button>Enter</button></form></body>", None),
        # Age eligibility rule — NOT a wall (very common on legit pages)
        ("<body>You must be at least 18 to enter this sweepstakes.</body>", None),
        # Normal entry page
        ("<body><h1>Enter to Win a Car</h1><form><input name=email>"
         "<button>Enter</button></form></body>", None),
    ]
    async with BrowserManager(headless=True) as bm:
        for html, expect in cases:
            page = await bm.new_page()
            await page.set_content(html)
            reason = await _detect_blocking_wall(page)
            await page.close()
            if expect is None:
                assert reason is None, f"expected no wall, got {reason!r} for {html[:45]}"
            else:
                assert reason and expect in reason, \
                    f"expected '{expect}' wall, got {reason!r} for {html[:45]}"


async def test_has_unmet_required():
    """HTML5 validation state is detected: empty required = unmet, filled = met."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _has_unmet_required
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content("<form><input type='email' required></form>")
        empty = await _has_unmet_required(page)
        await page.set_content("<form><input type='email' required value='a@b.com'></form>")
        filled = await _has_unmet_required(page)
        await page.close()
    assert empty is True, "empty required field should be unmet"
    assert filled is False, "valid filled required field should be met"


async def test_fill_and_submit_skips_login_wall():
    """A page that requires login to enter should be skipped as no_form."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <h1>Sweepstakes</h1>
    <p>Please log in to enter.</p>
    <a href="/login">Sign In</a>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await fill_and_submit(page, _PROFILE, manual_captcha=False)
        await page.close()
    assert result["status"] == "no_form", \
        f"Form-less login wall should be skipped as no_form, got {result}"


async def test_has_form_fields_ignores_search():
    """A site search box must NOT count as a fillable form (FreebieMom case)."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _has_form_fields
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(
            "<input type='text' name='s' placeholder='Search freebies, sweeps, brands'>")
        search_only = await _has_form_fields(page)
        await page.set_content("<input type='search' aria-label='Search'>")
        typed_search = await _has_form_fields(page)
        await page.set_content("<input type='email' name='email'>")
        real = await _has_form_fields(page)
        await page.close()
    assert search_only is False, "a text search box should not count as a form field"
    assert typed_search is False, "type=search should not count as a form field"
    assert real is True, "a real email input should count as a form field"


async def test_advance_past_search_box_to_enter_here():
    """A listing-style page (search box + 'Enter Here') should still advance to
    the entry form when 'Enter Here' reveals it."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _advance_to_entry_form, _count_entry_fields
    html = """<html><body>
    <input type="text" name="s" placeholder="Search freebies, sweeps, brands">
    <button onclick="document.getElementById('f').style.display='block'">Enter Here</button>
    <div id="f" style="display:none">
      <input name="first_name"><input name="last_name"><input name="city">
    </div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        advanced = await _advance_to_entry_form(page)
        after = await _count_entry_fields(page)
        await page.close()
    assert advanced is True, "should click 'Enter Here' despite the search box"
    assert after >= 2, f"entry form should be revealed, got {after} fields"


async def test_advance_to_entry_form_clicks_enter():
    """A landing page with an 'Enter Now' button that reveals the form should be
    advanced to the form automatically."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _advance_to_entry_form, _count_entry_fields
    html = """<html><body>
    <h1>Win a Car Sweepstakes</h1>
    <button id="enter" onclick="document.getElementById('f').style.display='block'">Enter Now</button>
    <div id="f" style="display:none">
      <input name="first_name" placeholder="First">
      <input name="last_name" placeholder="Last">
      <input name="city" placeholder="City">
    </div>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        before = await _count_entry_fields(page)
        advanced = await _advance_to_entry_form(page)
        after = await _count_entry_fields(page)
        await page.close()
    assert before < 2, f"Form should be hidden before clicking, got {before} fields"
    assert advanced is True, "Should have clicked the Enter button"
    assert after >= 2, f"Form should be revealed after clicking Enter, got {after} fields"


async def test_manual_mode_skips_newsletter_captcha():
    """Manual mode must NOT pause on a newsletter box (email + Subscribe) + CAPTCHA
    — that's a mailing-list signup, not a sweepstakes entry. Skip to no_form."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <form>
      <input type="email" name="email" placeholder="Email">
      <button type="submit">Subscribe</button>
      <div class="g-recaptcha" data-sitekey="X" style="width:304px;height:78px"></div>
    </form>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, manual_captcha_timeout=1
        )
        await page.close()
    assert result["status"] == "no_form", \
        f"Newsletter (Subscribe) box + captcha should skip to no_form, got {result}"


async def test_email_plus_plain_submit_is_entry():
    """Email box + a plain 'Submit' button IS a real entry (must not be skipped)."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _is_real_entry_form
    html = """<html><body><h1>Win a Prize</h1><form>
      <input type="email" name="email" placeholder="Your email">
      <button type="submit">Submit</button>
    </form></body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        is_entry = await _is_real_entry_form(page)
        await page.close()
    assert is_entry is True, "email + plain Submit should be treated as an entry"


async def test_comment_form_is_not_entry():
    """A blog comment box (email + 'Post Comment') is NOT a sweepstakes entry."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import _is_real_entry_form
    html = """<html><body><form>
      <input type="email" name="email" placeholder="Email">
      <textarea name="comment"></textarea>
      <button type="submit">Post Comment</button>
    </form></body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        is_entry = await _is_real_entry_form(page)
        await page.close()
    assert is_entry is False, "a comment form should not be treated as an entry"


async def test_manual_mode_pauses_on_email_only_entry():
    """An email-only sweepstakes ('enter your email' + Enter button) + CAPTCHA is
    a REAL entry — manual mode should pause for it, not skip it."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <h1>Win a Trip Sweepstakes</h1>
    <form>
      <input type="email" name="email" placeholder="Email">
      <button type="submit">Enter to Win</button>
      <div class="g-recaptcha" data-sitekey="X" style="width:304px;height:78px"></div>
    </form>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, manual_captcha_timeout=1
        )
        await page.close()
    assert result["status"] == "captcha", \
        f"Email-only entry + captcha should pause (then time out), got {result}"


async def test_manual_mode_pauses_on_real_entry_form():
    """Manual mode SHOULD pause on a real multi-field entry form + CAPTCHA."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <form>
      <input name="first_name" placeholder="First">
      <input name="last_name" placeholder="Last">
      <input name="city" placeholder="City">
      <input type="email" name="email" placeholder="Email">
      <div class="g-recaptcha" data-sitekey="X" style="width:304px;height:78px"></div>
      <button type="submit">Enter</button>
    </form>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        # timeout=1: it should PAUSE (real form), time out unsolved -> captcha
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, manual_captcha_timeout=1
        )
        await page.close()
    assert result["status"] == "captcha", \
        f"Real entry form + captcha should pause (then time out), got {result}"


async def test_manual_mode_skips_invisible_v3():
    """Manual mode must NOT hang on a page that only has invisible v3."""
    from sweepstake_genie.browser import BrowserManager
    from sweepstake_genie.form_filler import fill_and_submit
    html = """<html><body>
    <div class="grecaptcha-badge" style="width:256px;height:60px"></div>
    <p>Some article content, no entry form here.</p>
    </body></html>"""
    async with BrowserManager(headless=True) as bm:
        page = await bm.new_page()
        await page.set_content(html)
        # timeout of 2s: if the code incorrectly waited for a manual solve it
        # would block ~2s and then return captcha; instead it should fall
        # through to no_form quickly.
        result = await fill_and_submit(
            page, _PROFILE, manual_captcha=True, manual_captcha_timeout=2
        )
        await page.close()
    assert result["status"] in ("no_form", "entered", "error"), \
        f"Manual mode should not report captcha for invisible v3, got {result}"


for fn in [
    test_form_filler_about_blank,
    test_form_filler_expired_page,
    test_platform_handlers_are_coroutines,
    test_form_filler_no_crash_on_complex_page,
    test_manual_captcha_detects_solved_token,
    test_manual_captcha_times_out,
    test_detect_captcha_interactive_v2,
    test_detect_captcha_v2_zero_height_still_interactive,
    test_detect_captcha_invisible_v3_not_interactive,
    test_detect_captcha_none_on_plain_page,
    test_fill_required_controls,
    test_detect_blocking_wall,
    test_has_unmet_required,
    test_fill_and_submit_skips_login_wall,
    test_has_form_fields_ignores_search,
    test_advance_past_search_box_to_enter_here,
    test_advance_to_entry_form_clicks_enter,
    test_manual_mode_skips_newsletter_captcha,
    test_email_plus_plain_submit_is_entry,
    test_comment_form_is_not_entry,
    test_manual_mode_pauses_on_email_only_entry,
    test_manual_mode_pauses_on_real_entry_form,
    test_manual_mode_skips_invisible_v3,
    test_defer_captcha_returns_needs_captcha,
    test_defer_captcha_ignores_invisible_v3,
]:
    run_test(f"form_filler: {fn.__name__.replace('test_form_filler_', '').replace('test_', '')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Section 7 — Runner / CLI tests
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}=== 7. Runner / CLI Tests ==={RESET}")


def test_runner_run_status():
    """run_status should print a table without crashing."""
    from sweepstake_genie.database import Database
    from sweepstake_genie.runner import run_status
    import io
    tmp = tempfile.mktemp(suffix=".db")
    db = Database(tmp)
    db.add_sweepstake("https://a.com", "A", "t")
    db.mark_entered("https://a.com")
    # Should not raise
    run_status(db)


def test_runner_run_discover_mocked():
    """run_discover with mocked discover_all should add entries to DB."""
    from sweepstake_genie.database import Database
    from sweepstake_genie.runner import run_discover

    mock_entries = [
        {"url": "https://sweep1.com/enter", "title": "Sweep 1", "source": "mock"},
        {"url": "https://sweep2.com/giveaway", "title": "Sweep 2", "source": "mock"},
    ]

    tmp = tempfile.mktemp(suffix=".db")
    db = Database(tmp)

    with patch("sweepstake_genie.runner.discover_all", return_value=mock_entries):
        count = run_discover(db)

    assert count == 2, f"Expected 2 new entries, got {count}"
    assert db.url_exists("https://sweep1.com/enter")
    assert db.url_exists("https://sweep2.com/giveaway")


def test_runner_run_discover_no_duplicates():
    """run_discover should not double-count already-known URLs."""
    from sweepstake_genie.database import Database
    from sweepstake_genie.runner import run_discover

    mock_entries = [
        {"url": "https://sweep1.com/enter", "title": "Sweep 1", "source": "mock"},
    ]

    tmp = tempfile.mktemp(suffix=".db")
    db = Database(tmp)
    db.add_sweepstake("https://sweep1.com/enter", "Sweep 1", "mock")  # pre-existing

    with patch("sweepstake_genie.runner.discover_all", return_value=mock_entries):
        count = run_discover(db)

    assert count == 0, f"Expected 0 new (duplicate), got {count}"


def test_cli_status_exits_zero():
    """CLI `python main.py status` should exit 0."""
    result = subprocess.run(
        [sys.executable, "main.py", "status"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, \
        f"Expected exit 0, got {result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"


def test_cli_config_exits_zero():
    """CLI `python main.py config` should exit 0."""
    result = subprocess.run(
        [sys.executable, "main.py", "config"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, \
        f"Expected exit 0, got {result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"


def test_cli_help_exits_zero():
    """CLI `python main.py --help` should exit 0."""
    result = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, \
        f"Expected exit 0, got {result.returncode}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"


for fn in [
    test_runner_run_status,
    test_runner_run_discover_mocked,
    test_runner_run_discover_no_duplicates,
    test_cli_status_exits_zero,
    test_cli_config_exits_zero,
    test_cli_help_exits_zero,
]:
    run_test(f"runner/cli: {fn.__name__.replace('test_runner_', '').replace('test_cli_', 'cli_')}", fn)


# ══════════════════════════════════════════════════════════════════════════════
# Final Summary Table
# ══════════════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'='*60}{RESET}")
print(f"{BOLD}SUMMARY{RESET}")
print(f"{'='*60}")

passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
total  = len(results)

# Column widths
name_w = max(len(r["name"]) for r in results) + 2

print(f"{'Test Name':<{name_w}}  {'Result'}")
print(f"{'-'*name_w}  {'------'}")
for r in results:
    tag = f"{GREEN}PASS{RESET}" if r["status"] == "PASS" else f"{RED}FAIL{RESET}"
    print(f"{r['name']:<{name_w}}  {tag}")

print(f"\n{'='*60}")
score_colour = GREEN if failed == 0 else (YELLOW if failed < total // 2 else RED)
print(f"{BOLD}{score_colour}{passed}/{total} tests passed{RESET}  ({failed} failed)")
print(f"{'='*60}\n")

if failed > 0:
    print(f"{RED}FAILED TESTS:{RESET}")
    for r in results:
        if r["status"] == "FAIL":
            print(f"  - {r['name']}")
            if r["error"]:
                first_line = r["error"].strip().splitlines()[0]
                print(f"    {YELLOW}{first_line}{RESET}")

sys.exit(0 if failed == 0 else 1)
