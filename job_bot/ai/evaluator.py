from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console

from job_bot.ai.ollama_client import ollama_chat
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


def _repair_and_parse(raw: str) -> dict:
    """
    Best-effort JSON repair for Qwen3.5 output, which sometimes produces:
    - Markdown fences (```json ... ```)
    - Single quotes instead of double quotes
    - Trailing commas before } or ]
    - Extra text before/after the JSON object
    Raises json.JSONDecodeError if all attempts fail.
    """
    import re

    s = raw.strip()

    # Strip <think>...</think> blocks (Qwen3.5 can leak these even with think:false)
    s = re.sub(r"<think>[\s\S]*?</think>", "", s, flags=re.IGNORECASE).strip()

    # Strip JavaScript-style // line comments (model sometimes adds these)
    s = re.sub(r"//[^\n]*", "", s)

    # Strip markdown fences
    if s.startswith("```"):
        parts = s.split("```")
        # Take the first fenced block (index 1)
        s = parts[1] if len(parts) > 1 else s
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()

    # Try as-is first
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Extract the first {...} block (handles leading/trailing prose)
    match = re.search(r"\{[\s\S]*\}", s)
    if match:
        s = match.group(0)

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Remove trailing commas before } or ]
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Replace single-quoted strings with double-quoted (simple heuristic)
    s = re.sub(r"(?<![\\])'", '"', s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Last resort: remove trailing commas again after quote fix
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return json.loads(s)  # raise if still broken


def _parse_json_result(raw: str) -> EvaluationResult:
    data = _repair_and_parse(raw)
    return EvaluationResult(
        score=int(data.get("score", 0)),
        reasoning=data.get("reasoning", ""),
        missing_requirements=data.get("missing_requirements", []),
        standout_qualifications=data.get("standout_qualifications", []),
        recommendation=data.get("recommendation", "skip"),
    )


def evaluate_job(job: Job) -> EvaluationResult:
    """Score how well the candidate fits this job. Uses Qwen3 (Ollama) as primary, Claude as fallback."""
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
    user_msg = f"Please evaluate this job opportunity and return JSON only:\n{job_content}"

    # --- Primary: Qwen3 via Ollama ---
    try:
        raw = ollama_chat(
            system=system_content,
            user=user_msg,
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            max_tokens=1024,
        )
        return _parse_json_result(raw)
    except Exception as e:
        console.print(f"  [yellow]Ollama evaluator failed ({e}), falling back to Claude...[/yellow]")

    # --- Fallback: Claude ---
    try:
        from job_bot.ai.client import get_client
        client = get_client()
        with client.messages.stream(
            model=settings.evaluator_model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": system_content, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            response = stream.get_final_message()
        raw = next((b.text for b in response.content if b.type == "text"), "")
        return _parse_json_result(raw)
    except json.JSONDecodeError as e:
        console.print(f"  [red]Evaluator JSON parse error: {e}[/red]")
    except Exception as e:
        console.print(f"  [red]Claude evaluator error: {e}[/red]")

    return EvaluationResult(
        score=0,
        reasoning="All evaluators failed.",
        missing_requirements=[],
        standout_qualifications=[],
        recommendation="manual_review",
    )
