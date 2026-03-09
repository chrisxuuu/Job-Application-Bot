#!/usr/bin/env python3
"""
Job Bot CLI — automated job scraping and application tool.

Usage examples:
  python cli.py run --dry-run               # Scrape & evaluate, no applications
  python cli.py run                         # Live run (Easy Apply only)
  python cli.py run --non-easy-apply        # Live run (non-Easy Apply jobs only)
  python cli.py run --source linkedin       # Single source
  python cli.py login linkedin          # Save browser session
  python cli.py report                  # Show application history
  python cli.py review                  # List jobs flagged for manual review
  python cli.py clear                   # Remove cached jobs (new/evaluated/skipped)
  python cli.py clear --all             # Remove all jobs + application history
  python cli.py clear --status applied  # Remove jobs with a specific status
  python cli.py schedule                # Start daily scheduler
"""

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Automated job bot", no_args_is_help=True)
console = Console()


@app.command()
def run(
    dry_run: bool = typer.Option(None, "--dry-run/--live", help="Override DRY_RUN from .env"),
    source: Optional[str] = typer.Option(
        None, "--source", "-s", help="Source to run (currently only: linkedin)"
    ),
    non_easy_apply: bool = typer.Option(
        False, "--non-easy-apply", help="Search non-Easy Apply jobs only (omits f_AL=true filter)"
    ),
    max_applications: Optional[int] = typer.Option(
        None, "--max-applications", "-m", help="Override max applications per day (for testing)"
    ),
    skip_scrape: bool = typer.Option(
        False, "--skip-scrape", help="Skip scraping; apply to already-evaluated jobs in the DB"
    ),
) -> None:
    """Scrape jobs, evaluate with AI, and apply."""
    from job_bot.pipeline import run_pipeline

    sources = [source] if source else None
    asyncio.run(
        run_pipeline(
            sources=sources,
            dry_run=dry_run,
            easy_apply_only=not non_easy_apply,
            max_applications=max_applications,
            skip_scrape=skip_scrape,
        )
    )


@app.command()
def login() -> None:
    """Open a browser and save the LinkedIn login session."""

    async def _login():
        from config.settings import settings
        from job_bot.scrapers.linkedin import LinkedInScraper
        scraper = LinkedInScraper(
            email=settings.linkedin_email,
            password=settings.linkedin_password,
            headless=False,
        )
        async with scraper:
            await scraper.login()
            console.print("[green]LinkedIn session saved. You can close the browser now.[/green]")
            input("Press ENTER to close the browser...")

    asyncio.run(_login())


@app.command()
def report(
    days: int = typer.Option(30, "--days", "-d", help="Number of days of history to show"),
) -> None:
    """Show application history."""
    from config.settings import settings
    from job_bot.storage.database import init_db
    from job_bot.storage.repository import JobRepository

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    applications = repo.get_application_history(days=days)

    if not applications:
        console.print(f"No applications in the last {days} days.")
        session.close()
        return

    table = Table(title=f"Application History (last {days} days)")
    table.add_column("Date", style="dim")
    table.add_column("Job ID")
    table.add_column("Method")
    table.add_column("Status")

    for app in applications:
        status_color = "green" if app.success else "red"
        table.add_row(
            app.applied_at.strftime("%Y-%m-%d %H:%M"),
            app.job_id[:8] + "...",
            app.method,
            f"[{status_color}]{'✓' if app.success else '✗'}[/{status_color}]",
        )

    console.print(table)
    console.print(
        f"\nTotal: {len(applications)} applications, "
        f"{sum(1 for a in applications if a.success)} successful"
    )
    session.close()


@app.command()
def review() -> None:
    """List jobs flagged for manual review."""
    from config.settings import settings
    from job_bot.storage.database import init_db
    from job_bot.storage.repository import JobRepository

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    jobs = repo.get_jobs_for_review()

    if not jobs:
        console.print("No jobs flagged for manual review.")
        session.close()
        return

    table = Table(title="Jobs for Manual Review")
    table.add_column("#", style="dim", width=4)
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Source")
    table.add_column("Score")
    table.add_column("URL")

    for i, job in enumerate(jobs, 1):
        score_str = str(job.fit_score) if job.fit_score is not None else "—"
        table.add_row(
            str(i),
            job.title,
            job.company,
            job.source,
            score_str,
            job.url,
        )

    console.print(table)
    session.close()


@app.command()
def jobs(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of jobs to show"),
) -> None:
    """Show recently scraped jobs."""
    from config.settings import settings
    from job_bot.storage.database import init_db
    from job_bot.storage.repository import JobRepository

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    all_jobs = repo.get_all_jobs(limit=limit)

    if not all_jobs:
        console.print("No jobs in database yet. Run: python cli.py run --dry-run")
        session.close()
        return

    table = Table(title=f"Recent Jobs (last {limit})")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Source")
    table.add_column("Score")
    table.add_column("Status")

    status_colors = {
        "new": "dim",
        "evaluated": "blue",
        "applied": "green",
        "skipped": "dim",
        "manual_review": "yellow",
    }

    for job in all_jobs:
        color = status_colors.get(job.status, "white")
        score_str = str(job.fit_score) if job.fit_score is not None else "—"
        table.add_row(
            job.title[:50],
            job.company[:30],
            job.source,
            score_str,
            f"[{color}]{job.status}[/{color}]",
        )

    console.print(table)
    session.close()


