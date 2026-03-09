from __future__ import annotations

from pathlib import Path

from rich.console import Console

from job_bot.ai.evaluator import EvaluationResult
from job_bot.ai.ollama_client import ollama_chat
from job_bot.models.job import Job

console = Console()

COVER_LETTER_SYSTEM = """\
You are an expert career coach who writes highly personalized, compelling cover letters.
Your letters are concise (3 paragraphs, ~250 words), specific, and never generic.

Rules:
- Open with a specific detail from the job description — never with "I am writing to express..."
- In paragraph 2, map the candidate's top 2-3 relevant experiences to the role's stated requirements
- Close with a concrete next step
- Never use clichés like "passionate", "dynamic", "synergy"
- Write in first person as the candidate
- Do not include placeholders like [Your Name] — write the full letter body only
"""


def generate_cover_letter(job: Job, evaluation: EvaluationResult) -> str:
    """Generate a tailored cover letter. Uses Qwen3 (Ollama) as primary, Claude as fallback."""
    from config.settings import settings

    resume_path = Path(settings.resume_path)
    resume = resume_path.read_text() if resume_path.exists() else "[Resume not found]"

    prompt = f"""Write a cover letter for this position:

Job Title: {job.title}
Company: {job.company}
Location: {job.location or 'Not specified'}

Job Description:
{(job.description or 'No description available.')[:3000]}

Candidate's Key Strengths for This Role:
{chr(10).join(f"- {q}" for q in evaluation.standout_qualifications)}

Candidate Resume:
{resume[:4000]}

Write the cover letter body now (3 paragraphs, ~250 words):"""

    # --- Primary: Qwen3 via Ollama ---
    try:
        return ollama_chat(
            system=COVER_LETTER_SYSTEM,
            user=prompt,
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            max_tokens=1024,
        ).strip()
    except Exception as e:
        console.print(f"  [yellow]Ollama cover letter failed ({e}), falling back to Claude...[/yellow]")

    # --- Fallback: Claude ---
    try:
        from job_bot.ai.client import get_client
        client = get_client()
        with client.messages.stream(
            model=settings.cover_letter_model,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=COVER_LETTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = stream.get_final_message()
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
    except Exception as e:
        console.print(f"  [red]Claude cover letter error: {e}[/red]")

    return f"I am excited to apply for the {job.title} position at {job.company}."
