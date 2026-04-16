import json
import logging
from typing import AsyncGenerator, List, Optional

import httpx

logger = logging.getLogger(__name__)

PROVIDER_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "sarvam": "https://api.sarvam.ai/v1/chat/completions",
}

# Fast "instant" models for latency-critical turns (greeting, short replies).
FAST_MODELS = {
    "groq": "llama-3.1-8b-instant",
    "sarvam": "sarvam-m",
}


class LLMClient:
    """
    Streaming LLM client supporting Groq and Sarvam (OpenAI-compatible APIs).

    Uses a single persistent httpx.AsyncClient for connection pooling + HTTP/2.
    Supports two-tier model selection: fast model for greetings/short turns,
    main model for complex conversation.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        fast_model: Optional[str] = None,
    ):
        self.provider = provider
        self.model = model
        self.fast_model = fast_model or FAST_MODELS.get(provider, model)
        self.api_key = api_key
        self.url = PROVIDER_URLS.get(provider, PROVIDER_URLS["groq"])

        # Persistent client with HTTP/2 + keep-alive for connection reuse.
        # This alone cuts ~50-100ms per request by avoiding TLS handshake.
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=50,
                keepalive_expiry=300.0,
            ),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def warmup(self) -> None:
        """Pre-open the HTTP connection to skip TLS handshake on first real call."""
        try:
            await self._client.head(self.url.replace("/chat/completions", "/models"))
        except Exception:
            pass

    async def chat_completion_stream(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 256,
        fast: bool = False,
    ) -> AsyncGenerator[str, None]:
        """
        Stream chat completion tokens via SSE. Yields text chunks.

        If fast=True, uses the faster instant model (lower TTFT, worse quality).
        Use for greetings, confirmations, and short acknowledgements.
        """
        model = self.fast_model if fast else self.model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self._client.stream("POST", self.url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    async def chat_completion(
        self,
        messages: List[dict],
        temperature: float = 0.7,
        max_tokens: int = 256,
        fast: bool = False,
    ) -> str:
        """Non-streaming chat completion. Returns full response text."""
        model = self.fast_model if fast else self.model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        response = await self._client.post(self.url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
