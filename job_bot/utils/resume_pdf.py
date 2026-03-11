from __future__ import annotations

import asyncio
from pathlib import Path

from rich.console import Console

console = Console()

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Helvetica Neue', Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.45;
    color: #111;
    max-width: 780px;
    margin: 0 auto;
    padding: 32px 40px;
}
h1 {
    font-size: 20pt;
    font-weight: 700;
    margin-bottom: 2px;
    text-transform: uppercase;
    letter-spacing: 1px;
}
h1 + p {
    font-size: 9.5pt;
    color: #444;
    margin-bottom: 14px;
    border-bottom: 1.5px solid #111;
    padding-bottom: 6px;
}
h2 {
    font-size: 11pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-top: 14px;
    margin-bottom: 4px;
    border-bottom: 0.5px solid #999;
    padding-bottom: 2px;
}
h3 {
    font-size: 10.5pt;
    font-weight: 600;
    margin-top: 8px;
    margin-bottom: 1px;
}
ul {
    padding-left: 18px;
    margin-bottom: 6px;
}
li {
    margin-bottom: 2px;
}
p {
    margin-bottom: 5px;
}
strong { font-weight: 600; }
em { font-style: italic; }
"""


async def _render_pdf(html: str, output_path: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        await page.pdf(
            path=str(output_path),
            format="Letter",
            margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
            print_background=False,
        )
        await browser.close()


def build_resume_pdf(resume_md_path: str, output_pdf_path: str) -> str:
    """
    Convert resume.md → resume.pdf using markdown-it-py + Playwright.
    Only re-renders if the .md is newer than the existing .pdf.
    Returns the absolute path to the PDF.
    """
    from markdown_it import MarkdownIt

    md_path = Path(resume_md_path)
    pdf_path = Path(output_pdf_path)

    # Only regenerate if md is newer or pdf missing
    if pdf_path.exists() and pdf_path.stat().st_mtime >= md_path.stat().st_mtime:
        return str(pdf_path.resolve())

    console.print("  [dim]Rendering resume.pdf from resume.md...[/dim]")
    md = MarkdownIt()
    body_html = md.render(md_path.read_text())
    full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>{body_html}</body></html>"

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_render_pdf(full_html, pdf_path))
    console.print(f"  [green]resume.pdf written → {pdf_path}[/green]")
    return str(pdf_path.resolve())
