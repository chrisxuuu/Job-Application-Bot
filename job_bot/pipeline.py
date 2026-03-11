from __future__ import annotations

import asyncio
from typing import Literal

from rich.console import Console
from rich.table import Table

from config.settings import settings
from job_bot.models.application import Application
from job_bot.models.job import Job
from job_bot.scrapers.base import SearchCriteria
from job_bot.storage.database import init_db
from job_bot.storage.repository import JobRepository

console = Console()

Source = Literal["linkedin"]


async def run_pipeline(
    sources: list[Source] | None = None,
    dry_run: bool | None = None,
    easy_apply_only: bool = True,
    max_applications: int | None = None,
    skip_scrape: bool = False,
) -> dict:
    if dry_run is None:
        dry_run = settings.dry_run

    import yaml
    with open(settings.search_criteria_path) as f:
        raw = yaml.safe_load(f)
    enabled_sources: list[str] = raw.get("sources", ["linkedin"])
    sources = [s for s in (sources or enabled_sources) if s in enabled_sources]

    criteria = SearchCriteria.from_yaml(settings.search_criteria_path, easy_apply_only=easy_apply_only)

    session_factory = init_db(settings.db_path)
    session = session_factory()
    repo = JobRepository(session)

    summary = {
        "scraped": 0,
        "new": 0,
        "evaluated": 0,
        "applied": 0,
        "skipped": 0,
        "manual_review": 0,
        "errors": 0,
    }

    # Phase 1: Scrape (or load from DB)
    new_jobs: list[Job] = []

    if skip_scrape:
        console.rule("[bold blue]Skipping scrape — loading evaluated jobs from DB[/bold blue]")
        new_jobs = repo.get_evaluated_jobs()
        console.print(f"  Loaded [bold]{len(new_jobs)}[/bold] evaluated job(s) from database.\n")
    else:
        for source in sources:
            console.rule(f"[bold blue]Scraping: {source}[/bold blue]")
            try:
                from job_bot.scrapers.linkedin import LinkedInScraper
                scraper = LinkedInScraper(
                    email=settings.linkedin_email,
                    password=settings.linkedin_password,
                    headless=False,
                    request_delay_min=settings.request_delay_min,
                    request_delay_max=settings.request_delay_max,
                    max_jobs_per_session=settings.linkedin_max_jobs_per_session,
                )

                async with scraper:
                    await scraper.login()

                    async for job in scraper.search_jobs(criteria):
                        summary["scraped"] += 1
                        if repo.already_seen(job.source, job.external_id):
                            continue
                        job = await scraper.get_job_detail(job)
                        if job.status == "closed":
                            summary["skipped"] += 1
                            continue
                        saved = repo.upsert_job(job)
                        new_jobs.append(saved)
                        summary["new"] += 1
                        console.print(f"  [dim]+[/dim] {saved.title} @ {saved.company}")

            except Exception as e:
                console.print(f"[red]Scraping failed: {e}[/red]")
                summary["errors"] += 1

        console.print(
            f"\nScraped [bold]{summary['scraped']}[/bold] jobs, "
            f"[bold]{summary['new']}[/bold] new.\n"
        )

    # Phase 2: Evaluate
    from job_bot.ai.evaluator import evaluate_job

    console.rule("[bold blue]Evaluating jobs[/bold blue]")

    jobs_to_apply: list[tuple[Job, str]] = []

    if skip_scrape:
        # Jobs already evaluated — skip re-evaluation, just generate cover letters
        console.rule("[bold blue]Generating cover letters (skip-scrape mode)[/bold blue]")
        from job_bot.ai.cover_letter import generate_cover_letter
        from job_bot.ai.evaluator import EvaluationResult
        for job in new_jobs:
            console.print(f"  [cyan]{job.title}[/cyan] @ {job.company} — score {job.fit_score}")
            result = EvaluationResult(
                score=job.fit_score or 0,
                reasoning=job.fit_reasoning or "",
                missing_requirements=list(filter(None, (job.missing_requirements or "").splitlines())),
                standout_qualifications=list(filter(None, (job.standout_qualifications or "").splitlines())),
                recommendation="apply",
            )
            cover_letter = generate_cover_letter(job, result)
            jobs_to_apply.append((job, cover_letter))
            summary["evaluated"] += 1
    else:
        for job in new_jobs:
            console.print(f"  Evaluating: [cyan]{job.title}[/cyan] @ {job.company}")
            try:
                result = evaluate_job(job)
                summary["evaluated"] += 1

                if result.recommendation == "apply" and result.score >= settings.min_fit_score:
                    status = "evaluated"
                elif result.recommendation == "manual_review":
                    status = "manual_review"
                    summary["manual_review"] += 1
                else:
                    status = "skipped"
                    summary["skipped"] += 1

                repo.update_job_evaluation(
                    job_id=job.id,
                    fit_score=result.score,
                    fit_reasoning=result.reasoning,
                    missing_requirements="\n".join(result.missing_requirements),
                    standout_qualifications="\n".join(result.standout_qualifications),
                    status=status,
                )

                console.print(
                    f"    Score: [bold]{result.score}[/bold] → {result.recommendation} ({status})"
                )

                if status == "evaluated":
                    from job_bot.ai.cover_letter import generate_cover_letter
                    cover_letter = generate_cover_letter(job, result)
                    jobs_to_apply.append((job, cover_letter))

            except Exception as e:
                console.print(f"    [red]Evaluation error: {e}[/red]")
                summary["errors"] += 1

    # Phase 3: Apply
    if not jobs_to_apply:
        console.print("\nNo jobs to apply to.")
    else:
        console.rule("[bold blue]Applying to jobs[/bold blue]")
        daily_count = repo.get_daily_application_count()
        daily_cap = max_applications if max_applications is not None else settings.max_applications_per_day
        console.print(f"  Daily cap: [bold]{daily_cap}[/bold] (today so far: {daily_count})")

        if dry_run:
            for job, cover_letter in jobs_to_apply:
                console.print(
                    f"  [dim][DRY RUN][/dim] Would apply to: "
                    f"[cyan]{job.title}[/cyan] @ {job.company}"
                )
                summary["applied"] += 1
        else:
            # Open ONE browser session for all applications to avoid profile lock conflicts
            from job_bot.scrapers.linkedin import LinkedInScraper
            scraper = LinkedInScraper(
                email=settings.linkedin_email,
                password=settings.linkedin_password,
                headless=False,
            )
            async with scraper:
                await scraper.login()
                for job, cover_letter in jobs_to_apply:
                    if daily_count >= daily_cap:
                        console.print(
                            f"[yellow]Daily limit ({daily_cap}) reached.[/yellow]"
                        )
                        repo.update_job_status(job.id, "manual_review")
                        summary["manual_review"] += 1
                        continue

                    try:
                        success = await scraper.apply_easy(job, cover_letter)
                    except Exception as e:
                        console.print(f"  [red]Apply error: {e}[/red]")
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
                        summary["applied"] += 1
                        daily_count += 1
                    else:
                        repo.update_job_status(job.id, "manual_review")
                        summary["manual_review"] += 1

    _print_summary(summary, dry_run)
    session.close()
    return summary



def _print_summary(summary: dict, dry_run: bool) -> None:
    table = Table(title=f"Pipeline Summary {'[DRY RUN]' if dry_run else ''}")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")
    for key, value in summary.items():
        color = "green" if key in ("applied", "evaluated") else "yellow" if key == "manual_review" else ""
        table.add_row(
            key.replace("_", " ").title(),
            f"[{color}]{value}[/{color}]" if color else str(value),
        )
    console.print(table)
