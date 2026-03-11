from __future__ import annotations

import asyncio
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

Name handling:
- "first_name" / "first name" / "fname" fields → use profile first_name (e.g. "Chen Yang")
- "last_name" / "last name" / "lname" / "surname" fields → use profile last_name (e.g. "Xu")
- "full_name" / "name" fields → use the full name (first_name + " " + last_name)

Phone handling:
- For phone number fields, use phone_number_only (digits only, no dashes, no country code)
- If the field already shows a country code (+1), provide only the local number digits

Address handling:
- For US jobs: address = "8324 Regents Rd, San Diego, CA 92122", city = "San Diego", state = "CA", zip = "92122"
- For Canadian jobs: address = "5626 Bell Harbour Dr, Mississauga, ON L5M 5J3", city = "Mississauga", province = "ON", postal = "L5M 5J3"
- Infer US vs Canada from the job company/location context; default to US address if unclear

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

            # Skip fields with no identifier at all (id, name, aria-label, or placeholder)
            if not field["id"] and not field["name"] and not field["aria_label"] and not field["placeholder"]:
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

    from job_bot.ai.evaluator import _repair_and_parse

    raw = ollama_chat(
        system=FORM_FILLER_SYSTEM,
        user=user_msg,
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        max_tokens=2048,
    )

    try:
        return _repair_and_parse(raw)
    except Exception:
        # If JSON is still broken, return empty dict — fields will be skipped
        console.print("  [yellow]Form filler JSON parse failed — skipping AI fill[/yellow]")
        return {}


async def _fill_page_fields(page, fields: list[dict], job: Job, cover_letter: str) -> int:
    """Fill fields on the current page using AI. Returns number of fields filled."""
    if not fields:
        return 0
    mapping = await fill_form_with_ai(page, fields, job, cover_letter)
    filled = 0
    for field in fields:
        key = field["id"] or field["name"] or field["aria_label"] or field["placeholder"]
        value = mapping.get(key)
        if not value:
            continue
        try:
            # Build selector chain: id > name > aria-label > placeholder
            el = None
            if field["id"]:
                el = await page.query_selector(f'#{field["id"]}')
            if not el and field["name"]:
                el = await page.query_selector(f'[name="{field["name"]}"]')
            if not el and field["aria_label"]:
                el = await page.query_selector(f'[aria-label="{field["aria_label"]}"]')
            if not el and field["placeholder"]:
                el = await page.query_selector(f'[placeholder="{field["placeholder"]}"]')
            if not el:
                continue
            selector = f'#{field["id"]}' if field["id"] else f'[name="{field["name"]}"]'
            if field["tag"] == "select":
                try:
                    await el.select_option(label=str(value))
                except Exception:
                    await el.select_option(value=str(value))
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
    return filled


