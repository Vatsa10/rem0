import json
import logging
from typing import AsyncGenerator, List, Optional

import httpx

logger = logging.getLogger(__name__)

PROVIDER_URLS = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "sarvam": "https://api.sarvam.ai/v1/chat/completions",
}

FAST_MODELS = {
    "groq": "llama-3.1-8b-instant",
    "sarvam": "sarvam-m",
}


class LLMClient:
    """
    Streaming LLM client supporting Groq and Sarvam (OpenAI-compatible APIs).

    Uses a single persistent httpx.AsyncClient for connection pooling + HTTP/2.
    Falls back automatically from fast_model → model on 4xx errors.
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
        Stream tokens via SSE. Tries fast_model first if fast=True;
        falls back to main model on 4xx (e.g. bad model name).
        """
        # Build the list of models to try in order.
        if fast and self.fast_model and self.fast_model != self.model:
            models_to_try = [self.fast_model, self.model]
        else:
            models_to_try = [self.fast_model if fast else self.model]

        logger.info(f"LLM stream: trying models={models_to_try}")

        last_error: Optional[str] = None
        for model in models_to_try:
            yielded_any = False
            try:
                async for chunk in self._try_model(
                    model, messages, temperature, max_tokens
                ):
                    yielded_any = True
                    yield chunk
                if yielded_any:
                    return  # success
                # Model call completed but yielded nothing — try next.
                logger.warning(f"Model {model!r} returned empty stream")
            except _ModelNotAvailable as e:
                last_error = str(e)
                logger.warning(
                    f"Model {model!r} unavailable: {e}. "
                    f"Trying next model..."
                )
                continue
            except httpx.HTTPError as e:
                last_error = str(e)
                logger.error(f"LLM transport error on {model!r}: {e}")
                continue

        logger.error(f"All LLM models failed. Last error: {last_error}")

    async def _try_model(
        self,
        model: str,
        messages: List[dict],
        temperature: float,
        max_tokens: int,
    ) -> AsyncGenerator[str, None]:
        """
        Try one model. Raises _ModelNotAvailable on 4xx model errors
        (so the outer loop can fall back). Yields nothing and returns
        normally on empty responses.
        """
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with self._client.stream("POST", self.url, json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                logger.error(
                    f"LLM {response.status_code} for model={model!r}: {body[:300]}"
                )
                if response.status_code in (400, 404, 422):
                    raise _ModelNotAvailable(f"{response.status_code}: {body[:200]}")
                response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
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
        await self._client.aclose()


class _ModelNotAvailable(Exception):
    """Signals a model-specific 4xx error — outer loop should try the next model."""
