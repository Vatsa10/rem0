import hashlib
import logging
import os
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class GreetingCache:
    """
    Redis-backed cache for pre-synthesized greeting audio.

    Stores mulaw 8kHz audio keyed by (language, voice, time_period, company, agent).
    - `voice` differentiation prevents a male/female mismatch.
    - `time_period` differentiation prevents serving a "good morning" greeting
      at 8 PM. Typical values: morning / afternoon / evening / late.
    """

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self.url = url
        self._client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        if self._client is None:
            try:
                self._client = redis.from_url(
                    self.url,
                    decode_responses=False,
                    socket_keepalive=True,
                )
                await self._client.ping()
                logger.info(f"Greeting cache connected: {self.url}")
            except Exception as e:
                logger.warning(f"Greeting cache unavailable: {e}")
                self._client = None

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _key(
        self, language: str, voice: str, time_period: str, company: str, agent: str
    ) -> str:
        h = hashlib.md5(f"{company}|{agent}".encode()).hexdigest()[:8]
        return f"greeting:v3:{language}:{voice}:{time_period}:{h}"

    async def get(
        self, language: str, voice: str, time_period: str, company: str, agent: str
    ) -> Optional[bytes]:
        """Get cached greeting audio (mulaw 8kHz). Returns None if not cached."""
        if not self._client:
            return None
        try:
            return await self._client.get(
                self._key(language, voice, time_period, company, agent)
            )
        except Exception as e:
            logger.warning(f"Cache get failed: {e}")
            return None

    async def set(
        self, language: str, voice: str, time_period: str, company: str, agent: str,
        audio: bytes, ttl_seconds: int = 86400 * 7,
    ) -> None:
        """Cache greeting audio with 7-day TTL by default."""
        if not self._client:
            return
        try:
            await self._client.set(
                self._key(language, voice, time_period, company, agent),
                audio,
                ex=int(ttl_seconds),
            )
            logger.info(
                f"Cached greeting: lang={language}, voice={voice}, "
                f"period={time_period}, bytes={len(audio)}"
            )
        except Exception as e:
            logger.warning(f"Cache set failed: {e}")

    async def get_text(
        self, language: str, voice: str, time_period: str, company: str, agent: str
    ) -> Optional[str]:
        """Get the text that was used to generate the cached greeting."""
        if not self._client:
            return None
        try:
            key = self._key(language, voice, time_period, company, agent) + ":text"
            val = await self._client.get(key)
            return val.decode("utf-8") if val else None
        except Exception:
            return None

    async def set_text(
        self, language: str, voice: str, time_period: str, company: str, agent: str,
        text: str, ttl_seconds: int = 86400 * 7,
    ) -> None:
        if not self._client:
            return
        try:
            key = self._key(language, voice, time_period, company, agent) + ":text"
            await self._client.set(key, text.encode("utf-8"), ex=int(ttl_seconds))
        except Exception:
            pass

    async def invalidate_all(self) -> None:
        """Invalidate all greeting cache entries (e.g., on settings change)."""
        if not self._client:
            return
        try:
            async for key in self._client.scan_iter(match="greeting:*"):
                await self._client.delete(key)
            logger.info("Greeting cache invalidated")
        except Exception as e:
            logger.warning(f"Cache invalidate failed: {e}")


_cache_singleton: Optional[GreetingCache] = None


def get_greeting_cache() -> GreetingCache:
    """Get the global greeting cache singleton."""
    global _cache_singleton
    if _cache_singleton is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        _cache_singleton = GreetingCache(url=url)
    return _cache_singleton
