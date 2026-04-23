import asyncio
import base64
import json
import logging
from typing import AsyncGenerator, Optional

import websockets

logger = logging.getLogger(__name__)

PING_INTERVAL_SEC = 30


class SarvamTTSClient:
    """
    Streaming text-to-speech via Sarvam AI WebSocket.

    Protocol: https://docs.sarvam.ai/api-reference-docs/text-to-speech-streaming/stream
      → Client sends: config, text, flush, ping (all JSON)
      → Server sends: {"type":"audio","data":{"audio":"<base64-mulaw>"}}  (audio chunks)
                      {"type":"events","data":{"event_type":"final", ...}} (completion)
    """

    WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"

    def __init__(self, language: str, voice: str, api_key: str):
        self.language = language
        self.voice = voice
        self.api_key = api_key
        self.ws = None
        self._is_open = False
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._cancelled = False
        self._ping_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self._is_open and self.ws is not None

    async def connect(self, open_timeout: float = 5.0, retries: int = 2) -> None:
        """
        Open WebSocket connection with bounded timeout and retry.

        Raises TimeoutError / websockets.WebSocketException after all retries fail,
        so callers can react (skip greeting / end call / mark invalid).
        """
        headers = {"api-subscription-key": self.api_key}
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 2):  # retries=2 → 3 attempts total
            try:
                self.ws = await websockets.connect(
                    self.WS_URL,
                    additional_headers=headers,
                    open_timeout=open_timeout,
                )
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Sarvam TTS connect attempt {attempt} failed: {e!r}"
                )
                if attempt < retries + 1:
                    await asyncio.sleep(0.3 * attempt)  # small backoff
        else:
            # for/else runs only if loop completed without break
            raise last_error or TimeoutError("TTS connect failed")

        self._is_open = True

        # Sarvam TTS WS config — the WS endpoint uses bulbul:v2 by default
        # (the model field is ignored). bulbul:v2 supports these voices:
        # Female: anushka, manisha, vidya, arya
        # Male:   abhilash, hitesh, karun
        config = {
            "type": "config",
            "data": {
                "target_language_code": self.language,
                "speaker": self.voice,
                "output_audio_codec": "mulaw",
                "speech_sample_rate": "8000",
                "min_buffer_size": 50,
                "send_completion_event": True,
            },
        }
        await self.ws.send(json.dumps(config))
        logger.info(
            f"Sarvam TTS connected: lang={self.language}, voice={self.voice}, "
            f"codec=mulaw@8kHz"
        )
        asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def _ping_loop(self) -> None:
        """Keep the connection alive — Sarvam closes idle connections after ~60s."""
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
                    logger.debug(f"TTS ping error: {e}")
                    break
        except asyncio.CancelledError:
            pass

    async def _receive_loop(self) -> None:
        """Consume messages from Sarvam TTS and put audio bytes on the queue."""
        try:
            async for message in self.ws:
                if self._cancelled:
                    continue

                # All Sarvam TTS messages are JSON text frames.
                if not isinstance(message, str):
                    logger.warning(f"TTS: unexpected binary frame ({len(message)}B)")
                    continue

                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(f"TTS: non-JSON message: {message[:100]}")
                    continue

                event_type = event.get("type")

                if event_type == "audio":
                    b64 = event.get("data", {}).get("audio", "")
                    if b64:
                        try:
                            audio_bytes = base64.b64decode(b64)
                            await self._audio_queue.put(audio_bytes)
                        except Exception as e:
                            logger.error(f"TTS: failed to decode audio: {e}")

                elif event_type == "events":
                    inner_type = event.get("data", {}).get("event_type")
                    if inner_type == "final":
                        logger.debug("TTS: completion event (final)")
                        await self._audio_queue.put(None)

                elif event_type == "error":
                    err = event.get("data") or event.get("message") or event
                    logger.error(f"TTS ERROR from Sarvam: {err}")
                    await self._audio_queue.put(None)

                else:
                    # Log anything unexpected so we can see it.
                    logger.debug(f"TTS event: {event_type} -> {str(event)[:200]}")

        except websockets.ConnectionClosed as e:
            logger.info(f"Sarvam TTS connection closed: code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"TTS receive error: {e}", exc_info=True)
        finally:
            self._is_open = False
            # Unblock any awaiting synthesize() calls.
            try:
                self._audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        Send text to TTS; yield mulaw 8kHz audio chunks as they stream back.

        Strategy: wait up to FIRST_CHUNK_TIMEOUT for the first audio chunk,
        then use a short INACTIVITY_TIMEOUT between subsequent chunks.
        Sarvam sends audio in rapid bursts — a gap means the burst is done.
        """
        # Skip empty / whitespace / non-language text (Sarvam rejects these).
        clean = (text or "").strip()
        if not clean or not any(c.isalnum() for c in clean):
            return

        if not self.is_open:
            logger.warning("TTS synthesize: not open, skipping")
            return

        self._cancelled = False

        try:
            await self.ws.send(json.dumps({"type": "text", "data": {"text": clean}}))
            await self.ws.send(json.dumps({"type": "flush"}))
        except websockets.ConnectionClosed:
            self._is_open = False
            return
        except Exception as e:
            logger.error(f"TTS send error: {e}")
            return

        FIRST_CHUNK_TIMEOUT = 5.0    # allow some cold-start latency
        INACTIVITY_TIMEOUT = 0.4     # Sarvam streams fast — 400ms gap = done

        chunks_yielded = 0
        bytes_yielded = 0
        timeout = FIRST_CHUNK_TIMEOUT

        while True:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=timeout)
                if chunk is None:
                    break
                if self._cancelled:
                    break
                chunks_yielded += 1
                bytes_yielded += len(chunk)
                yield chunk
                timeout = INACTIVITY_TIMEOUT
            except asyncio.TimeoutError:
                if chunks_yielded == 0:
                    logger.warning(f"TTS: no audio in {timeout}s for text {clean!r}")
                break

        logger.debug(
            f"TTS synthesize complete: {chunks_yielded} chunks, {bytes_yielded}B"
        )

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
        self._is_open = False
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"TTS close: {e}")
            logger.info("Sarvam TTS disconnected")
