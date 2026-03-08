from __future__ import annotations

import asyncio
import random
import re
from html.parser import HTMLParser
from typing import AsyncIterator
from urllib.parse import quote_plus

import httpx
from rich.console import Console

from job_bot.models.job import Job
from job_bot.scrapers.base import BaseScraper, SearchCriteria

console = Console()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


class _JobCardParser(HTMLParser):
    """Minimal HTML parser to extract job listing data from ZipRecruiter pages."""

    def __init__(self):
        super().__init__()
        self.jobs: list[dict] = []
        self._current: dict | None = None
        self._in_title = False
        self._in_company = False
        self._in_location = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        # ZipRecruiter job cards use article[data-job-id]
        if tag == "article" and "data-job-id" in attrs_dict:
            self._current = {
                "id": attrs_dict["data-job-id"],
                "url": attrs_dict.get("data-job-url", ""),
                "title": "",
                "company": "",
                "location": "",
            }
        if self._current:
            classes = attrs_dict.get("class", "")
            if "job_title" in classes or "title" in classes:
                self._in_title = True
            elif "hiring_company_text" in classes or "company_name" in classes:
                self._in_company = True
            elif "location" in classes:
                self._in_location = True

    def handle_data(self, data):
        if not self._current:
            return
        data = data.strip()
        if not data:
            return
        if self._in_title:
            self._current["title"] += data
        elif self._in_company:
            self._current["company"] += data
        elif self._in_location:
            self._current["location"] += data

    def handle_endtag(self, tag):
        if self._current and tag in ("h2", "span", "p"):
            self._in_title = False
            self._in_company = False
            self._in_location = False
        if tag == "article" and self._current:
            if self._current.get("id") and self._current.get("title"):
                self.jobs.append(self._current)
            self._current = None


