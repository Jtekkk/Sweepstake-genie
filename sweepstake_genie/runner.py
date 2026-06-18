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

from .browser import BrowserManager
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
            result = await enter_sweepstake(page, url, config.profile, captcha_solver)
            status = result["status"]
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

    pending   = db.get_pending()
    daily     = db.get_due_for_reentry()
    retryable = db.get_retryable(include_captcha=captcha_solver.enabled)

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

    total       = len(all_entries)
    concurrency = config.concurrency
    semaphore   = asyncio.Semaphore(concurrency)
    stop_event  = asyncio.Event()

    console.print(f"[bold]Entering {total} sweepstakes ({concurrency} parallel workers)…[/bold]")

    async with BrowserManager(headless=config.headless) as bm:
        tasks = [
            _enter_worker(bm, sw, i + 1, total, config, db, captcha_solver,
                          semaphore, stop_event)
            for i, sw in enumerate(all_entries)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
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


def run_enter(config: Config, db: Database) -> None:
    """Entry point for the entry phase — runs parallel workers."""
    _warn_if_captcha_unusable(config)
    asyncio.run(_run_enter_async(config, db))


# ── Single URL entry ──────────────────────────────────────────────────────────

async def _enter_one(url: str, config: Config, db: Database) -> None:
    captcha_solver = CaptchaSolver(
        service=config.captcha_service,
        api_key=config.captcha_api_key,
    )
    async with BrowserManager(headless=config.headless) as bm:
        page = await bm.new_page()
        result = await enter_sweepstake(page, url, config.profile, captcha_solver)

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
