from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = Field(..., description="Anthropic API key")

    # LinkedIn credentials
    linkedin_email: str = Field("", description="LinkedIn login email")
    linkedin_password: str = Field("", description="LinkedIn login password")

    # Application behavior
    min_fit_score: int = Field(70, description="Minimum Claude fit score (0-100) to auto-apply")
    max_applications_per_day: int = Field(10, description="Hard cap on daily applications")
    dry_run: bool = Field(True, description="If True, evaluate but never submit applications")

    # Paths
    browser_profile_dir: str = Field(
        "~/.job_bot/browser_profiles",
        description="Directory to store persistent browser profiles",
    )
    search_criteria_path: str = Field(
        "config/search_criteria.yaml",
        description="Path to YAML file with search criteria",
    )
    resume_path: str = Field("data/resume.md", description="Path to resume markdown file")
    profile_path: str = Field("data/profile.yaml", description="Path to profile YAML file")
    db_path: str = Field("data/applications.db", description="Path to SQLite database")
    screenshots_dir: str = Field(
        "data/screenshots", description="Directory for pre-submit screenshots"
    )

    # Scraper behavior
    linkedin_max_jobs_per_session: int = Field(
        40, description="Max LinkedIn job cards to inspect per browser session"
    )
    request_delay_min: float = Field(2.0, description="Min seconds between page loads")
    request_delay_max: float = Field(7.0, description="Max seconds between page loads")

    # Ollama (primary AI)
    ollama_base_url: str = Field(
        "http://localhost:11434", description="Base URL for Ollama server (primary AI)"
    )
    ollama_model: str = Field("qwen3.5", description="Ollama model to use as primary AI")
    ollama_vision_model: str = Field("qwen3.5", description="Ollama vision model for screenshot-based decisions")

    # Claude fallback (used when Ollama is unavailable)
    evaluator_model: str = Field("claude-opus-4-6", description="Claude fallback model for job fit evaluation")
    cover_letter_model: str = Field(
        "claude-opus-4-6", description="Claude fallback model for cover letter generation"
    )


settings = Settings()  # type: ignore[call-arg]