class ZipRecruiterScraper(BaseScraper):
    """
    ZipRecruiter scraper using httpx for listing pages.
    Falls back to Playwright if a challenge page is detected.
    """

    SEARCH_URL = "https://www.ziprecruiter.com/jobs-search"
    LOGIN_URL = "https://www.ziprecruiter.com/login"

    def __init__(
        self,
        email: str = "",
        password: str = "",
        profile_dir: str = "~/.job_bot/browser_profiles/ziprecruiter",
        headless: bool = False,
        request_delay_min: float = 1.0,
        request_delay_max: float = 3.0,
    ) -> None:
        self.email = email
        self.password = password
        self.profile_dir = profile_dir
        self.headless = headless
        self.delay_min = request_delay_min
        self.delay_max = request_delay_max
        self._http_client: httpx.AsyncClient | None = None
        self._playwright_context = None
        self._page = None
        self._pw = None

    async def _delay(self) -> None:
        await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

    async def __aenter__(self):
        self._http_client = httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30)
        return self

    async def __aexit__(self, *args):
        if self._http_client:
            await self._http_client.aclose()
        if self._playwright_context:
            await self._playwright_context.close()
        if self._pw:
            await self._pw.stop()

    async def login(self) -> None:
        """Login via Playwright for authenticated requests (needed for 1-Click Apply)."""
        if not self.email:
            console.print("[yellow]ZipRecruiter: no credentials provided, skipping login[/yellow]")
            return

        from playwright.async_api import async_playwright
        from pathlib import Path

        Path(self.profile_dir).expanduser().mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._playwright_context = await self._pw.chromium.launch_persistent_context(
            str(Path(self.profile_dir).expanduser()),
            headless=self.headless,
        )
        self._page = await self._playwright_context.new_page()
        await self._page.goto(self.LOGIN_URL)
        await self._delay()

        if "dashboard" in self._page.url or "jobs" in self._page.url:
            console.print("[green]ZipRecruiter: already logged in[/green]")
            return

        await self._page.fill("input[name='email']", self.email)
        await self._page.fill("input[name='password']", self.password)
        await self._page.click("button[type='submit']")
        await self._delay()
        console.print("[green]ZipRecruiter: logged in[/green]")

    async def search_jobs(self, criteria: SearchCriteria) -> AsyncIterator[Job]:
        client = self._http_client
        assert client is not None

        for title in criteria.job_titles:
            for location in criteria.locations:
                url = (
                    f"{self.SEARCH_URL}"
                    f"?search={quote_plus(title)}"
                    f"&location={quote_plus(location)}"
                )
                console.print(f"[blue]ZipRecruiter: searching '{title}' in '{location}'[/blue]")

                try:
                    resp = await client.get(url)

                    # Detect challenge page
                    if resp.status_code != 200 or "challenge" in resp.text.lower()[:500]:
                        console.print(
                            "[yellow]ZipRecruiter: challenge detected, using Playwright[/yellow]"
                        )
                        async for job in self._search_with_playwright(title, location, criteria):
                            yield job
                        continue

                    jobs = self._parse_listings_html(resp.text, criteria)
                    console.print(f"  Found {len(jobs)} jobs via httpx")
                    for job in jobs:
                        yield job
                        await self._delay()

                except Exception as e:
                    console.print(f"[red]ZipRecruiter search error: {e}[/red]")
                    continue

                await self._delay()

    def _parse_listings_html(self, html: str, criteria: SearchCriteria) -> list[Job]:
        parser = _JobCardParser()
        parser.feed(html)
        jobs = []
        for item in parser.jobs:
            if not item.get("id") or not item.get("title"):
                continue
            job = Job(
                source="ziprecruiter",
                external_id=item["id"],
                title=item["title"].strip(),
                company=item.get("company", "").strip(),
                location=item.get("location", "").strip() or None,
                url=item.get("url") or f"https://www.ziprecruiter.com/ojob/view/{item['id']}",
                status="new",
            )
            # Apply exclusion keywords
            text = f"{job.title} {job.company}".lower()
            if any(kw.lower() in text for kw in criteria.keywords_excluded):
                continue
            jobs.append(job)
        return jobs

    async def _search_with_playwright(
        self, title: str, location: str, criteria: SearchCriteria
    ) -> AsyncIterator[Job]:
        if not self._page:
            await self.login()
        if not self._page:
            return

        url = (
            f"{self.SEARCH_URL}"
            f"?search={quote_plus(title)}"
            f"&location={quote_plus(location)}"
        )
        await self._page.goto(url)
        await self._delay()

        cards = await self._page.query_selector_all("article[data-job-id]")
        for card in cards:
            job_id = await card.get_attribute("data-job-id")
            if not job_id:
                continue
            title_el = await card.query_selector(".job_title, h2")
            company_el = await card.query_selector(".hiring_company_text, .company_name")
            location_el = await card.query_selector(".location")

            title_text = (await title_el.inner_text()).strip() if title_el else "Unknown"
            company_text = (await company_el.inner_text()).strip() if company_el else "Unknown"
            location_text = (await location_el.inner_text()).strip() if location_el else None

            job = Job(
                source="ziprecruiter",
                external_id=job_id,
                title=title_text,
                company=company_text,
                location=location_text,
                url=f"https://www.ziprecruiter.com/ojob/view/{job_id}",
                status="new",
            )
            text = f"{job.title} {job.company}".lower()
            if any(kw.lower() in text for kw in criteria.keywords_excluded):
                continue
            yield job

    async def get_job_detail(self, job: Job) -> Job:
        client = self._http_client
        assert client is not None
        try:
            resp = await client.get(job.url)
            if resp.status_code == 200:
                # Extract description text — strip HTML tags crudely
                text = re.sub(r"<[^>]+>", " ", resp.text)
                text = re.sub(r"\s+", " ", text)
                # Find the description section (heuristic)
                m = re.search(r"Job Description(.{100,5000}?)(?:Apply Now|Similar Jobs)", text)
                if m:
                    job.description = m.group(1).strip()
                else:
                    job.description = text[:3000]
        except Exception as e:
            console.print(f"  [yellow]ZipRecruiter detail fetch error: {e}[/yellow]")
        return job

    async def apply_easy(self, job: Job, cover_letter: str) -> bool:
        """
        ZipRecruiter 1-Click Apply.
        Requires a logged-in Playwright session with an uploaded resume.
        Jobs that redirect to external ATS are flagged for manual review.
        """
        if not self._page:
            console.print("[yellow]ZipRecruiter: no browser session for apply[/yellow]")
            return False

        try:
            await self._page.goto(job.url)
            await self._delay()

            # Check for external redirect
            current_url = self._page.url
            if "ziprecruiter.com" not in current_url:
                console.print(
                    f"  [yellow]ZipRecruiter: external ATS redirect ({current_url}), "
                    f"flagging for manual review[/yellow]"
                )
                return False

            apply_btn = await self._page.query_selector(
                "a[data-goal='ApplyStart'], button.apply_button, .apply-link"
            )
            if not apply_btn:
                console.print(f"  [yellow]Apply button not found on {job.url}[/yellow]")
                return False

            await apply_btn.hover()
            await asyncio.sleep(0.3)
            await apply_btn.click()
            await asyncio.sleep(2)

            # ZipRecruiter's 1-click apply either submits immediately or shows a brief confirmation
            confirm_btn = await self._page.query_selector(
                "button[data-goal='Submit'], button.one-click-apply"
            )
            if confirm_btn:
                await confirm_btn.click()
                await asyncio.sleep(2)

            console.print(f"  [green]Applied via ZipRecruiter 1-click: {job.title}[/green]")
            return True

        except Exception as e:
            console.print(f"  [red]ZipRecruiter apply error: {e}[/red]")
            return False
