from __future__ import annotations

import httpx
from rich.console import Console

console = Console()


def ollama_chat(
    system: str,
    user: str,
    model: str,
    base_url: str,
    max_tokens: int = 1024,
) -> str:
    """
    Call Ollama's OpenAI-compatible chat completions endpoint.
    Ollama must be running and the model must be pulled on the target machine.
    """
    # Use Ollama's native API — it supports think:false unlike the OpenAI-compat endpoint
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,  # Disable Qwen3 extended thinking
        "options": {"num_predict": max_tokens},
    }
    console.print(f"  [cyan]🤖 Qwen3 ({model})[/cyan]")
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
    return resp.json()["message"]["content"]


def is_credit_error(exc: Exception) -> bool:
    """Return True if the Anthropic error is a credit/billing exhaustion error."""
    import anthropic

    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 402:
            return True
        msg = str(exc).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg:
            return True
    return False