@app.command()
def apply(
    dry_run: bool = typer.Option(False, "--dry-run/--live", help="Preview without submitting"),
    max_applications: Optional[int] = typer.Option(
        None, "--max-applications", "-m", help="Override max applications per day (for testing)"
    ),
) -> None:
    """Apply to all evaluated jobs already in the database."""
    import asyncio
    from config.settings import settings
    from job_bot.storage.database import init_db
    from job_bot.storage.repository import JobRepository
    from job_bot.models.application import Application
    from job_bot.ai.cover_letter import generate_cover_letter
    from job_bot.ai.evaluator import EvaluationResult
    from job_bot.scrapers.linkedin import LinkedInScraper

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    jobs = repo.get_evaluated_jobs()
    if not jobs:
        console.print("[yellow]No evaluated jobs in database. Run: python cli.py run[/yellow]")
        session.close()
        return

    console.print(f"Found [bold]{len(jobs)}[/bold] evaluated jobs to apply to.\n")

    async def _run():
        daily_count = repo.get_daily_application_count()
        daily_cap = max_applications if max_applications is not None else settings.max_applications_per_day
        scraper = LinkedInScraper(
            email=settings.linkedin_email,
            password=settings.linkedin_password,
            headless=False,
        )
        async with scraper:
            await scraper.login()
            for job in jobs:
                if daily_count >= daily_cap:
                    console.print(f"[yellow]Daily limit ({daily_cap}) reached.[/yellow]")
                    break

                # Rebuild EvaluationResult from stored data
                result = EvaluationResult(
                    score=job.fit_score or 0,
                    reasoning=job.fit_reasoning or "",
                    missing_requirements=list(filter(None, (job.missing_requirements or "").splitlines())),
                    standout_qualifications=list(filter(None, (job.standout_qualifications or "").splitlines())),
                    recommendation="apply",
                )

                console.print(f"  [{job.fit_score}] [cyan]{job.title}[/cyan] @ {job.company}")
                cover_letter = generate_cover_letter(job, result)

                if dry_run:
                    console.print(f"  [dim][DRY RUN] Would apply[/dim]")
                    continue

                try:
                    success = await scraper.apply_easy(job, cover_letter)
                except Exception as e:
                    console.print(f"  [red]Error: {e}[/red]")
                    success = False

                application = Application(
                    job_id=job.id,
                    cover_letter=cover_letter,
                    method="easy_apply_or_external",
                    success=success,
                    error_message=None if success else "Apply flow did not complete",
                )
                repo.save_application(application)

                if success:
                    repo.update_job_status(job.id, "applied")
                    daily_count += 1
                    console.print(f"  [green]✓ Applied[/green]")
                else:
                    repo.update_job_status(job.id, "manual_review")
                    console.print(f"  [yellow]✗ Failed — moved to manual review[/yellow]")

    asyncio.run(_run())
    session.close()


@app.command()
def clear(
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Only remove jobs with this status (new, evaluated, skipped, manual_review, applied)",
    ),
    all_data: bool = typer.Option(
        False, "--all", help="Remove ALL jobs and application history"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Remove cached/scraped jobs from the database."""
    from config.settings import settings
    from job_bot.storage.database import init_db
    from job_bot.storage.repository import JobRepository

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    # Determine scope
    if all_data:
        target_statuses = None  # all jobs
        scope_desc = "ALL jobs and application history"
    elif status:
        target_statuses = [status]
        scope_desc = f"jobs with status '{status}'"
    else:
        # Default: remove unapplied cache (new + evaluated + skipped)
        target_statuses = ["new", "evaluated", "skipped"]
        scope_desc = "cached jobs (new / evaluated / skipped)"

    # Preview count
    from job_bot.models.job import Job as _Job
    preview_q = session.query(_Job)
    if target_statuses:
        preview_q = preview_q.filter(_Job.status.in_(target_statuses))
    job_count = preview_q.count()

    if job_count == 0 and not all_data:
        console.print(f"[dim]No {scope_desc} found — nothing to clear.[/dim]")
        session.close()
        return

    if not yes:
        console.print(f"[yellow]This will delete {job_count} {scope_desc}.[/yellow]")
        if all_data:
            console.print("[yellow]Application history will also be deleted.[/yellow]")
        typer.confirm("Continue?", abort=True)

    deleted_jobs = repo.clear_jobs(statuses=target_statuses)
    console.print(f"[green]Deleted {deleted_jobs} job(s).[/green]")

    if all_data:
        deleted_apps = repo.clear_applications()
        console.print(f"[green]Deleted {deleted_apps} application record(s).[/green]")

    session.close()


@app.command()
def schedule(
    hour: int = typer.Option(8, "--hour", help="Hour of day to run (24h format)"),
    minute: int = typer.Option(0, "--minute", help="Minute of hour to run"),
) -> None:
    """Start the scheduler to run the pipeline daily."""
    from scheduler import start_scheduler
    start_scheduler(hour=hour, minute=minute)


if __name__ == "__main__":
    app()
