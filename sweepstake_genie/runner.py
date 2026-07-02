"""
Orchestrates the full sweepstakes discovery → entry flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from .browser import BrowserManager, ensure_browser_installed
from .captcha_solver import CaptchaSolver
from .config import Config
from .database import Database
from .form_filler import enter_sweepstake
from .scraper import discover_all

console = Console()
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_icon(status: str) -> str:
    return {
        "entered": "[green]✓[/green]",
        "captcha": "[yellow]⚠[/yellow]",
        "no_form": "[dim]–[/dim]",
        "error":   "[red]✗[/red]",
        "skipped": "[dim]↷[/dim]",
    }.get(status, "?")


# ── Discovery ─────────────────────────────────────────────────────────────────

def run_discover(db: Database) -> int:
    """Scrape all sources concurrently and save new sweepstakes to the database."""
    console.print("[bold cyan]Discovering sweepstakes…[/bold cyan]")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("[dim]{task.fields[source]}[/dim]"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scraping sources", total=None, source="")

        def _on_progress(done: int, total: int, source: str) -> None:
            progress.update(task, total=total, completed=done, source=source)

        sweepstakes = discover_all(progress=_on_progress)

    new_count = db.add_sweepstakes_bulk(sweepstakes)
    console.print(
        f"[green]Found {len(sweepstakes)} sweepstakes, "
        f"{new_count} new added to database.[/green]"
    )
    return new_count


# ── Entry ─────────────────────────────────────────────────────────────────────

async def _enter_worker(
    bm,
    sw: dict,
    idx: int,
    total: int,
    config: Config,
    db: Database,
    captcha_solver,
    semaphore: asyncio.Semaphore,
    stop_event: asyncio.Event,
    *,
    defer_captcha: bool = False,
    deferred_sink: list | None = None,
) -> None:
    """Enter a single sweepstake inside a semaphore-guarded slot."""
    async with semaphore:
        if stop_event.is_set():
            return
        url   = sw["url"]
        title = (sw.get("title") or url)[:70]
        page  = None
        try:
            page = await bm.new_page()
            result = await enter_sweepstake(
                page, url, config.profile, captcha_solver,
                manual_captcha=config.manual_captcha,
                manual_captcha_timeout=config.manual_captcha_timeout,
                defer_captcha=defer_captcha,
            )
            status = result["status"]
            if status == "needs_captcha":
                # Headless pass found an interactive CAPTCHA — queue it for the
                # visible pass instead of marking it in the database now.
                if deferred_sink is not None:
                    deferred_sink.append(sw)
                    console.print(
                        f"  [cyan]⧗[/cyan] [{idx}/{total}] {title} [needs CAPTCHA → queued]"
                    )
                else:
                    db.mark_captcha(url)
                    console.print(f"  [yellow]⚠[/yellow] [{idx}/{total}] {title} [captcha]")
                return
            if status == "entered":
                db.mark_entered(url)
                if result.get("allows_daily"):
                    db.mark_allows_daily(url)
                icon = "[green]✓[/green]"
            elif status == "captcha":
                db.mark_captcha(url)
                icon = "[yellow]⚠[/yellow]"
            elif status == "expired":
                db.mark_skipped(url, "expired")
                icon = "[dim]⌛[/dim]"
            elif status == "no_form":
                db.mark_skipped(url, "no entry form detected")
                icon = "[dim]–[/dim]"
            else:
                db.mark_error(url, result.get("message", "unknown"))
                icon = "[red]✗[/red]"
            detail = ""
            if status == "captcha":
                detail = " [captcha]"
            elif status == "expired":
                detail = " [expired]"
            elif status == "no_form":
                detail = " [no form]"
            elif status == "error":
                detail = f" [{result.get('message', 'error')[:40]}]"
            console.print(f"  {icon} [{idx}/{total}] {title}{detail}")
        except Exception as exc:
            db.mark_error(url, str(exc))
            logger.error("[%d/%d] %s — %s", idx, total, title, exc)
            console.print(f"  [red]✗[/red] [{idx}/{total}] {title} — {exc}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    # Sleep OUTSIDE the semaphore so other workers can claim the slot immediately
    if config.delay_between_entries > 0:
        await asyncio.sleep(config.delay_between_entries)


async def _run_enter_async(config: Config, db: Database) -> None:
    captcha_solver = CaptchaSolver(
        service=config.captcha_service,
        api_key=config.captcha_api_key,
    )

    # In manual mode the user solves CAPTCHAs by hand, so previously-skipped
    # CAPTCHA entries become retryable too.
    include_captcha = captcha_solver.enabled or config.manual_captcha

    pending   = db.get_pending()
    daily     = db.get_due_for_reentry()
    retryable = db.get_retryable(include_captcha=include_captcha)

    seen_urls: set[str] = {sw["url"] for sw in pending}
    for e in daily:
        if e["url"] not in seen_urls:
            pending.append(e)
            seen_urls.add(e["url"])
    for e in retryable:
        if e["url"] not in seen_urls:
            pending.append(e)
            seen_urls.add(e["url"])
    all_entries = pending

    if not all_entries:
        console.print("[yellow]No sweepstakes to enter.[/yellow]")
        return

    limit = config.max_entries_per_run
    if len(all_entries) > limit:
        console.print(f"[dim]{len(all_entries)} entries available; capping at {limit}[/dim]")
        all_entries = all_entries[:limit]

    total      = len(all_entries)
    stop_event = asyncio.Event()

    if config.manual_captcha:
        # ── Two-pass manual mode ──────────────────────────────────────────────
        # Pass 1 runs everything HEADLESS (no window) and simply queues any page
        # that shows a real, interactive CAPTCHA. Pass 2 opens a single VISIBLE
        # window, one page at a time, only for those queued pages — so windows
        # appear for CAPTCHAs, never for ordinary entry pages.
        deferred: list[dict] = []
        concurrency = max(1, config.concurrency)
        console.print(
            f"[bold]Pass 1/2 — processing {total} sweepstakes in the background "
            f"({concurrency} workers, no window)…[/bold]"
        )
        sem1 = asyncio.Semaphore(concurrency)
        async with BrowserManager(headless=True) as bm:
            tasks = [
                _enter_worker(bm, sw, i + 1, total, config, db, captcha_solver,
                              sem1, stop_event,
                              defer_captcha=True, deferred_sink=deferred)
                for i, sw in enumerate(all_entries)
            ]
            for r in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(r, BaseException):
                    logger.error("Worker task raised unhandled exception: %s", r)

        if deferred:
            n = len(deferred)
            console.print(
                f"\n[bold yellow]Pass 2/2 — {n} sweepstake(s) need a CAPTCHA.[/bold yellow] "
                "A browser window will open for each one; solve it and the entry "
                f"finishes automatically (up to {int(config.manual_captcha_timeout)}s each)."
            )
            sem2 = asyncio.Semaphore(1)  # one visible window at a time
            async with BrowserManager(headless=False) as bm:
                tasks = [
                    _enter_worker(bm, sw, i + 1, n, config, db, captcha_solver,
                                  sem2, stop_event,
                                  defer_captcha=False, deferred_sink=None)
                    for i, sw in enumerate(deferred)
                ]
                for r in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(r, BaseException):
                        logger.error("Worker task raised unhandled exception: %s", r)
        else:
            console.print(
                "[green]No CAPTCHAs needed solving — everything ran in the "
                "background with no window.[/green]"
            )
    else:
        # ── Standard single-pass mode ─────────────────────────────────────────
        concurrency = config.concurrency
        semaphore   = asyncio.Semaphore(concurrency)
        console.print(
            f"[bold]Entering {total} sweepstakes ({concurrency} parallel workers)…[/bold]"
        )
        async with BrowserManager(headless=config.headless) as bm:
            tasks = [
                _enter_worker(bm, sw, i + 1, total, config, db, captcha_solver,
                              semaphore, stop_event)
                for i, sw in enumerate(all_entries)
            ]
            for r in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(r, BaseException):
                    logger.error("Worker task raised unhandled exception: %s", r)

    stats = db.get_stats()
    console.print()
    table = Table(title="Entry Results", show_header=True)
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("[green]Entered[/green]",   str(stats.get("entered", 0)))
    table.add_row("[yellow]CAPTCHA[/yellow]", str(stats.get("captcha", 0)))
    table.add_row("[dim]No form[/dim]",       str(stats.get("skipped", 0)))
    table.add_row("[red]Error[/red]",         str(stats.get("error",   0)))
    console.print(table)


def _warn_if_captcha_unusable(config: Config) -> None:
    """Warn early if a CAPTCHA service is configured but appears unusable."""
    if config.captcha_service == "none" or not config.captcha_api_key:
        return
    solver = CaptchaSolver(
        service=config.captcha_service,
        api_key=config.captcha_api_key,
    )
    balance = solver.check_balance()
    if balance is None:
        console.print(
            f"[yellow]⚠ Could not verify {config.captcha_service} balance — "
            "the API key may be invalid or the service unreachable.[/yellow]"
        )
    elif balance <= 0:
        console.print(
            f"[yellow]⚠ {config.captcha_service} balance is ${balance:.2f} — "
            "CAPTCHAs will not be solved until you top up.[/yellow]"
        )
    else:
        console.print(
            f"[dim]{config.captcha_service} balance: ${balance:.2f}[/dim]"
        )


def _ensure_browser_ready() -> bool:
    """Ensure Chromium is installed, printing status. Returns False on failure."""
    if not ensure_browser_installed(progress_cb=lambda msg: console.print(f"[dim]{msg}[/dim]")):
        console.print(
            "[red]Could not set up the browser. Ensure you have an internet "
            "connection, then try again. You can also run "
            "'playwright install chromium' manually.[/red]"
        )
        return False
    return True


def run_enter(config: Config, db: Database) -> None:
    """Entry point for the entry phase — runs parallel workers."""
    if not _ensure_browser_ready():
        return
    _warn_if_captcha_unusable(config)
    asyncio.run(_run_enter_async(config, db))


# ── Single URL entry ──────────────────────────────────────────────────────────

async def _enter_one(url: str, config: Config, db: Database) -> None:
    captcha_solver = CaptchaSolver(
        service=config.captcha_service,
        api_key=config.captcha_api_key,
    )
    # Manual CAPTCHA mode needs a visible window so the user can solve it.
    headless = config.headless and not config.manual_captcha
    if config.manual_captcha:
        console.print(
            "[bold yellow]Manual CAPTCHA mode:[/bold yellow] solve the CAPTCHA in "
            "the browser window when prompted."
        )
    async with BrowserManager(headless=headless) as bm:
        page = await bm.new_page()
        result = await enter_sweepstake(
            page, url, config.profile, captcha_solver,
            manual_captcha=config.manual_captcha,
            manual_captcha_timeout=config.manual_captcha_timeout,
        )

    status = result["status"]
    icon   = _status_icon(status)
    console.print(f"{icon} {status.upper()}: {url}")

    if status == "entered":
        db.mark_entered(url)
        if result.get("allows_daily"):
            db.mark_allows_daily(url)
    elif status == "captcha":
        db.mark_captcha(url)
    elif status == "expired":
        db.mark_skipped(url, "expired")
    elif status == "no_form":
        db.mark_skipped(url, "no entry form detected")
    else:
        msg = result.get("message", "")
        db.mark_error(url, msg)
        if msg:
            console.print(f"  [red]{msg}[/red]")


def run_enter_url(url: str, config: Config, db: Database) -> None:
    """Enter a single sweepstake URL."""
    if not _ensure_browser_ready():
        return
    if not db.url_exists(url):
        db.add_sweepstake(url, url, "manual")
    asyncio.run(_enter_one(url, config, db))


# ── Status display ────────────────────────────────────────────────────────────

def run_status(db: Database) -> None:
    stats = db.get_stats()
    table = Table(title="Sweepstakes Database Status", show_header=True)
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("Total",                         str(stats.get("total",   0)))
    table.add_row("[cyan]Pending[/cyan]",           str(stats.get("pending", 0)))
    table.add_row("[green]Entered[/green]",         str(stats.get("entered", 0)))
    table.add_row("[yellow]CAPTCHA (skipped)[/yellow]", str(stats.get("captcha", 0)))
    table.add_row("[dim]No form (skipped)[/dim]",  str(stats.get("skipped", 0)))
    table.add_row("[red]Error[/red]",              str(stats.get("error",   0)))
    console.print(table)
