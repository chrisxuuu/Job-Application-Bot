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
                    f"&f_AL=true"  # Easy Apply only
                )
                console.print(f"[blue]LinkedIn: searching '{title}' in '{location}'[/blue]")

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

            title = (await title_el.inner_text()).strip() if title_el else "Unknown"
            company = (await company_el.inner_text()).strip() if company_el else "Unknown"
            location = (await location_el.inner_text()).strip() if location_el else None

            return Job(
                source="linkedin",
                external_id=external_id,
                title=title,
                company=company,
                location=location,
                url=url,
                is_easy_apply=True,
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

    async def apply_easy(self, job: Job, cover_letter: str) -> bool:
        """
        Attempt LinkedIn Easy Apply flow.
        Returns True if the application was submitted successfully.
        """
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        page = self._page

        try:
            await page.goto(job.url)
            await self._delay()

            # Click Easy Apply button
            easy_apply_btn = await page.query_selector(
                "button.jobs-apply-button, .jobs-apply-button--top-card button"
            )
            if not easy_apply_btn:
                console.print(f"  [yellow]Easy Apply button not found for {job.url}[/yellow]")
                return False

            await easy_apply_btn.hover()
            await asyncio.sleep(0.3)
            await easy_apply_btn.click()
            await asyncio.sleep(2)

            # Walk through the modal wizard
            max_steps = 10
            for step in range(max_steps):
                # Check if we reached the submit/review step
                submit_btn = await page.query_selector(
                    "button[aria-label='Submit application'], button[data-control-name='submit_unify']"
                )
                if submit_btn:
                    # Take screenshot before submitting
                    from config.settings import settings
                    import os
                    os.makedirs(settings.screenshots_dir, exist_ok=True)
                    screenshot_path = (
                        f"{settings.screenshots_dir}/{job.source}_{job.external_id}_pre_submit.png"
                    )
                    await page.screenshot(path=screenshot_path)
                    console.print(f"  [cyan]Screenshot saved: {screenshot_path}[/cyan]")

                    await submit_btn.click()
                    await asyncio.sleep(2)
                    console.print(f"  [green]Applied to {job.title} @ {job.company}[/green]")
                    return True

                # Handle screening questions
                await self._handle_easy_apply_step(page, cover_letter)

                # Click Next button
                next_btn = await page.query_selector(
                    "button[aria-label='Continue to next step'], "
                    "button[aria-label='Review your application']"
                )
                if not next_btn:
                    console.print(f"  [yellow]No Next button found at step {step}[/yellow]")
                    break

                await next_btn.click()
                await asyncio.sleep(1.5)

            console.print(f"  [yellow]Easy Apply: max steps reached without submit[/yellow]")
            return False

        except PlaywrightTimeout:
            console.print(f"  [red]Timeout during Easy Apply for {job.url}[/red]")
            return False
        except Exception as e:
            console.print(f"  [red]Easy Apply error for {job.url}: {e}[/red]")
            return False

    async def _handle_easy_apply_step(self, page, cover_letter: str) -> None:
        """Fill in form fields on the current Easy Apply step."""
        # Cover letter text area
        cover_letter_el = await page.query_selector(
            "textarea[id*='cover-letter'], textarea[placeholder*='cover letter']"
        )
        if cover_letter_el:
            await cover_letter_el.fill(cover_letter[:3000])  # LinkedIn has a char limit

        # Handle yes/no radio questions (answer "Yes" by default where applicable)
        radio_groups = await page.query_selector_all(".jobs-easy-apply-form-section__grouping")
        for group in radio_groups:
            yes_radio = await group.query_selector("input[type='radio'][value='Yes']")
            if yes_radio:
                await yes_radio.check()

        # Handle numeric inputs for years of experience
        number_inputs = await page.query_selector_all("input[type='number']")
        for inp in number_inputs:
            label = await inp.get_attribute("aria-label") or ""
            if "year" in label.lower() or "experience" in label.lower():
                current_val = await inp.input_value()
                if not current_val:
                    await inp.fill("3")  # Default to 3 years; override in profile.yaml
