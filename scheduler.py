from __future__ import annotations

import asyncio

from apscheduler.schedulers.blocking import BlockingScheduler
from rich.console import Console

console = Console()


def _run_pipeline_sync() -> None:
    from job_bot.pipeline import run_pipeline
    asyncio.run(run_pipeline())


def start_scheduler(hour: int = 8, minute: int = 0) -> None:
    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_pipeline_sync,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="daily_job_run",
    )
    console.print(
        f"[green]Scheduler started. Pipeline will run daily at {hour:02d}:{minute:02d}.[/green]"
    )
    console.print("Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Scheduler stopped.[/yellow]")
