from __future__ import annotations

import httpx
from rich.console import Console

console = Console()


def ollama_chat_vision(
    system: str,
    user: str,
    image_b64: str,
    model: str,
    base_url: str,
    max_tokens: int = 512,
    retries: int = 2,
) -> str:
    """
    Call Ollama with a vision-capable model (e.g. qwen3.5).
    image_b64 is a base64-encoded PNG/JPEG string (no data-URI prefix).
    Retries on empty response (can happen when thinking mode produces no output).
    """
    url = base_url.rstrip("/") + "/api/chat"
    # Merge system into user for better vision model compatibility
    combined_user = f"{system}\n\n{user}"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": combined_user,
                "images": [image_b64],
            },
        ],
        "stream": False,
        "think": False,  # disable extended thinking — produces empty content
        "options": {"num_predict": max_tokens},
    }
    console.print(f"  [cyan]👁️  Vision ({model})[/cyan]")
    last_err = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=180.0) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
            content = resp.json()["message"]["content"].strip()
            if content:
                return content
            # Empty response — retry without think:False in case model ignores it
            payload["think"] = True
            last_err = ValueError("empty response from vision model")
        except Exception as e:
            last_err = e
    raise last_err


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
    console.print(f"  [cyan]🤖 Qwen ({model})[/cyan]")
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
