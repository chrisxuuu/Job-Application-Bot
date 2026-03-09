from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from job_bot.models.job import Job
from job_bot.models.application import Application


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_job(self, job: Job) -> Job:
        existing = (
            self.session.query(Job)
            .filter_by(source=job.source, external_id=job.external_id)
            .first()
        )
        if existing:
            existing.title = job.title
            existing.company = job.company
            existing.location = job.location
            existing.url = job.url
            existing.is_easy_apply = job.is_easy_apply
            if job.description:
                existing.description = job.description
            if job.salary_min is not None:
                existing.salary_min = job.salary_min
            if job.salary_max is not None:
                existing.salary_max = job.salary_max
            self.session.commit()
            return existing
        self.session.add(job)
        self.session.commit()
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.session.query(Job).filter_by(id=job_id).first()

    def get_pending_jobs(self) -> list[Job]:
        return self.session.query(Job).filter_by(status="new").all()

    def get_jobs_for_review(self) -> list[Job]:
        return self.session.query(Job).filter_by(status="manual_review").all()

    def update_job_status(self, job_id: str, status: str) -> None:
        job = self.get_job(job_id)
        if job:
            job.status = status
            self.session.commit()

    def update_job_evaluation(
        self,
        job_id: str,
        fit_score: int,
        fit_reasoning: str,
        missing_requirements: str,
        standout_qualifications: str,
        status: str,
    ) -> None:
        job = self.get_job(job_id)
        if job:
            job.fit_score = fit_score
            job.fit_reasoning = fit_reasoning
            job.missing_requirements = missing_requirements
            job.standout_qualifications = standout_qualifications
            job.status = status
            self.session.commit()

    def already_seen(self, source: str, external_id: str) -> bool:
        return (
            self.session.query(Job)
            .filter_by(source=source, external_id=external_id)
            .first()
            is not None
        )

    def get_daily_application_count(self) -> int:
        since = datetime.utcnow() - timedelta(days=1)
        return (
            self.session.query(Application)
            .filter(Application.applied_at >= since, Application.success == True)  # noqa: E712
            .count()
        )

    def save_application(self, application: Application) -> Application:
        self.session.add(application)
        self.session.commit()
        return application

    def get_application_history(self, days: int = 30) -> list[Application]:
        since = datetime.utcnow() - timedelta(days=days)
        return (
            self.session.query(Application)
            .filter(Application.applied_at >= since)
            .order_by(Application.applied_at.desc())
            .all()
        )

    def get_evaluated_jobs(self) -> list[Job]:
        """Return jobs scored and ready to apply, highest score first."""
        return (
            self.session.query(Job)
            .filter_by(status="evaluated")
            .order_by(Job.fit_score.desc())
            .all()
        )

    def get_all_jobs(self, limit: int = 100) -> list[Job]:
        return (
            self.session.query(Job)
            .order_by(Job.scraped_at.desc())
            .limit(limit)
            .all()
        )
