# Job Bot

Automated job scraping and application bot targeting LinkedIn Easy Apply and external ATS forms.

## Architecture

```mermaid
graph TB
    subgraph LI["🔵 LinkedIn"]
        LI1("Job Search\n(Easy Apply filter)")
        LI2("Easy Apply Modal\nMulti-step form walker")
        LI3("External Apply\nATS redirect handler")
    end

    subgraph PW["🎭 Playwright"]
        PW1("Persistent Browser Profile\nCookies survive restarts")
        PW2("Screenshot Capture\nAudit trail per submission")
        PW3("DOM Interaction\nFill · Click · Select")
    end

    subgraph AI["🤖 AI Model"]
        AI1("Qwen3.5 · 9.7B · Q4_K_M\nvia Ollama")
        AI2("Tailscale VPN\n100.99.x.x:11434")
        AI3("Vision Loop\nScreenshot → action decisions")
        AI4("Claude Opus 4.6\nFallback if Ollama down")
    end

    subgraph PIPE["⚙️ Pipeline"]
        P1["Scrape"]
        P2["Evaluate\nFit score 0–100"]
        P3["Cover Letter"]
        P4["Apply"]
    end

    LI1 -->|"25 jobs / search query"| P1
    P1 --> P2
    P2 -->|"score ≥ threshold"| P3
    P3 --> P4

    PW1 -->|"Authenticated session"| LI1
    PW3 -->|"Walk modal steps"| LI2
    PW3 -->|"Fill ATS forms"| LI3
    PW2 -->|"Pre-submit screenshot"| P4

    AI1 -->|"Job fit scoring"| P2
    AI1 -->|"Cover letter generation"| P3
    AI3 -->|"Next action JSON"| LI3
    PW2 -->|"Page screenshot"| AI3
    AI2 -.->|"Remote inference"| AI1
    AI4 -.->|"Fallback"| AI1
```

## Quick Start

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY + LinkedIn credentials
pip install -e .
playwright install chromium
python cli.py login linkedin  # save browser session
python cli.py run --dry-run   # test without applying
python cli.py run             # live run
```

## Commands

| Command | Description |
|---|---|
| `python cli.py run` | Full pipeline: scrape → evaluate → apply |
| `python cli.py run --dry-run` | Preview only, no applications submitted |
| `python cli.py run --skip-scrape` | Apply to already-evaluated jobs in DB |
| `python cli.py run --non-easy-apply` | Include external ATS jobs |
| `python cli.py login linkedin` | Authenticate and save browser session |
| `python cli.py report` | Show application statistics |
| `python cli.py jobs` | List jobs in database |
| `python cli.py clear --all` | Wipe database |
