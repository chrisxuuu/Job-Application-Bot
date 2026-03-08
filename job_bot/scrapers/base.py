from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class SearchCriteria:
    job_titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    salary_min: int = 0
    keywords_required: list[str] = field(default_factory=list)
    keywords_excluded: list[str] = field(default_factory=list)
    experience_years_max: int = 0

    @classmethod
    def from_yaml(cls, path: str) -> "SearchCriteria":
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(
            job_titles=data.get("job_titles", []),
            locations=data.get("locations", []),
            salary_min=data.get("salary_min", 0),
            keywords_required=data.get("keywords_required", []),
            keywords_excluded=data.get("keywords_excluded", []),
            experience_years_max=data.get("experience_years_max", 0),
        )


class BaseScraper(ABC):
    @abstractmethod
    async def login(self) -> None:
        """Log into the job site and persist the session."""
        ...

    @abstractmethod
    async def search_jobs(self, criteria: SearchCriteria) -> AsyncIterator["job_bot.models.job.Job"]:  # type: ignore[name-defined]
        """Yield Job objects (without full description) from search results."""
        ...

    @abstractmethod
    async def get_job_detail(self, job: "job_bot.models.job.Job") -> "job_bot.models.job.Job":  # type: ignore[name-defined]
        """Fetch the full job description and fill it into the job object."""
        ...

    @abstractmethod
    async def apply_easy(self, job: "job_bot.models.job.Job", cover_letter: str) -> bool:
        """Attempt automated application. Returns True on success."""
        ...