async def _handle_account_gate(page, email: str, password: str) -> bool:
    """
    Detect a login or sign-up gate and fill credentials.
    Returns True if an account form was found and handled.
    """
    import asyncio

    has_email_input = await page.query_selector(
        'input[type="email"], input[name*="email" i], input[id*="email" i]'
    )
    has_password_input = await page.query_selector('input[type="password"]')

    if not has_email_input:
        return False

    page_text = (await page.inner_text("body")).lower()
    is_auth_page = any(kw in page_text for kw in (
        "sign in", "log in", "login", "sign up", "create account",
        "register", "welcome back", "create your account",
    ))
    if not is_auth_page and not has_password_input:
        return False

    is_signup = any(kw in page_text for kw in ("sign up", "create account", "register", "create your account"))
    is_login = any(kw in page_text for kw in ("sign in", "log in", "login", "welcome back"))
    console.print(f"  [dim]Account gate detected ({'sign-up' if is_signup and not is_login else 'sign-in'}) — filling credentials...[/dim]")

    # If on signup page, try switching to sign-in first (we may already have an account)
    if is_signup and not is_login:
        for sel in [
            "a:has-text('Sign in')", "a:has-text('Log in')", "a:has-text('Sign In')",
            "a:has-text('Already have an account')", "a:has-text('already have an account')",
            "button:has-text('Sign in')",
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

    # Fill email
    try:
        email_el = await page.query_selector(
            'input[type="email"], input[name*="email" i], input[id*="email" i]'
        )
        if email_el and await email_el.is_visible():
            await email_el.click(click_count=3)
            await email_el.fill(email)
            await asyncio.sleep(0.3)
    except Exception:
        pass

    # Some flows (Google-style) show password only after "Continue"
    pw_el = await page.query_selector('input[type="password"]')
    if not pw_el or not await pw_el.is_visible():
        for cont_sel in [
            "button:has-text('Continue')", "button:has-text('Next')",
            "button[type='submit']", "input[type='submit']",
        ]:
            try:
                el = await page.query_selector(cont_sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

    # Fill password
    try:
        pw_el = await page.query_selector('input[type="password"]')
        if pw_el and await pw_el.is_visible():
            await pw_el.click(click_count=3)
            await pw_el.fill(password)
            await asyncio.sleep(0.3)
    except Exception:
        pass

    # Submit
    for submit_sel in [
        "button:has-text('Sign in')", "button:has-text('Log in')",
        "button:has-text('Sign In')", "button:has-text('Log In')",
        "button:has-text('Continue')", "button[type='submit']",
        "input[type='submit']",
    ]:
        try:
            el = await page.query_selector(submit_sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(3)
                break
        except Exception:
            continue

    # If we landed on a signup form (new account created), fill name fields too
    page_text_after = (await page.inner_text("body")).lower()
    if any(kw in page_text_after for kw in ("first name", "last name", "create your profile")):
        import yaml
        from config.settings import settings
        from pathlib import Path
        try:
            profile = yaml.safe_load(Path(settings.profile_path).read_text()) or {}
            first = profile.get("first_name", "")
            last = profile.get("last_name", "")
            for fname_sel in ['input[name*="first" i]', 'input[id*="first" i]', 'input[placeholder*="first" i]']:
                el = await page.query_selector(fname_sel)
                if el and await el.is_visible() and first:
                    await el.fill(first)
                    break
            for lname_sel in ['input[name*="last" i]', 'input[id*="last" i]', 'input[placeholder*="last" i]']:
                el = await page.query_selector(lname_sel)
                if el and await el.is_visible() and last:
                    await el.fill(last)
                    break
            # Submit signup form
            for s in ["button[type='submit']", "button:has-text('Continue')", "button:has-text('Create')"]:
                el = await page.query_selector(s)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(3)
                    break
        except Exception:
            pass

    return True


async def _ai_decide_action(page, job: Job, step_num: int, settings) -> dict:
    """
    Take a screenshot + simplified HTML, ask the vision model to decide the next action.
    Returns a dict: {action, button_text, reasoning}
    Actions: fill_form | click_button | dismiss_cookie | check_agreements |
             handle_account | done | stuck
    """
    import base64
    from job_bot.ai.ollama_client import ollama_chat_vision

    screenshot_bytes = await page.screenshot()
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

    try:
        simplified = await page.evaluate("""() => {
            const tags = 'input,select,textarea,button,label,h1,h2,h3,a[href],\
[role="dialog"],.modal,[class*="cookie"],[class*="consent"],[class*="agreement"],\
[class*="terms"],[class*="error"],[class*="alert"]';
            return Array.from(document.querySelectorAll(tags))
                .slice(0, 60)
                .map(el => el.outerHTML.slice(0, 300))
                .join('\\n');
        }""")
    except Exception:
        simplified = ""

    system = (
        "You are controlling a browser to complete an external job application.\n"
        "Look at the screenshot and page elements, then decide the single next action.\n"
        "Return ONLY valid JSON — no markdown, no explanation:\n"
        '{"action": "<type>", "button_text": "<exact visible text>", "reasoning": "<brief>"}\n\n'
        "Action types:\n"
        "- fill_form: Unfilled application form fields are visible\n"
        "- click_button: Click a specific button (provide exact button_text)\n"
        "- dismiss_cookie: A cookie/privacy banner is blocking interaction\n"
        "- check_agreements: Unchecked agreement/terms/consent checkboxes present\n"
        "- handle_account: Login or sign-up form is blocking (email+password fields visible)\n"
        "- done: Confirmation/thank-you page — application submitted\n"
        "- stuck: Cannot determine what to do\n\n"
        "Priority order: dismiss_cookie > handle_account > check_agreements > fill_form > click_button > done > stuck"
    )
    user = (
        f"Step {step_num}. Applying to: {job.title} at {job.company}\n"
        f"URL: {page.url}\n\n"
        f"Page elements:\n{simplified[:2500]}\n\n"
        "What is the single next action? Return JSON only."
    )

    def _parse_json_action(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())

    # Try vision model first
    try:
        raw = ollama_chat_vision(
            system=system,
            user=user,
            image_b64=screenshot_b64,
            model=settings.ollama_vision_model,
            base_url=settings.ollama_base_url,
            max_tokens=300,
        )
        return _parse_json_action(raw)
    except Exception as vision_err:
        console.print(f"  [dim]Vision model failed ({vision_err}) — falling back to text-only[/dim]")

    # Fallback: text-only model (no screenshot)
    try:
        from job_bot.ai.ollama_client import ollama_chat
        raw = ollama_chat(
            system=system,
            user=user,
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            max_tokens=300,
        )
        return _parse_json_action(raw)
    except Exception as text_err:
        return {"action": "stuck", "reasoning": f"AI error: {text_err}"}


async def _switch_to_newest_page(context, current_page):
    """If a new tab was opened, switch to it and return it. Otherwise return current_page."""
    try:
        pages = context.pages
        if len(pages) > 1:
            newest = pages[-1]
            if newest != current_page:
                await newest.wait_for_load_state("domcontentloaded", timeout=10000)
                console.print(f"  [dim]Switched to new tab: {newest.url}[/dim]")
                return newest
    except Exception:
        pass
    return current_page


async def _execute_ai_action(page, action_data: dict, job: Job, cover_letter: str,
                              acct_email: str, acct_password: str, settings, context=None) -> bool:
    """
    Execute the action decided by _ai_decide_action.
    Returns True if the application is confirmed done, False if stuck, None to continue.
    """
    action = action_data.get("action", "stuck")
    btn_text = action_data.get("button_text", "")
    reasoning = action_data.get("reasoning", "")
    console.print(f"  [dim]AI: {action} — {reasoning}[/dim]")

    if action == "done":
        return True

    if action == "stuck":
        return False

    if action == "dismiss_cookie":
        for sel in [
            # OneTrust specific
            "#onetrust-accept-btn-handler",
            "#accept-recommended-btn-handler",
            "button.ot-pc-refuse-all-handler",
            "button#onetrust-pc-btn-handler",
            "button:has-text('Confirm My Choices')",
            "button:has-text('Confirm my choices')",
            "button:has-text('Save My Choices')",
            "button:has-text('Accept All Cookies')",
            "button:has-text('Allow All')",
            # Generic
            "button:has-text('Accept all')", "button:has-text('Accept All')",
            "button:has-text('Accept cookies')", "button:has-text('Accept Cookies')",
            "button:has-text('I Accept')", "button:has-text('I agree')",
            "button:has-text('Agree')", "button:has-text('Accept')",
            "button:has-text('OK')", "button:has-text('Got it')",
            "button:has-text('Close')", "[id*='cookie'] button", "[class*='cookie'] button",
            "[id*='consent'] button", "[class*='consent'] button",
            "#onetrust-banner-sdk button", ".optanon-alert-box-wrapper button",
        ]:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1.5)
                    return None  # continue loop
            except Exception:
                continue
        # Fallback: press Escape
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        return None

    if action == "check_agreements":
        checkboxes = await page.query_selector_all('input[type="checkbox"]')
        for cb in checkboxes:
            try:
                if await cb.is_visible() and not await cb.is_checked():
                    label_text = await cb.evaluate("""el => {
                        const id = el.id;
                        const lbl = id ? document.querySelector('label[for="' + id + '"]') : null;
                        return lbl ? lbl.innerText.toLowerCase() : (el.closest('label') || {innerText:''}).innerText.toLowerCase();
                    }""")
                    # Only check agreement/consent/terms checkboxes
                    if any(kw in label_text for kw in (
                        "agree", "accept", "consent", "terms", "privacy", "policy", "confirm", "acknowledge"
                    )) or label_text.strip() == "":
                        await cb.check()
                        await asyncio.sleep(0.3)
            except Exception:
                continue
        return None

    if action == "handle_account":
        if acct_email and acct_password:
            await _handle_account_gate(page, acct_email, acct_password)
            await asyncio.sleep(2)
        return None

    if action == "fill_form":
        fields = await extract_form_fields(page)
        if fields:
            filled = await _fill_page_fields(page, fields, job, cover_letter)
            console.print(f"  [dim]Filled {filled}/{len(fields)} fields[/dim]")
        return None

    if action == "click_button":
        pages_before = len(context.pages) if context else 0

        async def _try_click(el) -> bool:
            try:
                if await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await asyncio.sleep(2)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
            return False

        clicked_ok = False

        # 1. Playwright locator (handles apostrophes/special chars correctly)
        for locator in [
            page.get_by_role("button", name=btn_text),
            page.get_by_role("link", name=btn_text),
            page.get_by_text(btn_text, exact=True),
        ]:
            try:
                el = locator.first
                if await el.count() and await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await asyncio.sleep(2)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    clicked_ok = True
                    break
            except Exception:
                continue

        if not clicked_ok:
            # 2. Iterate all clickable elements for partial text match
            all_btns = await page.query_selector_all("button, input[type='submit'], a[role='button'], a")
            for el in all_btns:
                try:
                    t = (await el.inner_text()).strip()
                    if btn_text.lower() in t.lower():
                        if await _try_click(el):
                            clicked_ok = True
                            break
                except Exception:
                    continue

        if not clicked_ok:
            # 3. JavaScript fallback — find by visible text across whole DOM
            try:
                clicked = await page.evaluate("""(text) => {
                    const els = [...document.querySelectorAll('button, a, [role="button"]')];
                    const match = els.find(e => e.offsetParent !== null &&
                        e.innerText && e.innerText.trim().toLowerCase().includes(text.toLowerCase()));
                    if (match) { match.click(); return true; }
                    return false;
                }""", btn_text)
                if clicked:
                    await asyncio.sleep(2)
                    clicked_ok = True
            except Exception:
                pass

        if not clicked_ok:
            console.print(f"  [yellow]Button '{btn_text}' not found[/yellow]")
            return "button_not_found"  # distinct sentinel for stuck detection

        # After a successful click, check if a new tab was opened — caller must handle page swap
        if context and len(context.pages) > pages_before:
            return "new_tab_opened"
        return None

    return None  # unknown action — continue


async def _navigate_to_application_form(page) -> bool:
    """
    If the current page is a job listing (not a form), find and click the Apply button
    to reach the actual application form. Returns True if navigation occurred.
    """
    apply_selectors = [
        # Greenhouse
        "a.apply-button",
        "a[href*='/jobs/apply']",
        "a[data-mapped='true'][href*='apply']",
        # Lever
        "a.postings-btn[href*='apply']",
        # Workday / generic
        "a[href*='apply']",
        # Generic button/link text
        "a:has-text('Apply for this Job')",
        "a:has-text('Apply Now')",
        "a:has-text('Apply now')",
        "button:has-text('Apply for this Job')",
        "button:has-text('Apply Now')",
    ]

    for sel in apply_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                text = (await el.inner_text()).strip()
                console.print(f"  [dim]Job listing page — clicking '{text}' to reach form...[/dim]")
                await el.click()
                await asyncio.sleep(3)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                return True
        except Exception:
            continue
    return False


async def _upload_resume(page, resume_pdf_path: str) -> bool:
    """
    Find a resume file-upload input (visible or hidden) and upload the PDF.
    Also handles 'Upload Resume' buttons that trigger a hidden <input type=file>.
    Returns True if the file was uploaded.
    """
    # 1. Direct file input (may be hidden — ATS sites often hide it behind a styled button)
    for sel in [
        'input[type="file"][name*="resume" i]',
        'input[type="file"][id*="resume" i]',
        'input[type="file"][accept*="pdf" i]',
        'input[type="file"][accept*=".pdf"]',
        'input[type="file"]',
    ]:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.set_input_files(resume_pdf_path)
                await asyncio.sleep(1.5)
                console.print(f"  [green]📎 Resume uploaded via file input[/green]")
                return True
        except Exception:
            continue

    # 2. Click a visible "Upload Resume" button to reveal the hidden input, then upload
    for btn_sel in [
        "button:has-text('Upload Resume')",
        "button:has-text('Upload resume')",
        "button:has-text('Attach Resume')",
        "button:has-text('Choose File')",
        "label:has-text('Upload Resume')",
        "label:has-text('Upload resume')",
        "label[for*='resume' i]",
        "label[for*='file' i]",
    ]:
        try:
            btn = await page.query_selector(btn_sel)
            if btn and await btn.is_visible():
                # If it's a <label for="...">, grab the associated input directly
                for_attr = await btn.get_attribute("for")
                if for_attr:
                    file_input = await page.query_selector(f'#{for_attr}')
                    if file_input:
                        await file_input.set_input_files(resume_pdf_path)
                        await asyncio.sleep(1.5)
                        console.print(f"  [green]📎 Resume uploaded via label input[/green]")
                        return True

                # Otherwise click the button and wait for a file chooser dialog
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_pdf_path)
                await asyncio.sleep(1.5)
                console.print(f"  [green]📎 Resume uploaded via file chooser[/green]")
                return True
        except Exception:
            continue

    return False


async def apply_on_external_site(page, job: Job, cover_letter: str) -> bool:
    """
    AI-driven form filler for external employer ATS pages.
    Handles job listing pages, account gates, and multi-step forms.
    """
    from config.settings import settings
    from pathlib import Path
    import yaml

    console.print(f"  [cyan]🌐 Attempting external apply at {page.url}[/cyan]")

    # Load account credentials from profile
    try:
        profile = yaml.safe_load(Path(settings.profile_path).read_text()) or {}
        acct_email = profile.get("account_email", "")
        acct_password = profile.get("account_password", "")
    except Exception:
        acct_email, acct_password = "", ""

    # Build resume PDF (cached — only re-renders if resume.md changed)
    try:
        from job_bot.utils.resume_pdf import build_resume_pdf
        resume_pdf = build_resume_pdf(settings.resume_path, settings.resume_path.replace(".md", ".pdf"))
    except Exception as e:
        console.print(f"  [yellow]Could not build resume PDF: {e}[/yellow]")
        resume_pdf = None

    try:
        # Wait for page to settle
        await asyncio.sleep(3)

        # Handle account gate (login/signup) if present
        if acct_email and acct_password:
            await _handle_account_gate(page, acct_email, acct_password)
            await asyncio.sleep(2)

        # Upload resume if a file input is present
        if resume_pdf:
            await _upload_resume(page, resume_pdf)

        fields = await extract_form_fields(page)

        # If no fields, try navigating from a job listing page
        if not fields:
            navigated = await _navigate_to_application_form(page)
            if navigated:
                await asyncio.sleep(2)
                # If navigation opened a new tab, switch to it and close extras
                ctx = page.context
                all_pages = ctx.pages
                if len(all_pages) > 1:
                    page = all_pages[-1]
                    for p in all_pages[1:-1]:
                        try:
                            await p.close()
                        except Exception:
                            pass
                    console.print(f"  [dim]Navigated to new tab: {page.url}[/dim]")
                # Check for account gate again after navigation
                if acct_email and acct_password:
                    await _handle_account_gate(page, acct_email, acct_password)
                    await asyncio.sleep(2)
                if resume_pdf:
                    await _upload_resume(page, resume_pdf)
                fields = await extract_form_fields(page)

        if fields:
            console.print(f"  [dim]Found {len(fields)} form fields — asking Qwen3 to fill...[/dim]")
            filled = await _fill_page_fields(page, fields, job, cover_letter)
            console.print(f"  [dim]Filled {filled}/{len(fields)} fields[/dim]")
        else:
            console.print("  [dim]No standard form fields found — handing off to AI-guided loop[/dim]")

        # AI-guided multi-step loop: Claude sees screenshot + HTML and decides each action
        os.makedirs(settings.screenshots_dir, exist_ok=True)
        context = page.context
        current_page = page
        max_steps = 15
        consecutive_failures = 0
        last_action_key = ""
        for form_step in range(max_steps):
            action_data = await _ai_decide_action(current_page, job, form_step, settings)
            result = await _execute_ai_action(
                current_page, action_data, job, cover_letter,
                acct_email, acct_password, settings, context=context
            )

            # Save screenshot for audit trail
            screenshot_path = (
                f"{settings.screenshots_dir}/{job.source}_{job.external_id}"
                f"_external_step{form_step}.png"
            )
            try:
                await current_page.screenshot(path=screenshot_path)
            except Exception:
                pass

            if result is True:
                console.print(f"  [green]✓ Submitted external application for {job.title} @ {job.company}[/green]")
                return True
            if result is False:
                console.print(f"  [yellow]AI could not proceed — flagging for manual review[/yellow]")
                return False

            # New tab opened — switch to it and close all stale extras
            if result == "new_tab_opened":
                all_pages = context.pages
                if len(all_pages) > 1:
                    new_page = all_pages[-1]
                    # Close all tabs except the LinkedIn tab (index 0) and the new one
                    for p in all_pages[1:-1]:
                        try:
                            await p.close()
                        except Exception:
                            pass
                    try:
                        await new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                    console.print(f"  [dim]Switched to new tab: {new_page.url}[/dim]")
                    current_page = new_page
                    if resume_pdf:
                        await _upload_resume(current_page, resume_pdf)
                consecutive_failures = 0
                last_action_key = ""
                continue

            # Stuck detection: same failed action repeating
            action_key = f"{action_data.get('action')}:{action_data.get('button_text', '')}"
            if result == "button_not_found" or action_key == last_action_key:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            last_action_key = action_key

            if consecutive_failures >= 3:
                console.print(f"  [yellow]Stuck: same action failed {consecutive_failures}x — giving up[/yellow]")
                return False
            # result is None → continue to next step

        console.print(f"  [yellow]Reached max steps ({max_steps}) without confirmation[/yellow]")
        return False

    except Exception as e:
        console.print(f"  [red]External apply error: {e}[/red]")
        return False
