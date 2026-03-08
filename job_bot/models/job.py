from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Integer, Float, DateTime, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(50), nullable=False)  # "linkedin" | "ziprecruiter"
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    company: Mapped[str] = mapped_column(String(300), nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    salary_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    salary_max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    fit_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fit_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    missing_requirements: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    standout_qualifications: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "new" | "evaluated" | "skipped" | "applied" | "manual_review"
    status: Mapped[str] = mapped_column(String(50), default="new")
    is_easy_apply: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Job {self.source}:{self.external_id} '{self.title}' @ {self.company} [{self.status}]>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "url": self.url,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "fit_score": self.fit_score,
            "status": self.status,
        }
