"""
Sweepstake Genie — CLI entry point.

Commands
--------
  run          Discover sweepstakes then enter all pending ones.
  discover     Only discover and save sweepstakes (no entry).
  enter-url    Enter a single specific sweepstake URL.
  status       Show database statistics.
"""

from __future__ import annotations

import logging
import sys

import click
from rich.console import Console

from sweepstake_genie.config import load_config
from sweepstake_genie.database import Database
from sweepstake_genie.runner import run_discover, run_enter, run_enter_url, run_status

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
@click.option("--profile", default="profile.yaml", show_default=True,
              help="Path to your profile YAML file.")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable debug logging.")
@click.pass_context
def cli(ctx: click.Context, profile: str, verbose: bool) -> None:
    """Sweepstake Genie — automatically enter sweepstakes using your saved profile."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["profile_path"] = profile


# ── run ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Discover new sweepstakes and enter all pending ones."""
    profile_path = ctx.obj["profile_path"]
    try:
        config = load_config(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        sys.exit(1)

    db = Database(config.database)
    run_discover(db)
    run_enter(config, db)


# ── discover ──────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def discover(ctx: click.Context) -> None:
    """Scrape aggregator sites and save found sweepstakes to the database (no entry)."""
    profile_path = ctx.obj["profile_path"]
    try:
        config = load_config(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        sys.exit(1)

    db = Database(config.database)
    run_discover(db)


# ── enter-url ─────────────────────────────────────────────────────────────────

@cli.command("enter-url")
@click.argument("url")
@click.pass_context
def enter_url(ctx: click.Context, url: str) -> None:
    """Enter a single specific sweepstake URL."""
    profile_path = ctx.obj["profile_path"]
    try:
        config = load_config(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        sys.exit(1)

    db = Database(config.database)
    run_enter_url(url, config, db)


# ── status ────────────────────────────────────────────────────────────────────

@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show database statistics (total found, entered, skipped, CAPTCHA, errors)."""
    profile_path = ctx.obj["profile_path"]
    try:
        config = load_config(profile_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        sys.exit(1)

    db = Database(config.database)
    run_status(db)


# ── entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
