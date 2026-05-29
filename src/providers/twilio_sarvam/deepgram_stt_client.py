import asyncio
import json
import logging
from typing import AsyncGenerator, List, Optional

import websockets

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_SEC = 8.0


class DeepgramSTTClient:
    """
    Streaming speech-to-text via Deepgram WebSocket.

    Telephony-native: Deepgram consumes raw mulaw 8 kHz binary frames directly
    (encoding=mulaw&sample_rate=8000), so we forward Twilio's audio with NO
    PCM conversion or WAV wrapping — lower latency than the old Sarvam path.

    Interface matches the previous SarvamSTTClient so the agent is unchanged:
      connect(), send_audio(mulaw_bytes), receive_events(), close(), is_open.

    Emitted events (normalized for the agent loop):
      {"type": "speech_start"}                       (VAD SpeechStarted)
      {"type": "speech_end"}                         (UtteranceEnd)
      {"type": "data", "data": {"transcript", "is_final"}}

    To avoid the "STT cascade" (one utterance split into many finals), we
    accumulate Deepgram's is_final segments and emit ONE final transcript when
    speech_final / UtteranceEnd fires. Interim results pass through as
    is_final=False for barge-in detection.

    Docs: https://developers.deepgram.com/docs/streaming
    """

    WS_URL = "wss://api.deepgram.com/v1/listen"

    def __init__(
        self,
        language: str,
        api_key: str,
        model: str = "nova-2",
        sample_rate: int = 8000,
    ):
        self.language = language
        self.api_key = api_key
        self.model = model
        self.sample_rate = sample_rate
        self.ws = None
        self._is_open = False
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._final_segments: List[str] = []
        self._receive_loop_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None

    @property
    def is_open(self) -> bool:
        return self._is_open and self.ws is not None

    async def connect(self, open_timeout: float = 5.0, retries: int = 2) -> None:
        """Open the Deepgram WS with bounded timeout + retry."""
        # endpointing=300  → speech_final after 300ms silence (turn trigger)
        # utterance_end_ms → UtteranceEnd safety net if speech_final misses
        # vad_events=true  → SpeechStarted for fast barge-in
        # interim_results  → partial transcripts for barge-in
        params = (
            f"?model={self.model}"
            f"&language={self.language}"
            f"&encoding=mulaw"
            f"&sample_rate={self.sample_rate}"
            f"&channels=1"
            f"&interim_results=true"
            f"&vad_events=true"
            f"&endpointing=300"
            f"&utterance_end_ms=1000"
            f"&smart_format=true"
            f"&no_delay=true"
        )
        url = f"{self.WS_URL}{params}"
        headers = {"Authorization": f"Token {self.api_key}"}
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
                logger.warning(f"Deepgram STT connect attempt {attempt} failed: {e!r}")
                if attempt < retries + 1:
                    await asyncio.sleep(0.3 * attempt)
        else:
            raise last_error or TimeoutError("Deepgram STT connect failed")

        self._is_open = True
        logger.info(
            f"Deepgram STT connected: lang={self.language}, model={self.model}, "
            f"sr={self.sample_rate}Hz mulaw, vad=on"
        )
        self._receive_loop_task = asyncio.create_task(self._receive_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        """Deepgram closes the socket after ~10s of no audio; ping during silence."""
        try:
            while self._is_open:
                await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
                if not self._is_open or not self.ws:
                    break
                try:
                    await self.ws.send(json.dumps({"type": "KeepAlive"}))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _receive_loop(self) -> None:
        try:
            async for message in self.ws:
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "SpeechStarted":
                    await self._event_queue.put({"type": "speech_start"})
                    continue

                if etype == "UtteranceEnd":
                    await self._flush_final()
                    await self._event_queue.put({"type": "speech_end"})
                    continue

                if etype == "Results":
                    alts = (event.get("channel") or {}).get("alternatives") or []
                    transcript = (alts[0].get("transcript") if alts else "") or ""
                    transcript = transcript.strip()
                    is_final = bool(event.get("is_final"))
                    speech_final = bool(event.get("speech_final"))

                    if is_final:
                        if transcript:
                            self._final_segments.append(transcript)
                        if speech_final:
                            await self._flush_final()
                    elif transcript:
                        # Interim — pass through for barge-in only.
                        await self._event_queue.put(
                            {"type": "data", "data": {
                                "transcript": transcript, "is_final": False}}
                        )
                    continue

                if etype == "Error" or event.get("error"):
                    logger.error(f"Deepgram STT error: {event}")
                    continue

        except websockets.ConnectionClosed as e:
            logger.info(f"Deepgram STT closed: code={e.code}, reason={e.reason}")
        except Exception as e:
            logger.error(f"Deepgram STT receive error: {e}", exc_info=True)
        finally:
            self._is_open = False

    async def _flush_final(self) -> None:
        """Emit accumulated is_final segments as one clean final transcript."""
        full = " ".join(self._final_segments).strip()
        self._final_segments = []
        if full:
            await self._event_queue.put(
                {"type": "data", "data": {"transcript": full, "is_final": True}}
            )

    async def send_audio(self, mulaw_audio: bytes) -> None:
        """Forward raw mulaw 8 kHz bytes straight to Deepgram (binary frame)."""
        if not self.is_open or not mulaw_audio:
            return
        try:
            await self.ws.send(mulaw_audio)
        except websockets.ConnectionClosed:
            self._is_open = False
        except Exception as e:
            logger.error(f"Deepgram STT send error: {e}")

    async def receive_events(self) -> AsyncGenerator[dict, None]:
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
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self.ws:
            try:
                # Tell Deepgram to finalize and flush before we drop the socket.
                await self.ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            try:
                await self.ws.close()
            except Exception as e:
                logger.debug(f"Deepgram STT close: {e}")
            logger.info("Deepgram STT disconnected")
        if self._receive_loop_task and not self._receive_loop_task.done():
            try:
                await asyncio.wait_for(self._receive_loop_task, timeout=1.0)
            except asyncio.TimeoutError:
                self._receive_loop_task.cancel()
            except Exception:
                pass
