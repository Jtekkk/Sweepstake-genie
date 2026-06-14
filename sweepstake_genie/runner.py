"""
Orchestrates the full sweepstakes discovery → entry flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.console import Console
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
    """Scrape all sources and save new sweepstakes to the database."""
    console.print("[bold cyan]Discovering sweepstakes…[/bold cyan]")
    sweepstakes = discover_all()
    new_count = 0
    for sw in sweepstakes:
        added = db.add_sweepstake(sw["url"], sw["title"], sw["source"])
        if added:
            new_count += 1
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
        page  = await bm.new_page()
        try:
            result = await enter_sweepstake(page, url, config.profile, captcha_solver)
            status = result["status"]
            if status == "entered":
                db.mark_entered(url)
                icon = "[green]✓[/green]"
            elif status == "captcha":
                db.mark_captcha(url)
                icon = "[yellow]⚠[/yellow]"
            elif status == "no_form":
                db.mark_skipped(url, "no entry form detected")
                icon = "[dim]–[/dim]"
            else:
                db.mark_error(url, result.get("message", "unknown"))
                icon = "[red]✗[/red]"
            console.print(f"  {icon} [{idx}/{total}] {title}")
        finally:
            await page.close()

        if config.delay_between_entries > 0:
            await asyncio.sleep(config.delay_between_entries)


async def _run_enter_async(config: Config, db: Database) -> None:
    captcha_solver = CaptchaSolver(
        service=config.captcha_service,
        api_key=config.captcha_api_key,
    )

    pending = db.get_pending()
    daily   = db.get_due_for_reentry()
    pending_urls = {sw["url"] for sw in pending}
    all_entries = pending + [e for e in daily if e["url"] not in pending_urls]

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
        await asyncio.gather(*tasks, return_exceptions=True)

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


def run_enter(config: Config, db: Database) -> None:
    """Entry point for the entry phase — runs parallel workers."""
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
    elif status == "captcha":
        db.mark_captcha(url)
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
