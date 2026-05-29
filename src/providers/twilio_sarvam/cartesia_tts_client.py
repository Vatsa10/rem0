import asyncio
import base64
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

import websockets

logger = logging.getLogger(__name__)

CARTESIA_VERSION = "2024-11-13"


class CartesiaTTSClient:
    """
    Streaming text-to-speech via Cartesia Sonic WebSocket.

    Telephony-native: output_format container="raw", encoding="pcm_mulaw",
    sample_rate=8000 → bytes are Twilio's native codec, zero conversion.

    Interface matches the previous SarvamTTSClient so the agent is unchanged:
      connect(), synthesize(text), synthesize_stream(text_iter),
      cancel(), close(), is_open, .voice.

    Barge-in: each utterance uses a fresh context_id. The receive loop only
    queues chunks whose context_id matches the ACTIVE context, so stale audio
    from a cancelled utterance is dropped cleanly (no overlap in the caller's
    ear). cancel() clears the active context + drains.

    Docs: https://docs.cartesia.ai/api-reference/tts/tts
    """

    WS_URL = "wss://api.cartesia.ai/tts/websocket"

    def __init__(
        self,
        language: str,
        voice: str,
        api_key: str,
        model: str = "sonic-2",
    ):
        self.language = language
        self.voice = voice  # Cartesia voice id (UUID)
        self.api_key = api_key
        self.model = model
        self.ws = None
        self._is_open = False
        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        # Only chunks tagged with this context_id are accepted; everything
        # else (stale/cancelled utterance) is dropped on the floor.
        self._active_context: Optional[str] = None
        self._cancelled = False
        self._receive_loop_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self._is_open and self.ws is not None

    async def connect(self, open_timeout: float = 5.0, retries: int = 2) -> None:
        url = f"{self.WS_URL}?api_key={self.api_key}&cartesia_version={CARTESIA_VERSION}"
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            try:
                self.ws = await websockets.connect(url, open_timeout=open_timeout)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Cartesia TTS connect attempt {attempt} failed: {e!r}")
                if attempt < retries + 1:
                    await asyncio.sleep(0.3 * attempt)
        else:
            raise last_error or TimeoutError("Cartesia TTS connect failed")

        self._is_open = True
        logger.info(
            f"Cartesia TTS connected: lang={self.language}, voice={self.voice}, "
            f"model={self.model}, codec=pcm_mulaw@8kHz"
        )
        self._receive_loop_task = asyncio.create_task(self._receive_loop())

    def _build_request(self, transcript: str, context_id: str, continue_: bool) -> dict:
        return {
            "model_id": self.model,
            "transcript": transcript,
            "voice": {"mode": "id", "id": self.voice},
            "language": self.language,
            "context_id": context_id,
            "continue": continue_,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_mulaw",
                "sample_rate": 8000,
            },
        }

    async def _receive_loop(self) -> None:
        try:
            async for message in self.ws:
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue

                ctx = event.get("context_id")
                etype = event.get("type")

                # Drop anything not from the active utterance (cancelled/stale).
                if self._cancelled or ctx != self._active_context:
                    continue

                if etype == "chunk":
                    b64 = event.get("data", "")
                    if not b64:
                        continue
                    try:
                        await self._audio_queue.put(base64.b64decode(b64))
                    except Exception as e:
                        logger.error(f"Cartesia: failed to decode chunk: {e}")
                elif etype == "done":
                    await self._audio_queue.put(None)
                elif etype == "error":
                    logger.error(f"Cartesia TTS error: {event.get('error') or event}")
                    await self._audio_queue.put(None)

        except websockets.ConnectionClosed as e:
            logger.info(f"Cartesia TTS closed: code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"Cartesia TTS receive error: {e}", exc_info=True)
        finally:
            self._is_open = False
            try:
                self._audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _reset_for_new_utterance(self) -> str:
        """Drain stale audio, mint a fresh context, return its id."""
        try:
            while True:
                self._audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._cancelled = False
        ctx = uuid.uuid4().hex
        self._active_context = ctx
        return ctx

    async def _drain_queue(
        self, *, words_hint: int, label: str
    ) -> AsyncGenerator[bytes, None]:
        """Shared audio read loop with first-chunk + inactivity timeouts."""
        FIRST_CHUNK_TIMEOUT = 5.0
        INACTIVITY_TIMEOUT = 8.0
        chunks_yielded = 0
        bytes_yielded = 0
        timeout = FIRST_CHUNK_TIMEOUT
        end_reason = "unknown"
        t_start = time.monotonic()
        while True:
            try:
                chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=timeout)
                if chunk is None:
                    end_reason = "done"
                    break
                if self._cancelled:
                    end_reason = "cancelled"
                    break
                chunks_yielded += 1
                bytes_yielded += len(chunk)
                yield chunk
                timeout = INACTIVITY_TIMEOUT
            except asyncio.TimeoutError:
                end_reason = "no_first_chunk" if chunks_yielded == 0 else "inactivity"
                logger.warning(
                    f"Cartesia {label}: {end_reason} after {chunks_yielded} chunks "
                    f"({bytes_yielded}B)"
                )
                break
        wall_sec = time.monotonic() - t_start
        logger.info(
            f"Cartesia {label} complete: chunks={chunks_yielded}, "
            f"bytes={bytes_yielded}, audio_sec={bytes_yielded / 8000.0:.2f}, "
            f"wall_sec={wall_sec:.2f}, end_reason={end_reason}"
        )

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        """One-shot synth: send full text, yield mulaw 8 kHz chunks."""
        clean = (text or "").strip()
        if not clean or not any(c.isalnum() for c in clean):
            return
        if not self.is_open:
            logger.warning("Cartesia synthesize: not open, skipping")
            return

        ctx = self._reset_for_new_utterance()
        try:
            await self.ws.send(json.dumps(self._build_request(clean, ctx, False)))
        except websockets.ConnectionClosed:
            self._is_open = False
            return
        except Exception as e:
            logger.error(f"Cartesia send error: {e}")
            return

        async for chunk in self._drain_queue(
            words_hint=len(clean.split()), label="synthesize"
        ):
            yield chunk

    async def synthesize_stream(
        self, text_iter: "AsyncGenerator[str, None]"
    ) -> AsyncGenerator[bytes, None]:
        """
        Pipelined synth: send text pieces (e.g. phrase chunks from a streaming
        LLM) into one context as they arrive, reading audio concurrently. First
        audio starts after the first phrase — the big latency win.
        """
        if not self.is_open:
            logger.warning("Cartesia synthesize_stream: not open, skipping")
            return

        ctx = self._reset_for_new_utterance()
        sent_any = {"v": False}

        async def feeder() -> None:
            try:
                async for piece in text_iter:
                    clean = (piece or "").strip()
                    if not clean or not any(c.isalnum() for c in clean):
                        continue
                    if self._cancelled or not self.is_open:
                        return
                    sent_any["v"] = True
                    await self.ws.send(
                        json.dumps(self._build_request(clean + " ", ctx, True))
                    )
                # Close the context so Cartesia flushes the tail audio.
                if sent_any["v"] and self.is_open and not self._cancelled:
                    await self.ws.send(json.dumps(self._build_request("", ctx, False)))
            except websockets.ConnectionClosed:
                self._is_open = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Cartesia stream feeder error: {e}")

        feed_task = asyncio.create_task(feeder())
        try:
            async for chunk in self._drain_queue(words_hint=0, label="stream"):
                yield chunk
        finally:
            if not feed_task.done():
                feed_task.cancel()

    def cancel(self) -> None:
        """Barge-in: drop the active utterance + drain queued audio."""
        self._cancelled = True
        cancelled_ctx = self._active_context
        self._active_context = None
        try:
            while True:
                self._audio_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self._audio_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        # Best-effort server-side cancel so Cartesia stops generating.
        if cancelled_ctx and self.ws and self._is_open:
            try:
                asyncio.create_task(
                    self.ws.send(json.dumps(
                        {"context_id": cancelled_ctx, "cancel": True}))
                )
            except Exception:
                pass

    async def close(self) -> None:
        self._is_open = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"Cartesia TTS close: {e}")
            logger.info("Cartesia TTS disconnected")
        if self._receive_loop_task and not self._receive_loop_task.done():
            try:
                await asyncio.wait_for(self._receive_loop_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._receive_loop_task.cancel()
            except Exception:
                pass
