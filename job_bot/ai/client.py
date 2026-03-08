from __future__ import annotations

import anthropic

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        from config.settings import settings
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client
