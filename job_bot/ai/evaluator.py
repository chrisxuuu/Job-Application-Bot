from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from job_bot.ai.client import get_client
from job_bot.models.job import Job

console = Console()


@dataclass
class EvaluationResult:
    score: int  # 0-100
    reasoning: str
    missing_requirements: list[str]
    standout_qualifications: list[str]
    recommendation: Literal["apply", "skip", "manual_review"]


def _load_resume_and_profile() -> tuple[str, str]:
    from config.settings import settings
    resume_path = Path(settings.resume_path)
    profile_path = Path(settings.profile_path)

    resume = resume_path.read_text() if resume_path.exists() else "[Resume not found]"
    profile = profile_path.read_text() if profile_path.exists() else "[Profile not found]"
    return resume, profile


SYSTEM_PROMPT_TEMPLATE = """\
You are an expert career advisor and job-fit evaluator. You will receive a job description
and a candidate's resume and profile. Your job is to evaluate how well the candidate fits
the role and return a structured JSON assessment.

Candidate Resume:
---
{resume}
---

Candidate Profile:
---
{profile}
---

Always respond with valid JSON matching this exact schema:
{{
  "score": <integer 0-100>,
  "reasoning": "<2-3 sentence summary of fit>",
  "missing_requirements": ["<requirement 1>", ...],
  "standout_qualifications": ["<qualification 1>", ...],
  "recommendation": "<apply|skip|manual_review>"
}}

Scoring guide:
- 85-100: Excellent match, apply immediately
- 70-84: Good match, worth applying
- 50-69: Partial match, use manual_review
- 0-49: Poor match, skip

Use "manual_review" when the job requires skills or experience that the candidate partially
has but might be able to justify in an interview.
"""


def evaluate_job(job: Job) -> EvaluationResult:
    """
    Ask Claude to score how well the candidate fits this job.
    Uses prompt caching on the system message (resume + profile) to reduce costs.
    """
    client = get_client()
    from config.settings import settings

    resume, profile = _load_resume_and_profile()

    system_content = SYSTEM_PROMPT_TEMPLATE.format(resume=resume, profile=profile)

    job_content = f"""
Job Title: {job.title}
Company: {job.company}
Location: {job.location or 'Not specified'}
URL: {job.url}

Job Description:
{job.description or 'No description available.'}
"""

    try:
        with client.messages.stream(
            model=settings.evaluator_model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=[
                {
                    "type": "text",
                    "text": system_content,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Please evaluate this job opportunity and return JSON only:\n{job_content}"
                    ),
                }
            ],
        ) as stream:
            response = stream.get_final_message()

        raw = ""
        for block in response.content:
            if block.type == "text":
                raw = block.text
                break

        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        data = json.loads(raw)
        return EvaluationResult(
            score=int(data.get("score", 0)),
            reasoning=data.get("reasoning", ""),
            missing_requirements=data.get("missing_requirements", []),
            standout_qualifications=data.get("standout_qualifications", []),
            recommendation=data.get("recommendation", "skip"),
        )

    except json.JSONDecodeError as e:
        console.print(f"  [red]Evaluator JSON parse error: {e}[/red]")
        return EvaluationResult(
            score=0,
            reasoning="Failed to parse evaluation response.",
            missing_requirements=[],
            standout_qualifications=[],
            recommendation="manual_review",
        )
    except Exception as e:
        console.print(f"  [red]Evaluator error: {e}[/red]")
        return EvaluationResult(
            score=0,
            reasoning=f"Evaluation error: {e}",
            missing_requirements=[],
            standout_qualifications=[],
            recommendation="manual_review",
        )
