from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import quote_plus

from rich.console import Console

from job_bot.models.job import Job
from job_bot.scrapers.base import BaseScraper, SearchCriteria

console = Console()


def _random_delay(min_s: float = 2.0, max_s: float = 7.0) -> None:
    asyncio.get_event_loop().run_until_complete(
        asyncio.sleep(random.uniform(min_s, max_s))
    )


class LinkedInScraper(BaseScraper):
    """
    Playwright-based LinkedIn scraper.

    Uses a persistent browser profile so login cookies survive across runs.
    Only targets jobs with Easy Apply (f_AL=true in search URL).
    """

    LOGIN_URL = "https://www.linkedin.com/login"
    JOBS_SEARCH_URL = "https://www.linkedin.com/jobs/search/"

    def __init__(
        self,
        email: str,
        password: str,
        profile_dir: str = "~/.job_bot/browser_profiles/linkedin",
        headless: bool = False,
        request_delay_min: float = 2.0,
        request_delay_max: float = 7.0,
        max_jobs_per_session: int = 40,
    ) -> None:
        self.email = email
        self.password = password
        self.profile_dir = str(Path(profile_dir).expanduser())
        self.headless = headless
        self.delay_min = request_delay_min
        self.delay_max = request_delay_max
        self.max_jobs_per_session = max_jobs_per_session
        self._browser = None
        self._context = None
        self._page = None

    async def _delay(self) -> None:
        await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

    async def _type_slowly(self, page, selector: str, text: str) -> None:
        await page.click(selector)
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.05, 0.2))

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Stealth: remove webdriver flag
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, *args):
        if self._context:
            await self._context.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        page = self._page
        await page.goto(self.LOGIN_URL)
        await self._delay()

        # Check if already logged in
        if "feed" in page.url or "jobs" in page.url:
            console.print("[green]LinkedIn: already logged in[/green]")
            return

        await self._type_slowly(page, "#username", self.email)
        await self._type_slowly(page, "#password", self.password)
        await page.hover("button[type='submit']")
        await asyncio.sleep(0.3)
        await page.click("button[type='submit']")
        await self._delay()

        # Handle 2FA / checkpoint
        if "checkpoint" in page.url or "challenge" in page.url:
            console.print(
                "[yellow]LinkedIn requires 2FA. Complete it in the browser, then press ENTER.[/yellow]"
            )
            input("Press ENTER after completing 2FA...")
            await self._delay()

        console.print("[green]LinkedIn: logged in successfully[/green]")

    async def search_jobs(self, criteria: SearchCriteria) -> AsyncIterator[Job]:
        page = self._page
        jobs_seen = 0

        for title in criteria.job_titles:
            for location in criteria.locations:
                if jobs_seen >= self.max_jobs_per_session:
                    console.print(
                        f"[yellow]LinkedIn: reached session limit ({self.max_jobs_per_session})[/yellow]"
                    )
                    return

                search_url = (
                    f"{self.JOBS_SEARCH_URL}"
                    f"?keywords={quote_plus(title)}"
                    f"&location={quote_plus(location)}"
                )
                if criteria.easy_apply_only:
                    search_url += "&f_AL=true"
                mode_label = "Easy Apply only" if criteria.easy_apply_only else "non-Easy Apply"
                console.print(f"[blue]LinkedIn: searching '{title}' in '{location}' [{mode_label}][/blue]")

                try:
                    await page.goto(search_url)
                    await self._delay()

                    # Scroll to load more results
                    for _ in range(3):
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(1.5)

                    job_cards = await page.query_selector_all(
                        "li[data-occludable-job-id]"
                    )
                    console.print(f"  Found {len(job_cards)} job cards")

                    for card in job_cards:
                        if jobs_seen >= self.max_jobs_per_session:
                            break
                        try:
                            job = await self._parse_job_card(card)
                            if job:
                                # Filter by easy apply mode
                                if criteria.easy_apply_only and not job.is_easy_apply:
                                    continue
                                if not criteria.easy_apply_only and job.is_easy_apply:
                                    continue
                                # Apply exclusion keywords
                                if self._is_excluded(job, criteria):
                                    continue
                                jobs_seen += 1
                                yield job
                                await asyncio.sleep(random.uniform(0.5, 1.5))
                        except Exception as e:
                            console.print(f"  [red]Error parsing card: {e}[/red]")
                            continue

                except Exception as e:
                    console.print(f"[red]LinkedIn search error for '{title}': {e}[/red]")
                    continue

                await self._delay()

    async def _parse_job_card(self, card) -> Job | None:
        try:
            # Extract job ID from data attribute
            job_id_attr = await card.get_attribute("data-occludable-job-id")
            if not job_id_attr:
                # Try inner anchor
                anchor = await card.query_selector("a.job-card-container__link")
                if anchor:
                    href = await anchor.get_attribute("href")
                    m = re.search(r"/jobs/view/(\d+)", href or "")
                    job_id_attr = m.group(1) if m else None

            if not job_id_attr:
                return None

            external_id = str(job_id_attr)
            url = f"https://www.linkedin.com/jobs/view/{external_id}/"

            title_el = await card.query_selector(".job-card-list__title--link, .job-card-list__title")
            company_el = await card.query_selector(".artdeco-entity-lockup__subtitle, .job-card-container__primary-description")
            location_el = await card.query_selector(".artdeco-entity-lockup__caption, .job-card-container__metadata-item")

            title = (await title_el.inner_text()).strip().splitlines()[0].strip() if title_el else "Unknown"
            company = (await company_el.inner_text()).strip().splitlines()[0].strip() if company_el else "Unknown"
            location = (await location_el.inner_text()).strip().splitlines()[0].strip() if location_el else None

            # Detect Easy Apply badge on the card
            card_text = (await card.inner_text()).lower()
            is_easy_apply = "easy apply" in card_text

            return Job(
                source="linkedin",
                external_id=external_id,
                title=title,
                company=company,
                location=location,
                url=url,
                is_easy_apply=is_easy_apply,
                status="new",
            )
        except Exception:
            return None

    def _is_excluded(self, job: Job, criteria: SearchCriteria) -> bool:
        text = f"{job.title} {job.company} {job.description or ''}".lower()
        for kw in criteria.keywords_excluded:
            if kw.lower() in text:
                return True
        return False

    async def get_job_detail(self, job: Job) -> Job:
        """
        Click the job card in the search results list to load the detail panel,
        then extract the description from .jobs-description__content.
        This is more reliable than navigating directly to /jobs/view/<id>/.
        """
        page = self._page
        try:
            # Click the job card in the list panel to populate the detail pane
            card = await page.query_selector(f"li[data-occludable-job-id='{job.external_id}']")
            if card:
                await card.click()
                await asyncio.sleep(2)
            else:
                # Fall back: navigate directly and wait for detail pane
                await page.goto(job.url)
                await asyncio.sleep(3)

            desc_el = await page.query_selector(".jobs-description__content")
            if desc_el:
                job.description = (await desc_el.inner_text()).strip()
            else:
                # Broader fallback
                desc_el = await page.query_selector(".jobs-description, .jobs-box__html-content")
                if desc_el:
                    job.description = (await desc_el.inner_text()).strip()

            # Extract salary/insights if present
            salary_el = await page.query_selector(
                ".job-details-jobs-unified-top-card__job-insight, "
                ".compensation__salary-range"
            )
            if salary_el:
                salary_text = (await salary_el.inner_text()).strip()
                job.description = f"[Salary/Insights: {salary_text}]\n\n" + (job.description or "")

        except Exception as e:
            console.print(f"  [yellow]Could not fetch detail for {job.external_id}: {e}[/yellow]")
        return job

    async def _find_apply_button(self, page):
        """
        Find the Apply / Easy Apply button on the current page.
        Tries multiple selector strategies to handle LinkedIn DOM changes.
        Returns (element, button_text) or (None, '').
        """
        strategies = [
            # Text-based (most resilient to DOM changes)
            "button:has-text('Easy Apply')",
            "button:has-text('Apply now')",
            "button:has-text('Apply')",
            # Class-based (LinkedIn-specific)
            "button.jobs-apply-button",
            ".jobs-apply-button--top-card button",
            ".jobs-s-apply button",
            # Aria-label
            "button[aria-label*='Easy Apply']",
            "button[aria-label*='Apply to']",
            "button[aria-label*='apply']",
            # Data attribute
            "button[data-control-name*='apply']",
        ]
        for sel in strategies:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    text = (await el.inner_text()).strip().lower()
                    return el, text
            except Exception:
                continue
        return None, ""

    async def apply_easy(self, job: Job, cover_letter: str) -> bool:
        """
        Attempt LinkedIn Easy Apply flow. Falls back to external apply via AI form-filler.
        Returns True if the application was submitted successfully.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        page = self._page

        try:
            # Try direct job URL first; fall back to search panel URL if button not found
            for nav_url in [job.url, f"https://www.linkedin.com/jobs/search/?currentJobId={job.external_id}"]:
                await page.goto(nav_url)
                await self._delay()
                try:
                    await page.wait_for_selector(
                        "button:has-text('Easy Apply'), button:has-text('Apply'), button.jobs-apply-button",
                        timeout=8000,
                    )
                except Exception:
                    pass
                apply_btn, btn_text = await self._find_apply_button(page)
                if apply_btn:
                    console.print(f"  [dim]Found apply button via {nav_url} → '{btn_text}'[/dim]")
                    break

            if not apply_btn:
                all_btns = await page.query_selector_all("button")
                labels = []
                for b in all_btns[:15]:
                    try:
                        txt = (await b.inner_text()).strip()[:50]
                        aria = await b.get_attribute("aria-label") or ""
                        if txt or aria:
                            labels.append(repr(txt or aria))
                    except Exception:
                        pass
                console.print(
                    f"  [yellow]No apply button found for {job.external_id}. "
                    f"Buttons on page: {', '.join(labels) or 'none'}[/yellow]"
                )
                return False

            # External apply — button text doesn't say "easy apply"
            if "easy" not in btn_text:
                console.print(f"  [cyan]External apply button ('{btn_text}') — launching form filler...[/cyan]")
                return await self._try_external_apply(page, apply_btn, job, cover_letter)

            # --- Easy Apply modal flow ---
            console.print(f"  [green]Easy Apply found — clicking...[/green]")
            await apply_btn.hover()
            await asyncio.sleep(0.3)
            await apply_btn.click()
            await asyncio.sleep(2)

            # Verify modal opened
            modal = await page.query_selector(".jobs-easy-apply-modal, [data-test-modal]")
            if not modal:
                console.print(f"  [yellow]Easy Apply modal did not open after click[/yellow]")
                return False

            console.print(f"  [dim]Modal opened — walking steps...[/dim]")

            max_steps = 15
            prev_btn_signature = ""
            stuck_count = 0
            for step in range(max_steps):
                await asyncio.sleep(0.5)

                # Dump current modal buttons for diagnostics
                modal_btns = await page.query_selector_all(
                    ".jobs-easy-apply-modal button, [data-test-modal] button"
                )
                btn_texts = []
                for b in modal_btns:
                    try:
                        t = (await b.inner_text()).strip()
                        a = await b.get_attribute("aria-label") or ""
                        btn_texts.append(repr(t or a))
                    except Exception:
                        pass
                btn_signature = "|".join(btn_texts)
                console.print(f"  [dim]Step {step} buttons: {', '.join(btn_texts)}[/dim]")

                # Stuck detection: same buttons twice in a row means a required field is blocking
                if btn_signature == prev_btn_signature:
                    stuck_count += 1
                    if stuck_count >= 2:
                        # Try to dismiss any open sub-form (Cancel button) before giving up
                        cancelled = False
                        for cancel_sel in [
                            "button:has-text('Cancel')",
                            "button[aria-label='Cancel']",
                        ]:
                            try:
                                cancel_btn = await page.query_selector(cancel_sel)
                                if cancel_btn and await cancel_btn.is_visible():
                                    txt = (await cancel_btn.inner_text()).strip().lower()
                                    if txt == "cancel":  # exact match — avoid Dismiss
                                        console.print(f"  [dim]Stuck — cancelling open sub-form...[/dim]")
                                        await cancel_btn.click()
                                        await asyncio.sleep(1.0)
                                        stuck_count = 0
                                        prev_btn_signature = ""
                                        cancelled = True
                                        break
                            except Exception:
                                continue
                        if not cancelled:
                            console.print(f"  [yellow]Stuck on step {step} — required field likely blocking[/yellow]")
                            break
                else:
                    stuck_count = 0
                prev_btn_signature = btn_signature

                # Submit button (final step)
                submit_btn = await page.query_selector(
                    "button[aria-label='Submit application'], "
                    "button[data-control-name='submit_unify']"
                )
                if submit_btn and await submit_btn.is_visible():
                    from config.settings import settings
                    import os
                    os.makedirs(settings.screenshots_dir, exist_ok=True)
                    screenshot_path = (
                        f"{settings.screenshots_dir}/{job.source}_{job.external_id}_pre_submit.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    console.print(f"  [cyan]Screenshot: {screenshot_path}[/cyan]")
                    await submit_btn.click()
                    await asyncio.sleep(2)
                    console.print(f"  [green]✓ Applied: {job.title} @ {job.company}[/green]")
                    return True

                # Fill fields on this step
                await self._handle_easy_apply_step(page, cover_letter)

                # Next / Review / Continue buttons (order matters — most specific first)
                next_btn = None
                for next_sel in [
                    "button[aria-label='Continue to next step']",
                    "button[aria-label='Review your application']",
                    "button:has-text('Review')",
                    "button:has-text('Next')",
                    "button:has-text('Continue')",
                ]:
                    try:
                        el = await page.query_selector(next_sel)
                        if el and await el.is_visible():
                            next_btn = el
                            break
                    except Exception:
                        continue

                if not next_btn:
                    console.print(f"  [yellow]No Next/Continue button at step {step} — stopping[/yellow]")
                    break

                next_text = (await next_btn.inner_text()).strip()
                console.print(f"  [dim]Clicking: '{next_text}'[/dim]")
                await next_btn.click()
                await asyncio.sleep(1.5)

            console.print(f"  [yellow]Easy Apply: did not reach submit after {max_steps} steps[/yellow]")
            return False

        except PlaywrightTimeout:
            console.print(f"  [red]Timeout during Easy Apply for {job.url}[/red]")
            return False
        except Exception as e:
            console.print(f"  [red]Easy Apply error for {job.url}: {e}[/red]")
            return False

    async def _try_external_apply(self, page, apply_btn, job: Job, cover_letter: str) -> bool:
        """
        Click the external Apply button, handle LinkedIn's 'leaving' confirmation modal,
        then use Qwen3 to fill and submit the employer's application form.
        """
        from job_bot.applicator.external_apply import apply_on_external_site

        new_page = None
        pages_before = set(id(p) for p in page.context.pages)

        # Click the Apply button
        await apply_btn.click()
        await asyncio.sleep(1.5)

        # Dismiss the "You're leaving LinkedIn" modal if it appears
        for selector in [
            "button[aria-label='Continue to apply']",
            "button:has-text('Continue to apply')",
            "button:has-text('Apply now')",
            ".artdeco-modal button.artdeco-button--primary",
        ]:
            try:
                modal_btn = await page.wait_for_selector(selector, timeout=3000)
                if modal_btn and await modal_btn.is_visible():
                    console.print(f"  [dim]Dismissing 'leaving LinkedIn' modal...[/dim]")
                    await modal_btn.click()
                    break
            except Exception:
                continue

        # Wait up to 10s for a new tab to appear
        for _ in range(20):
            await asyncio.sleep(0.5)
            new_tabs = [p for p in page.context.pages if id(p) not in pages_before]
            if new_tabs:
                new_page = new_tabs[-1]
                break

        if new_page:
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            console.print(f"  [cyan]External page (new tab): {new_page.url}[/cyan]")
        elif "linkedin.com" not in page.url:
            # Same-tab navigation
            new_page = page
            console.print(f"  [cyan]External page (same tab): {page.url}[/cyan]")
        else:
            console.print(f"  [yellow]External apply: no new page detected after clicking Apply[/yellow]")
            return False

        try:
            result = await apply_on_external_site(new_page, job, cover_letter)
        except Exception as e:
            console.print(f"  [red]External apply error: {e}[/red]")
            result = False
        finally:
            if new_page is not None and new_page is not page:
                await new_page.close()

        return result

    async def _get_field_label(self, page, element) -> str:
        """Return the best human-readable label for a form element."""
        try:
            el_id = await element.get_attribute("id") or ""
            if el_id:
                lbl = await page.query_selector(f'label[for="{el_id}"]')
                if lbl:
                    return (await lbl.inner_text()).strip()
            aria = await element.get_attribute("aria-label") or ""
            if aria:
                return aria.strip()
            ph = await element.get_attribute("placeholder") or ""
            if ph:
                return ph.strip()
            return await element.evaluate(
                """el => {
                    let p = el.parentElement;
                    while (p && !['LABEL','FIELDSET','FORM'].includes(p.tagName))
                        p = p.parentElement;
                    return (p && p.tagName === 'LABEL') ? p.innerText.trim() : '';
                }"""
            )
        except Exception:
            return ""

    async def _fill_add_section(self, page, scope, section_type: str, data: dict) -> None:
        """
        Click an 'Add education' or 'Add experience' button, fill the sub-form,
        then click Save to confirm the entry.
        """
        # Find the Add button for this section type
        keywords = {
            "education": ["add education", "add school"],
            "experience": ["add experience", "add work experience", "add position"],
        }.get(section_type, [])

        add_btn = None
        for btn in await scope.query_selector_all("button"):
            if not await btn.is_visible():
                continue
            txt = (await btn.inner_text()).strip().lower()
            if any(kw in txt for kw in keywords):
                add_btn = btn
                break

        if not add_btn:
            return

        console.print(f"  [dim]Expanding '{section_type}' sub-form...[/dim]")
        await add_btn.click()
        await asyncio.sleep(1.5)

        # --- Fill sub-form fields directly from structured data ---
        month_map = {
            "January": "1", "February": "2", "March": "3", "April": "4",
            "May": "5", "June": "6", "July": "7", "August": "8",
            "September": "9", "October": "10", "November": "11", "December": "12",
        }

        async def _try_fill(selectors: list[str], value: str) -> bool:
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        tag = await el.evaluate("e => e.tagName.toLowerCase()")
                        if tag == "select":
                            # Skip hidden/disabled selects (e.g. end-date when currently_working)
                            if not await el.is_enabled():
                                continue
                            try:
                                await el.select_option(label=value, timeout=5000)
                            except Exception:
                                # Try partial match on option text
                                opts = await el.query_selector_all("option")
                                for o in opts:
                                    ot = (await o.inner_text()).strip()
                                    if value.lower() in ot.lower() or ot.lower() in value.lower():
                                        await el.select_option(value=await o.get_attribute("value") or ot, timeout=5000)
                                        break
                        else:
                            await el.click(click_count=3, timeout=5000)
                            await el.fill(value)
                            # Dismiss any autocomplete dropdown
                            await page.keyboard.press("Escape")
                        await asyncio.sleep(0.3)
                        return True
                except Exception:
                    continue
            return False

        if section_type == "education":
            await _try_fill(
                ["input[id*='school'], input[aria-label*='school'], input[aria-label*='School'], "
                 "input[placeholder*='school'], input[placeholder*='School']".split(", ")],
                data.get("school", ""),
            )
            # Wait for autocomplete to appear and dismiss it
            await asyncio.sleep(1.0)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

            degree_val = data.get("degree", "")
            await _try_fill(
                ["select[id*='degree'], select[aria-label*='degree'], select[aria-label*='Degree']"],
                degree_val,
            )
            await _try_fill(
                ["input[id*='field'], input[aria-label*='field of study'], input[placeholder*='field']"],
                data.get("field_of_study", ""),
            )
            await asyncio.sleep(0.5)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            # Start date
            if data.get("start_month"):
                await _try_fill(
                    ["select[id*='start'][id*='month'], select[aria-label*='Start Month']"],
                    data["start_month"],
                )
                await _try_fill(
                    ["select[id*='start'][id*='year'], input[id*='start'][id*='year'], select[aria-label*='Start Year']"],
                    data.get("start_year", ""),
                )
            # End date
            if data.get("end_month"):
                await _try_fill(
                    ["select[id*='end'][id*='month'], select[aria-label*='End Month']"],
                    data["end_month"],
                )
                await _try_fill(
                    ["select[id*='end'][id*='year'], input[id*='end'][id*='year'], select[aria-label*='End Year']"],
                    data.get("end_year", ""),
                )
            if data.get("gpa"):
                await _try_fill(
                    ["input[id*='gpa'], input[aria-label*='gpa'], input[aria-label*='GPA']"],
                    data["gpa"],
                )

        elif section_type == "experience":
            await _try_fill(
                ["input[id*='title'], input[aria-label*='Title'], input[aria-label*='title'], input[placeholder*='Title']"],
                data.get("title", ""),
            )
            await _try_fill(
                ["input[id*='company'], input[aria-label*='Company'], input[placeholder*='Company']"],
                data.get("company", ""),
            )
            await asyncio.sleep(1.0)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)

            if data.get("employment_type"):
                await _try_fill(
                    ["select[id*='employment'], select[aria-label*='Employment']"],
                    data["employment_type"],
                )
            await _try_fill(
                ["input[id*='location'], input[aria-label*='Location'], input[placeholder*='Location']"],
                data.get("location", ""),
            )
            await asyncio.sleep(0.5)
            await page.keyboard.press("Escape")

            # Currently working checkbox
            if data.get("currently_working"):
                for sel in ["input[id*='current'], input[aria-label*='currently working']"]:
                    try:
                        el = await page.query_selector(sel)
                        if el and not await el.is_checked():
                            await el.check()
                            break
                    except Exception:
                        pass

            if data.get("start_month"):
                await _try_fill(
                    ["select[id*='start'][id*='month'], select[aria-label*='Start Month']"],
                    data["start_month"],
                )
                await _try_fill(
                    ["select[id*='start'][id*='year'], input[id*='start'][id*='year']"],
                    data.get("start_year", ""),
                )
            if not data.get("currently_working") and data.get("end_month"):
                await _try_fill(
                    ["select[id*='end'][id*='month'], select[aria-label*='End Month']"],
                    data["end_month"],
                )
                await _try_fill(
                    ["select[id*='end'][id*='year'], input[id*='end'][id*='year']"],
                    data.get("end_year", ""),
                )
            if data.get("description"):
                await _try_fill(
                    ["textarea[id*='description'], textarea[aria-label*='Description']"],
                    data["description"],
                )

        # Click Save button in the sub-form
        for save_sel in [
            "button[aria-label='Save']",
            "button:has-text('Save')",
            "button[data-easy-apply-next-button]",
        ]:
            try:
                save_btn = await page.query_selector(save_sel)
                if save_btn and await save_btn.is_visible():
                    console.print(f"  [dim]Saving '{section_type}' entry...[/dim]")
                    await save_btn.click()
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                continue

    async def _collect_validation_errors(self, page, scope) -> list[dict]:
        """Return [{field, error}] for any visible validation error messages in the modal."""
        errors = []
        selectors = [
            ".artdeco-inline-feedback--error .artdeco-inline-feedback__message",
            ".artdeco-inline-feedback__message",
            "[data-test-form-element-error-message]",
            ".fb-form-element__error-text",
        ]
        for sel in selectors:
            try:
                els = await (scope or page).query_selector_all(sel)
                for el in els:
                    if not await el.is_visible():
                        continue
                    msg = (await el.inner_text()).strip()
                    if not msg:
                        continue
                    label = await el.evaluate("""el => {
                        const c = el.closest('[data-test-form-element]')
                            || el.closest('fieldset')
                            || el.closest('.jobs-easy-apply-form-section__grouping')
                            || el.parentElement;
                        if (!c) return '';
                        const l = c.querySelector('label, legend, .fb-form-element-label, .jobs-easy-apply-form-element__label');
                        return l ? l.innerText.trim() : '';
                    }""")
                    errors.append({"field": label or "unknown", "error": msg})
            except Exception:
                continue
        # Deduplicate
        seen, unique = set(), []
        for e in errors:
            k = (e["field"], e["error"])
            if k not in seen:
                seen.add(k)
                unique.append(e)
        return unique

    async def _apply_answers_to_fields(self, page, fields: list, answer_map: dict) -> None:
        """Apply label→answer map to a list of extracted form fields."""
        import re as _re

        for f in fields:
            key = f["label"].strip().lower()
            answer = answer_map.get(key, "")

            if f["kind"] == "input":
                if not answer:
                    lbl = f["label"].lower()
                    if "year" in lbl or "experience" in lbl:
                        answer = "3"
                    else:
                        continue
                if f.get("type") == "number":
                    answer = _re.sub(r"[^\d]", "", str(answer))
                try:
                    await f["element"].click(click_count=3, timeout=5000)
                    await f["element"].fill(str(answer))
                    await asyncio.sleep(0.4)
                    # Dismiss any typeahead/autocomplete dropdown that may have appeared
                    await page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                except Exception as e:
                    console.print(f"  [dim]Could not fill '{f['label']}': {e}[/dim]")

            elif f["kind"] == "radio":
                ans_lower = answer.lower()
                target_opt = None
                for opt in f["options"]:
                    if opt["label"].lower() == ans_lower or opt["value"].lower() == ans_lower:
                        target_opt = opt
                        break
                if not target_opt:
                    for opt in f["options"]:
                        if opt["label"].lower() in ("yes", "y"):
                            target_opt = opt
                            break
                if not target_opt and f["options"]:
                    target_opt = f["options"][0]

                if target_opt:
                    async def _click_radio(opt):
                        if opt.get("label_element"):
                            try:
                                await opt["label_element"].click()
                                return
                            except Exception:
                                pass
                        try:
                            await opt["element"].click()
                        except Exception:
                            try:
                                await opt["element"].check()
                            except Exception as err:
                                console.print(f"  [dim]Radio click failed '{f['label']}': {err}[/dim]")

                    await _click_radio(target_opt)
                    await asyncio.sleep(0.3)
                    console.print(f"  [dim]Radio '{f['label']}' → '{target_opt['label']}'[/dim]")

            elif f["kind"] == "select":
                _placeholders = {"month", "year", "select", "choose", "please select", ""}
                if not answer or answer.strip().lower() in _placeholders:
                    continue
                try:
                    # Skip hidden/disabled selects (e.g. end-date when currently working)
                    is_visible = await f["element"].is_visible()
                    is_enabled = await f["element"].is_enabled()
                    if not is_visible or not is_enabled:
                        continue
                    opts = f.get("options", [])
                    ans_lower = answer.strip().lower()
                    matched = None
                    for opt in opts:
                        if opt.strip().lower() == ans_lower:
                            matched = opt
                            break
                    if not matched:
                        for opt in opts:
                            if ans_lower in opt.strip().lower() or opt.strip().lower() in ans_lower:
                                matched = opt
                                break
                    if matched:
                        await f["element"].select_option(label=matched, timeout=5000)
                    else:
                        await f["element"].select_option(value=answer, timeout=5000)
                    await asyncio.sleep(0.2)
                except Exception as e:
                    console.print(f"  [dim]Select failed '{f['label']}': {e}[/dim]")

    async def _handle_easy_apply_step(self, page, cover_letter: str) -> None:
        """
        Fill all visible form fields on the current Easy Apply modal step.
        Uses Qwen3 to answer screening questions based on the candidate profile.
        """
        import json
        import yaml
        from config.settings import settings
        from pathlib import Path
        from job_bot.ai.ollama_client import ollama_chat

        modal = await page.query_selector(".jobs-easy-apply-modal, [data-test-modal]")
        scope = modal if modal else page

        # --- 0. Handle 'Add education / experience' sections first ---
        try:
            profile_data: dict = {}
            with open(settings.profile_path) as pf:
                profile_data = yaml.safe_load(pf) or {}

            edu_list = profile_data.get("education", [])
            exp_list = profile_data.get("experience", [])

            # Only add entries if the section is empty (Remove/Edit = already populated)
            has_existing_entries = bool(await scope.query_selector("button:has-text('Remove')"))

            for btn in await scope.query_selector_all("button"):
                if not await btn.is_visible():
                    continue
                txt = (await btn.inner_text()).strip().lower()
                if "add" in txt and "education" in txt and edu_list and not has_existing_entries:
                    await self._fill_add_section(page, scope, "education", edu_list[0])
                elif "add" in txt and any(kw in txt for kw in ("experience", "work", "position")) and exp_list and not has_existing_entries:
                    await self._fill_add_section(page, scope, "experience", exp_list[0])
        except Exception as e:
            console.print(f"  [yellow]Add-section handling failed: {e}[/yellow]")

        # --- 1. Cover letter textarea ---
        for cl_sel in [
            "textarea[id*='cover-letter']",
            "textarea[placeholder*='cover letter']",
            "textarea[placeholder*='Cover letter']",
        ]:
            el = await scope.query_selector(cl_sel)
            if el and await el.is_visible():
                await el.fill(cover_letter[:3000])
                break

        # --- 2. Collect all unfilled visible fields ---
        fields = []  # list of dicts with keys: kind, label, id, name, element, options

        # Text / number / email / tel / url inputs and textareas
        for inp in await scope.query_selector_all(
            "input[type='text'], input[type='number'], input[type='tel'], "
            "input[type='email'], input[type='url'], textarea"
        ):
            try:
                if not await inp.is_visible():
                    continue
                if await inp.input_value():
                    continue  # already filled
                label = await self._get_field_label(page, inp)
                inp_type = await inp.get_attribute("type") or "text"
                fields.append({
                    "kind": "input",
                    "type": inp_type,
                    "label": label,
                    "id": await inp.get_attribute("id") or "",
                    "name": await inp.get_attribute("name") or "",
                    "element": inp,
                })
            except Exception:
                continue

        # Radio groups
        for group in await scope.query_selector_all(
            ".jobs-easy-apply-form-section__grouping, fieldset"
        ):
            try:
                if await group.query_selector("input[type='radio']:checked"):
                    continue  # already answered
                radios = await group.query_selector_all("input[type='radio']")
                if not radios:
                    continue
                lbl_el = await group.query_selector(
                    "legend, .fb-form-element-label, .jobs-easy-apply-form-element__label"
                )
                question = (await lbl_el.inner_text()).strip() if lbl_el else ""
                options = []
                for r in radios:
                    r_id = await r.get_attribute("id") or ""
                    lbl = await page.query_selector(f'label[for="{r_id}"]') if r_id else None
                    lbl_text = (await lbl.inner_text()).strip() if lbl else (await r.get_attribute("value") or "")
                    options.append({
                        "label": lbl_text,
                        "value": await r.get_attribute("value") or "",
                        "element": r,
                        "label_element": lbl,  # label is clickable; input may be CSS-hidden
                    })
                fields.append({"kind": "radio", "label": question, "options": options})
            except Exception:
                continue

        # Select dropdowns
        for sel_el in await scope.query_selector_all("select"):
            try:
                if not await sel_el.is_visible():
                    continue
                label = await self._get_field_label(page, sel_el)
                opts = [(await o.inner_text()).strip() for o in await sel_el.query_selector_all("option")]
                fields.append({
                    "kind": "select",
                    "label": label,
                    "id": await sel_el.get_attribute("id") or "",
                    "name": await sel_el.get_attribute("name") or "",
                    "options": opts,
                    "element": sel_el,
                })
            except Exception:
                continue

        if not fields:
            return

        # --- 3. Ask Qwen3 to answer each field ---
        profile_text = Path(settings.profile_path).read_text() if Path(settings.profile_path).exists() else ""
        resume_text = Path(settings.resume_path).read_text() if Path(settings.resume_path).exists() else ""

        fields_info = []
        for f in fields:
            info: dict = {"label": f["label"], "type": f.get("type", f["kind"])}
            if f["kind"] in ("radio", "select"):
                info["options"] = [o["label"] if isinstance(o, dict) else o for o in f["options"]]
            fields_info.append(info)

        system_prompt = (
            "You are filling out a LinkedIn Easy Apply screening form for a job candidate.\n"
            "Given the form fields below, return a JSON array with one object per field.\n"
            'Each object: {"label": "<exact field label>", "answer": "<your answer>"}\n'
            "Rules:\n"
            "- Work authorization / legally authorized to work → Yes\n"
            "- Require sponsorship / visa sponsorship / immigration sponsorship / will require sponsorship → No\n"
            "- For radios/selects pick an exact option from the provided list\n"
            "- For salary fields return a whole integer only (e.g. 105000), no symbols, no decimals, no ranges\n"
            "- Number/years fields: return digits only (e.g. 3)\n"
            "- Address / physical address / street address → use address_us ('8324 Regents Rd, San Diego, CA 92122') for US jobs, address_ca ('5626 Bell Harbour Dr, Mississauga, ON L5M 5J3') for Canadian jobs\n"
            "- City → San Diego (US) or Mississauga (CA)\n"
            "- State/Province → CA (US) or ON (Canada)\n"
            "- Zip/Postal code → 92122 (US) or L5M 5J3 (Canada)\n"
            "- Use the candidate profile/resume for all other answers\n"
            "- Return ONLY a valid JSON array, no explanation or markdown"
        )
        user_prompt = (
            f"Form fields:\n{json.dumps(fields_info, indent=2)}\n\n"
            f"Candidate Profile:\n{profile_text[:1500]}\n\n"
            f"Candidate Resume:\n{resume_text[:2000]}\n\n"
            "Return JSON array of answers:"
        )

        answer_map: dict = {}
        try:
            from job_bot.ai.evaluator import _repair_and_parse
            raw = ollama_chat(
                system=system_prompt,
                user=user_prompt,
                model=settings.ollama_model,
                base_url=settings.ollama_base_url,
                max_tokens=512,
            )
            answers = _repair_and_parse(raw)
            answer_map = {a["label"].strip().lower(): str(a["answer"]) for a in answers}
            console.print(f"  [dim]AI answers: {answer_map}[/dim]")
        except Exception as e:
            console.print(f"  [yellow]AI field-fill failed ({e}) — using defaults[/yellow]")

        # --- 4. Apply answers ---
        await self._apply_answers_to_fields(page, fields, answer_map)

        # --- 5. Check for validation errors and let Qwen fix them ---
        await asyncio.sleep(0.5)
        errors = await self._collect_validation_errors(page, scope)
        if not errors:
            return

        console.print(f"  [yellow]Validation errors — asking AI to correct {len(errors)} field(s):[/yellow]")
        for e in errors:
            console.print(f"    [dim]{e['field']}: {e['error']}[/dim]")

        error_labels = {e["field"].strip().lower() for e in errors}
        retry_fields = [f for f in fields if f["label"].strip().lower() in error_labels] or fields

        retry_info = []
        for f in retry_fields:
            info = {
                "label": f["label"],
                "type": f.get("type", f["kind"]),
                "current_wrong_answer": answer_map.get(f["label"].strip().lower(), ""),
            }
            if f["kind"] in ("radio", "select"):
                info["options"] = [o["label"] if isinstance(o, dict) else o for o in f["options"]]
            retry_info.append(info)

        error_list = "\n".join(f'- "{e["field"]}": {e["error"]}' for e in errors)
        try:
            raw_retry = ollama_chat(
                system=(
                    "You are correcting job application form answers that caused validation errors.\n"
                    "Return ONLY a valid JSON array: [{\"label\": \"...\", \"answer\": \"...\"}]\n"
                    "Rules:\n"
                    "- Number/years fields: return digits only (e.g. 3, not 'Yes', not '3 years')\n"
                    "- Radio/select: pick an exact option string from the provided options list\n"
                    "- Salary: whole integer only (e.g. 105000)\n"
                    "- Require sponsorship / visa sponsorship / immigration sponsorship → No\n"
                    "- Return ONLY valid JSON array, no explanation, no markdown"
                ),
                user=(
                    f"Validation errors from the previous answer attempt:\n{error_list}\n\n"
                    f"Fields to fix:\n{json.dumps(retry_info, indent=2)}\n\n"
                    f"Candidate Profile:\n{profile_text[:800]}\n\n"
                    "Return corrected JSON array:"
                ),
                model=settings.ollama_model,
                base_url=settings.ollama_base_url,
                max_tokens=256,
            )
            from job_bot.ai.evaluator import _repair_and_parse
            retry_answers = _repair_and_parse(raw_retry)
            retry_map = {a["label"].strip().lower(): str(a["answer"]) for a in retry_answers}
            console.print(f"  [dim]Retry answers: {retry_map}[/dim]")
            await self._apply_answers_to_fields(page, retry_fields, retry_map)
        except Exception as e:
            console.print(f"  [yellow]Retry prompt failed: {e}[/yellow]")
