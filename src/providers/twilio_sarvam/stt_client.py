import asyncio
import base64
import json
import logging
from typing import AsyncGenerator, Optional

import websockets


def _wrap_pcm_as_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM s16le mono data in a minimal WAV header."""
    data_size = len(pcm)
    file_size = data_size + 36
    byte_rate = sample_rate * 2  # 2 bytes per sample, mono
    header = (
        b"RIFF"
        + file_size.to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")     # fmt chunk size
        + (1).to_bytes(2, "little")      # PCM format
        + (1).to_bytes(2, "little")      # mono
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (2).to_bytes(2, "little")      # block align
        + (16).to_bytes(2, "little")     # bits per sample
        + b"data"
        + data_size.to_bytes(4, "little")
    )
    return header + pcm

logger = logging.getLogger(__name__)

PING_INTERVAL_SEC = 30


class SarvamSTTClient:
    """
    Streaming speech-to-text via Sarvam AI WebSocket.

    Protocol: https://docs.sarvam.ai/api-reference-docs/speech-to-text-streaming/transcribe/ws
      → Client sends: {"audio": {"data": "<b64-pcm>", "sample_rate": 8000, "encoding": "audio/pcm"}}
      → Server sends: {"type":"data","data":{"transcript":"...","request_id":"..."}}
                      {"type":"speech_start"} / {"type":"speech_end"}  (when vad_signals=true)
    """

    WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"

    def __init__(
        self,
        language: str,
        api_key: str,
        model: str = "saaras:v3",
        sample_rate: int = 8000,
    ):
        self.language = language
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.ws = None
        self._is_open = False
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self._is_open and self.ws is not None

    async def connect(self) -> None:
        """Open WebSocket with query params + auth header."""
        url = (
            f"{self.WS_URL}"
            f"?language-code={self.language}"
            f"&model={self.model}"
            f"&sample_rate={self.sample_rate}"
            f"&vad_signals=true"
            f"&high_vad_sensitivity=true"
        )
        headers = {"api-subscription-key": self.api_key}
        self.ws = await websockets.connect(url, additional_headers=headers)
        self._is_open = True
        logger.info(
            f"Sarvam STT connected: lang={self.language}, model={self.model}, "
            f"sr={self.sample_rate}Hz, vad=on"
        )
        asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self) -> None:
        """Keep the connection alive."""
        try:
            while self._is_open:
                await asyncio.sleep(PING_INTERVAL_SEC)
                if not self._is_open or not self.ws:
                    break
                try:
                    await self.ws.send(json.dumps({"type": "ping"}))
                except websockets.ConnectionClosed:
                    break
                except Exception as e:
                    logger.debug(f"STT ping error: {e}")
                    break
        except asyncio.CancelledError:
            pass

    async def _receive_loop(self) -> None:
        """Background task to receive STT events and push to the queue."""
        try:
            async for message in self.ws:
                if not isinstance(message, str):
                    logger.warning(f"STT: unexpected binary frame ({len(message)}B)")
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"STT: non-JSON message: {message[:100]}")
                    continue

                event_type = event.get("type")
                if event_type == "error":
                    logger.error(f"STT ERROR from Sarvam: {event}")
                else:
                    logger.debug(f"STT event: {event_type} -> {str(event)[:200]}")
                await self._event_queue.put(event)
        except websockets.ConnectionClosed as e:
            logger.info(f"Sarvam STT connection closed: code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"STT receive error: {e}", exc_info=True)
        finally:
            self._is_open = False

    async def send_audio(self, pcm_audio: bytes) -> None:
        """
        Send a chunk of PCM s16le mono audio to Sarvam STT.
        Each chunk is wrapped in a minimal WAV header (Sarvam requires audio/wav).
        """
        if not self.is_open:
            return
        try:
            wav_bytes = _wrap_pcm_as_wav(pcm_audio, self.sample_rate)
            b64 = base64.b64encode(wav_bytes).decode("utf-8")
            message = {
                "audio": {
                    "data": b64,
                    "sample_rate": self.sample_rate,
                    "encoding": "audio/wav",
                }
            }
            await self.ws.send(json.dumps(message))
        except websockets.ConnectionClosed:
            self._is_open = False
        except Exception as e:
            logger.error(f"STT send error: {e}")

    async def receive_events(self) -> AsyncGenerator[dict, None]:
        """Yield STT events (speech_start, speech_end, data/transcript)."""
        while True:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                if not self._is_open:
                    return
                continue

    async def close(self) -> None:
        self._is_open = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"STT close: {e}")
            logger.info("Sarvam STT disconnected")
