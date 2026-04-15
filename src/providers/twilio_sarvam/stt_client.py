import json
import asyncio
import logging
from typing import AsyncGenerator, Optional

import websockets

logger = logging.getLogger(__name__)


class SarvamSTTClient:
    """Streaming speech-to-text via Sarvam AI WebSocket."""

    WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"

    def __init__(self, language: str, api_key: str, model: str = "saaras:v3"):
        self.language = language
        self.api_key = api_key
        self.model = model
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._transcript_queue: asyncio.Queue[dict] = asyncio.Queue()

    async def connect(self) -> None:
        """Open WebSocket connection to Sarvam STT."""
        url = (
            f"{self.WS_URL}"
            f"?language-code={self.language}"
            f"&model={self.model}"
        )
        headers = {"api-subscription-key": self.api_key}
        self.ws = await websockets.connect(url, additional_headers=headers)
        logger.info(f"Sarvam STT connected: lang={self.language}, model={self.model}")
        asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        """Background task to receive STT events and put them in the queue."""
        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    await self._transcript_queue.put(event)
                except json.JSONDecodeError:
                    logger.warning(f"STT: non-JSON message received: {message[:100]}")
        except websockets.ConnectionClosed:
            logger.info("Sarvam STT connection closed")
        except Exception as e:
            logger.error(f"STT receive error: {e}")

    async def send_audio(self, pcm_audio: bytes) -> None:
        """Send PCM audio chunk (s16le, 8kHz) to Sarvam STT."""
        if self.ws and self.ws.open:
            await self.ws.send(pcm_audio)

    async def receive_events(self) -> AsyncGenerator[dict, None]:
        """Yield STT events (transcript, speech_start, speech_end) as they arrive."""
        while True:
            try:
                event = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=0.1
                )
                yield event
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        """Close the STT WebSocket connection."""
        if self.ws and self.ws.open:
            await self.ws.close()
            logger.info("Sarvam STT disconnected")
