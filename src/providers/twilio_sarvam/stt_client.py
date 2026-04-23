import asyncio
import base64
import json
import logging
import time
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
        sample_rate: int = 8000,  # telephony — matches Twilio's 8 kHz mulaw stream
    ):
        self.language = language
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.ws = None
        self._is_open = False
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()
        # Buffer ~200ms of audio (3200 bytes PCM s16le @ 8kHz) per STT message.
        # Twilio sends 20ms frames; sending each one individually as a WAV
        # file is both wasteful and too short for accurate recognition.
        self._buffer_target_bytes = int(sample_rate * 2 * 0.2)
        # Hard cap: if buffer grows beyond 4s (silence never broke the size
        # threshold for whatever reason), force a flush anyway.
        self._buffer_max_bytes = int(sample_rate * 2 * 4.0)
        self._audio_buffer = bytearray()
        # Timeout-based flush: if we've held audio for > 300ms without the
        # size threshold triggering a flush (user paused mid-sentence), push
        # it to Sarvam so VAD can still fire speech_end.
        self._last_flush_time = 0.0
        self._flush_timeout_sec = 0.3
        self._receive_loop_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self._is_open and self.ws is not None

    async def connect(self, open_timeout: float = 5.0, retries: int = 2) -> None:
        """Open WebSocket with bounded timeout and retry."""
        url = (
            f"{self.WS_URL}"
            f"?language-code={self.language}"
            f"&model={self.model}"
            f"&sample_rate={self.sample_rate}"
            f"&vad_signals=true"
            f"&high_vad_sensitivity=true"
        )
        headers = {"api-subscription-key": self.api_key}
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            try:
                self.ws = await websockets.connect(
                    url,
                    additional_headers=headers,
                    open_timeout=open_timeout,
                )
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    f"Sarvam STT connect attempt {attempt} failed: {e!r}"
                )
                if attempt < retries + 1:
                    await asyncio.sleep(0.3 * attempt)
        else:
            raise last_error or TimeoutError("STT connect failed")

        self._is_open = True
        logger.info(
            f"Sarvam STT connected: lang={self.language}, model={self.model}, "
            f"sr={self.sample_rate}Hz, vad=on"
        )
        self._receive_loop_task = asyncio.create_task(self._receive_loop())
        # No ping loop for STT — Sarvam STT doesn't support ping messages,
        # and the constant Twilio audio stream keeps the connection alive.

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
                    logger.info(f"STT raw event: {event_type} -> {str(event)[:200]}")

                # Normalize Sarvam's VAD signals into a flat form the agent
                # loop can pattern-match on.
                # Sarvam emits: {"type":"events","data":{"signal_type":"START_SPEECH"|"END_SPEECH"}}
                if event_type == "events":
                    signal = (event.get("data") or {}).get("signal_type", "")
                    if signal == "START_SPEECH":
                        await self._event_queue.put({"type": "speech_start"})
                        continue
                    if signal == "END_SPEECH":
                        await self._event_queue.put({"type": "speech_end"})
                        continue
                    # Unknown signal — pass through for logging/inspection.

                await self._event_queue.put(event)
        except websockets.ConnectionClosed as e:
            logger.info(f"Sarvam STT connection closed: code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"STT receive error: {e}", exc_info=True)
        finally:
            self._is_open = False

    async def send_audio(self, pcm_audio: bytes) -> None:
        """
        Append PCM s16le mono audio to the buffer. Flush to Sarvam STT:
          - once buffer reaches ~200ms of audio (normal case), OR
          - if we've held audio for > 300ms without flushing (user paused), OR
          - if buffer grows beyond the hard cap (safety net).
        """
        if not self.is_open or not pcm_audio:
            return

        self._audio_buffer.extend(pcm_audio)
        now = time.monotonic()
        if not self._last_flush_time:
            self._last_flush_time = now

        size_trigger = len(self._audio_buffer) >= self._buffer_target_bytes
        time_trigger = (
            self._audio_buffer
            and (now - self._last_flush_time) >= self._flush_timeout_sec
        )
        overflow_trigger = len(self._audio_buffer) >= self._buffer_max_bytes

        if size_trigger or time_trigger or overflow_trigger:
            await self._flush_buffer()
            self._last_flush_time = now

    async def _flush_buffer(self) -> None:
        """Wrap the buffered PCM in a WAV container and send to Sarvam."""
        if not self._audio_buffer or not self.is_open:
            return
        pcm = bytes(self._audio_buffer)
        self._audio_buffer.clear()
        try:
            wav_bytes = _wrap_pcm_as_wav(pcm, self.sample_rate)
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
        # Flush any buffered audio so a final transcript can still come through.
        if self._is_open and self._audio_buffer:
            try:
                await self._flush_buffer()
            except Exception as e:
                logger.debug(f"STT flush on close: {e}")
        self._is_open = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"STT close: {e}")
            logger.info("Sarvam STT disconnected")

        # Wait for the receive loop to exit so we don't leak the task.
        if self._receive_loop_task and not self._receive_loop_task.done():
            try:
                await asyncio.wait_for(self._receive_loop_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._receive_loop_task.cancel()
            except Exception:
                pass
