import asyncio
import base64
import json
import logging
import time
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

    def __init__(
        self,
        language: str,
        voice: str,
        api_key: str,
        model: str = "bulbul:v3",
    ):
        self.language = language
        self.voice = voice
        self.api_key = api_key
        self.model = model
        self.ws = None
        self._is_open = False
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._cancelled = False
        # When False, _receive_loop drops incoming audio chunks on the floor
        # instead of queuing them. Flipped False on cancel() (so stale chunks
        # from a cancelled text don't leak into the next synthesize) and
        # flipped back True at the start of the next synthesize() after a
        # brief drain window.
        self._accepting_audio = True
        self._ping_task: Optional[asyncio.Task] = None
        self._receive_loop_task: Optional[asyncio.Task] = None

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
        # Model is selected via URL query param (the in-config `model` field
        # is silently ignored by the WS endpoint — see probe results).
        url = f"{self.WS_URL}?model={self.model}"
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 2):  # retries=2 → 3 attempts total
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
                    f"Sarvam TTS connect attempt {attempt} failed: {e!r}"
                )
                if attempt < retries + 1:
                    await asyncio.sleep(0.3 * attempt)  # small backoff
        else:
            # for/else runs only if loop completed without break
            raise last_error or TimeoutError("TTS connect failed")

        self._is_open = True

        # Sarvam TTS WS config. Model (bulbul:v3) is set via URL query param
        # above — the `model` field in the config body is silently ignored
        # by the WS endpoint. v3 supports 40+ voices (rohan, aditya, ritu,
        # priya, neha, etc.) and uses `temperature` (0.01–1.0) for control.
        #
        # Audio format — telephony-optimized for Twilio:
        #   output_audio_codec = "mulaw" — Twilio's native codec (zero conversion)
        #   speech_sample_rate = "8000"  — telephony standard, matches Twilio's
        #                                   mulaw stream byte-for-byte
        # Docs: https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/
        #       text-to-speech/how-to/set-the-sample-rate
        # Higher rates (16k/22.05k/24k) would sound crisper in HiFi but Twilio
        # resamples to 8k mulaw anyway — so we'd burn TTS cost + downsampling
        # artifacts for no gain on a phone call.
        config = {
            "type": "config",
            "data": {
                "target_language_code": self.language,
                "speaker": self.voice,
                "output_audio_codec": "mulaw",
                "speech_sample_rate": "8000",
                "temperature": 0.5,
                # min_buffer_size omitted on purpose — Sarvam rejected
                # min_buffer_size=1 with 422 "Input parameters has to be a
                # valid dictionary", which closed the WS and left every
                # subsequent synthesize() bailing with "not open". Letting
                # Sarvam use its server-side default keeps the connection
                # healthy. If we ever need to tune it, valid range needs
                # to come from Sarvam docs first.
                "send_completion_event": True,
            },
        }
        await self.ws.send(json.dumps(config))
        logger.info(
            f"Sarvam TTS connected: lang={self.language}, voice={self.voice}, "
            f"model={self.model}, codec=mulaw@8kHz"
        )
        self._receive_loop_task = asyncio.create_task(self._receive_loop())
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
                    if not b64:
                        continue
                    # Drop stale chunks from a cancelled synthesis — they'd
                    # otherwise overlap with the next utterance's audio.
                    if not self._accepting_audio:
                        continue
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

        # If a previous synth was cancelled, let any in-flight stale audio
        # chunks land in _receive_loop and be dropped (because
        # _accepting_audio is still False). 100ms covers typical network
        # jitter for the tail end of a cancelled Sarvam response.
        if self._cancelled:
            await asyncio.sleep(0.10)

        # Final drain — catch anything that snuck in between the cancel
        # drain and now — then re-open the intake for this new synthesis.
        try:
            while True:
                self._audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._cancelled = False
        self._accepting_audio = True

        try:
            await self.ws.send(json.dumps({"type": "text", "data": {"text": clean}}))
            await self.ws.send(json.dumps({"type": "flush"}))
        except websockets.ConnectionClosed:
            self._is_open = False
            return
        except Exception as e:
            logger.error(f"TTS send error: {e}")
            return

        # First chunk allows model warmup. Inactivity between chunks must be
        # lenient — bulbul:v3 inserts natural pauses between phrases that
        # can exceed 1s on longer text. Trust `event_type:"final"` (enabled
        # via send_completion_event=True) for normal completion; inactivity
        # timeout is a hard safety net only.
        FIRST_CHUNK_TIMEOUT = 5.0
        INACTIVITY_TIMEOUT = 8.0
        # Even after `final` arrives, give the queue a brief drain window so
        # any audio chunks still in flight from the network land before we
        # exit the loop (belt-and-suspenders for ordering across the WS).
        POST_FINAL_DRAIN_SEC = 0.25

        chunks_yielded = 0
        bytes_yielded = 0
        timeout = FIRST_CHUNK_TIMEOUT
        end_reason = "unknown"
        t_start = time.monotonic()

        while True:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=timeout)
                if chunk is None:
                    end_reason = "final_event"
                    drain_until = time.monotonic() + POST_FINAL_DRAIN_SEC
                    while time.monotonic() < drain_until:
                        try:
                            extra = await asyncio.wait_for(
                                self._audio_queue.get(),
                                timeout=max(0.02, drain_until - time.monotonic()),
                            )
                        except asyncio.TimeoutError:
                            break
                        if extra is None:
                            continue
                        if self._cancelled:
                            break
                        chunks_yielded += 1
                        bytes_yielded += len(extra)
                        yield extra
                    break
                if self._cancelled:
                    end_reason = "cancelled"
                    break
                chunks_yielded += 1
                bytes_yielded += len(chunk)
                yield chunk
                timeout = INACTIVITY_TIMEOUT
            except asyncio.TimeoutError:
                if chunks_yielded == 0:
                    end_reason = "no_first_chunk"
                    logger.warning(f"TTS: no audio in {timeout}s for text {clean!r}")
                else:
                    end_reason = "inactivity_timeout"
                    logger.warning(
                        f"TTS: inactivity_timeout after {chunks_yielded} chunks "
                        f"({bytes_yielded}B) for text {clean!r} — tail likely "
                        f"truncated; raise INACTIVITY_TIMEOUT if frequent"
                    )
                break

        # Sanity check — if delivered audio is much shorter than expected for
        # the given text, that's a strong truncation signal worth a WARN.
        # Heuristic: ~3 wps fluent phone speech.
        words = max(1, len(clean.split()))
        expected_sec = words / 3.0
        actual_sec = bytes_yielded / 8000.0
        wall_sec = time.monotonic() - t_start
        if end_reason == "final_event" and actual_sec < 0.6 * expected_sec:
            logger.warning(
                f"TTS: short audio — actual {actual_sec:.2f}s vs expected "
                f"{expected_sec:.2f}s for {words}-word text {clean!r}. "
                f"Possible model/server-side truncation."
            )

        logger.info(
            f"TTS synthesize complete: chunks={chunks_yielded}, bytes={bytes_yielded}, "
            f"audio_sec={actual_sec:.2f}, expected_sec={expected_sec:.2f}, "
            f"wall_sec={wall_sec:.2f}, end_reason={end_reason}"
        )

    def cancel(self) -> None:
        """
        Cancel current TTS generation (for barge-in).

        Three things:
        1. _cancelled=True — synthesize()'s loop breaks on the next iteration.
        2. _accepting_audio=False — _receive_loop drops any chunks Sarvam
           keeps sending for the cancelled text (those would otherwise
           overlap with the next utterance's audio in the caller's ear).
        3. Put None on the queue — wakes synthesize() if it's blocked on
           queue.get().
        """
        self._cancelled = True
        self._accepting_audio = False
        # Drain anything already queued from the cancelled synth.
        try:
            while True:
                self._audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self._audio_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

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
        # Wait for the receive loop to exit so we don't leak the task.
        if self._receive_loop_task and not self._receive_loop_task.done():
            try:
                await asyncio.wait_for(self._receive_loop_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._receive_loop_task.cancel()
            except Exception:
                pass
