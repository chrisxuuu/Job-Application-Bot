from __future__ import annotations

import json
import os

from rich.console import Console

from job_bot.models.job import Job

console = Console()

FORM_FILLER_SYSTEM = """\
You are an AI assistant that fills out job application forms on behalf of a candidate.
You will receive a list of form fields (label, type, name/id, options) and the candidate's
resume and profile. Return a JSON object mapping each field identifier to the value to fill.

Rules:
- Use ONLY real information from the candidate's resume/profile — never fabricate details
- For work authorization / eligibility questions, answer "Yes"
- For salary, use the value from the profile if present, otherwise omit
- For dropdowns (type=select), pick the closest matching option from the provided list
- For file uploads, return null — the bot will skip those
- For fields you cannot determine, return null
- Return ONLY valid JSON — no explanation, no markdown fences

Response format:
{
  "<field_id_or_name>": "<value>",
  ...
}
"""


async def extract_form_fields(page) -> list[dict]:
    """Extract all visible, fillable form fields from the page."""
    fields = []

    elements = await page.query_selector_all(
        'input:not([type="hidden"]):not([type="submit"])'
        ':not([type="button"]):not([type="file"]):not([type="reset"]), '
        "textarea, select"
    )

    for el in elements:
        try:
            visible = await el.is_visible()
            if not visible:
                continue

            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            field: dict = {
                "tag": tag,
                "type": await el.get_attribute("type") or "text",
                "name": await el.get_attribute("name") or "",
                "id": await el.get_attribute("id") or "",
                "placeholder": await el.get_attribute("placeholder") or "",
                "aria_label": await el.get_attribute("aria-label") or "",
                "label": "",
                "options": [],
            }

            # Resolve label text via for= attribute
            el_id = field["id"]
            if el_id:
                label_el = await page.query_selector(f'label[for="{el_id}"]')
                if label_el:
                    field["label"] = (await label_el.inner_text()).strip()

            # Fallback: nearest ancestor label
            if not field["label"]:
                try:
                    field["label"] = await el.evaluate(
                        """el => {
                            let p = el.closest('label') || el.parentElement;
                            while (p && p.tagName !== 'LABEL' && p.tagName !== 'FORM') p = p.parentElement;
                            return p && p.tagName === 'LABEL' ? p.innerText.trim() : '';
                        }"""
                    )
                except Exception:
                    pass

            # Options for <select>
            if tag == "select":
                opts = await el.query_selector_all("option")
                field["options"] = [(await o.inner_text()).strip() for o in opts]

            # Skip fields with no identifier at all
            if not field["id"] and not field["name"]:
                continue

            fields.append(field)
        except Exception:
            continue

    return fields


async def fill_form_with_ai(page, fields: list[dict], job: Job, cover_letter: str) -> dict:
    """Ask Qwen3 to decide what to fill in each field. Returns {id_or_name: value}."""
    from config.settings import settings
    from job_bot.ai.ollama_client import ollama_chat
    from pathlib import Path

    resume = Path(settings.resume_path).read_text() if Path(settings.resume_path).exists() else ""
    profile = Path(settings.profile_path).read_text() if Path(settings.profile_path).exists() else ""

    fields_summary = json.dumps(fields, indent=2)
    user_msg = f"""Job: {job.title} at {job.company}

Form fields to fill:
{fields_summary}

Candidate Resume:
{resume[:3000]}

Candidate Profile:
{profile[:1500]}

Cover Letter (use if there is a cover letter field):
{cover_letter[:2000]}

Return JSON mapping field id or name to value. Use null for fields you should skip."""

    raw = ollama_chat(
        system=FORM_FILLER_SYSTEM,
        user=user_msg,
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        max_tokens=1024,
    )

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


async def apply_on_external_site(page, job: Job, cover_letter: str) -> bool:
    """
    AI-driven form filler for external employer ATS pages.
    Extracts form fields, asks Qwen3 what to fill, submits.
    """
    import asyncio
    from config.settings import settings

    console.print(f"  [cyan]🌐 Attempting external apply at {page.url}[/cyan]")

    try:
        # Wait for form to load
        await asyncio.sleep(3)

        fields = await extract_form_fields(page)
        if not fields:
            console.print("  [yellow]No form fields found on external page[/yellow]")
            return False

        console.print(f"  [dim]Found {len(fields)} form fields — asking Qwen3 to fill...[/dim]")
        mapping = await fill_form_with_ai(page, fields, job, cover_letter)

        filled = 0
        for field in fields:
            key = field["id"] or field["name"]
            value = mapping.get(key)
            if not value:
                continue

            try:
                selector = f'#{field["id"]}' if field["id"] else f'[name="{field["name"]}"]'
                el = await page.query_selector(selector)
                if not el:
                    continue

                if field["tag"] == "select":
                    await el.select_option(label=str(value))
                elif field["type"] == "radio":
                    await page.check(f'{selector}[value="{value}"]')
                elif field["type"] == "checkbox":
                    if str(value).lower() in ("yes", "true", "1"):
                        await el.check()
                else:
                    await el.fill(str(value))

                filled += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                console.print(f"  [dim]Could not fill '{key}': {e}[/dim]")
                continue

        console.print(f"  [dim]Filled {filled}/{len(fields)} fields[/dim]")

        # Screenshot before submit
        os.makedirs(settings.screenshots_dir, exist_ok=True)
        screenshot_path = f"{settings.screenshots_dir}/{job.source}_{job.external_id}_external_pre_submit.png"
        await page.screenshot(path=screenshot_path)
        console.print(f"  [cyan]Screenshot saved: {screenshot_path}[/cyan]")

        # Find and click submit button
        submit = await page.query_selector(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Submit'), button:has-text('Apply'), "
            "button:has-text('Send Application')"
        )
        if not submit:
            console.print("  [yellow]No submit button found on external page[/yellow]")
            return False

        await submit.click()
        await asyncio.sleep(3)
        console.print(f"  [green]Submitted external application for {job.title} @ {job.company}[/green]")
        return True

    except Exception as e:
        console.print(f"  [red]External apply error: {e}[/red]")
        return False
