"""
Orchestrates the full sweepstakes discovery → entry flow.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

from .browser import BrowserManager
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

async def _enter_batch(
    pending: list[dict[str, Any]],
    config: Config,
    db: Database,
) -> dict[str, int]:
    """Enter a list of pending sweepstakes using Playwright."""
    counts: dict[str, int] = {"entered": 0, "captcha": 0, "no_form": 0, "error": 0}

    async with BrowserManager(headless=config.headless) as bm:
        page = await bm.new_page()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Entering sweepstakes…", total=len(pending))

            for sw in pending:
                url   = sw["url"]
                title = (sw.get("title") or url)[:60]

                result = await enter_sweepstake(page, url, config.profile)
                status = result["status"]

                if status == "entered":
                    db.mark_entered(url)
                    counts["entered"] += 1
                elif status == "captcha":
                    db.mark_captcha(url)
                    counts["captcha"] += 1
                elif status == "no_form":
                    db.mark_skipped(url, "no entry form detected")
                    counts["no_form"] += 1
                else:
                    msg = result.get("message", "unknown error")
                    db.mark_error(url, msg)
                    counts["error"] += 1
                    logger.warning("Error on %s: %s", url, msg)

                icon = _status_icon(status)
                console.print(f"{icon} {title}")
                progress.advance(task)

                if config.delay_between_entries > 0:
                    await asyncio.sleep(config.delay_between_entries)

    return counts


def run_enter(config: Config, db: Database) -> dict[str, int]:
    """Entry point for the entry phase; wraps the async batch runner."""
    pending = db.get_pending()
    if not pending:
        console.print("[yellow]No pending sweepstakes to enter.[/yellow]")
        return {}

    limit = config.max_entries_per_run
    if len(pending) > limit:
        console.print(
            f"[dim]{len(pending)} pending; capping this run at {limit}.[/dim]"
        )
        pending = pending[:limit]

    console.print(f"[bold cyan]Entering {len(pending)} sweepstakes…[/bold cyan]")
    counts = asyncio.run(_enter_batch(pending, config, db))

    # Summary
    console.print()
    table = Table(title="Entry Results", show_header=True)
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_row("[green]Entered[/green]",   str(counts.get("entered",  0)))
    table.add_row("[yellow]CAPTCHA[/yellow]", str(counts.get("captcha",  0)))
    table.add_row("[dim]No form[/dim]",       str(counts.get("no_form",  0)))
    table.add_row("[red]Error[/red]",         str(counts.get("error",    0)))
    console.print(table)

    return counts


# ── Single URL entry ──────────────────────────────────────────────────────────

async def _enter_one(url: str, config: Config, db: Database) -> None:
    async with BrowserManager(headless=config.headless) as bm:
        page = await bm.new_page()
        result = await enter_sweepstake(page, url, config.profile)

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
