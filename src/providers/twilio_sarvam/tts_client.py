import json
import asyncio
import logging
from typing import AsyncGenerator, Optional

import websockets

logger = logging.getLogger(__name__)


class SarvamTTSClient:
    """Streaming text-to-speech via Sarvam AI WebSocket."""

    WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

    def __init__(self, language: str, voice: str, api_key: str):
        self.language = language
        self.voice = voice
        self.api_key = api_key
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._cancelled = False

    async def connect(self) -> None:
        """Open WebSocket connection and send initial config."""
        headers = {"api-subscription-key": self.api_key}
        self.ws = await websockets.connect(self.WS_URL, additional_headers=headers)

        config = {
            "type": "config",
            "data": {
                "model": "bulbul:v3",
                "target_language_code": self.language,
                "speaker": self.voice,
                "encoding": "mulaw",
                "sample_rate": 8000,
            },
        }
        await self.ws.send(json.dumps(config))
        logger.info(f"Sarvam TTS connected: lang={self.language}, voice={self.voice}")
        asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        """Background task to receive TTS audio chunks."""
        try:
            async for message in self.ws:
                if self._cancelled:
                    continue
                if isinstance(message, bytes):
                    await self._audio_queue.put(message)
                else:
                    try:
                        event = json.loads(message)
                        if event.get("type") == "end":
                            await self._audio_queue.put(None)
                    except json.JSONDecodeError:
                        pass
        except websockets.ConnectionClosed:
            logger.info("Sarvam TTS connection closed")
        except Exception as e:
            logger.error(f"TTS receive error: {e}")

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Send text to TTS and yield mulaw audio chunks as they stream back.
        Audio is mulaw 8kHz — ready to send directly to Twilio.
        """
        if not self.ws or not self.ws.open:
            return

        self._cancelled = False

        text_msg = {"type": "text", "data": {"text": text}}
        await self.ws.send(json.dumps(text_msg))

        flush_msg = {"type": "flush"}
        await self.ws.send(json.dumps(flush_msg))

        while True:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=5.0)
                if chunk is None:
                    break
                if self._cancelled:
                    break
                yield chunk
            except asyncio.TimeoutError:
                break

    def cancel(self) -> None:
        """Cancel current TTS generation (for barge-in)."""
        self._cancelled = True
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def close(self) -> None:
        """Close the TTS WebSocket connection."""
        if self.ws and self.ws.open:
            await self.ws.close()
            logger.info("Sarvam TTS disconnected")
