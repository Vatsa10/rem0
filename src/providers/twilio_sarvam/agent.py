import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from starlette.websockets import WebSocket, WebSocketDisconnect
from twilio.rest import Client as TwilioClient

from src.cache import get_greeting_cache
from src.config import CallConfig, get_language_config
from src.conversation.manager import ConversationManager
from src.models.subscriber import Subscriber
from src.providers.base_agent import BaseVoiceAgent
from .audio_utils import mulaw_to_pcm
from .llm_client import LLMClient
from .stt_client import SarvamSTTClient
from .tts_client import SarvamTTSClient
from .twilio_handler import TwilioMediaStreamHandler

logger = logging.getLogger(__name__)

# How long to wait after STT reports a partial before speculating with LLM.
# Shorter = more aggressive speculation (may cancel/restart more).
# Longer = waits for user to stop speaking.
PARTIAL_SPECULATION_DELAY = 0.8  # seconds


@dataclass
class CallSession:
    """Tracks state for a single active call."""
    call_sid: str
    subscriber: Subscriber
    conversation: ConversationManager
    stt: SarvamSTTClient
    tts: SarvamTTSClient
    llm: LLMClient
    twilio_handler: TwilioMediaStreamHandler
    cached_greeting: Optional[bytes] = None
    cached_greeting_text: Optional[str] = None
    transcript: str = ""
    status: str = "in_progress"
    pending_llm_task: Optional[asyncio.Task] = field(default=None)
    last_partial_text: str = ""
    last_partial_time: float = 0.0


class TwilioSarvamAgent(BaseVoiceAgent):
    """
    Orchestrates Twilio Media Streams with Sarvam STT/TTS and Groq LLM.

    Optimizations for low latency:
    - Persistent LLMClient with HTTP/2 + connection pooling
    - Pre-warm STT/TTS WebSockets during Twilio ring (before user answers)
    - Cached greeting audio served from Redis (first response instant)
    - Fast LLM (llama-3.1-8b-instant) for greetings
    - Phrase-boundary streaming to TTS (faster first audio)
    - Partial transcript speculation (start LLM before final transcript)
    """

    def __init__(self, config: CallConfig):
        self.config = config
        self.twilio_client = TwilioClient(
            config.twilio_account_sid, config.twilio_auth_token
        )
        self.active_calls: Dict[str, CallSession] = {}
        self.greeting_cache = get_greeting_cache()
        self._shared_llm: Optional[LLMClient] = None

    async def _safe_task(self, name: str, coro) -> None:
        """Wrap a task coroutine to log exceptions instead of swallowing them."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Task {name!r} failed: {e}", exc_info=True)

    def _get_shared_llm(self) -> LLMClient:
        """Single LLMClient across all calls — reuses HTTP/2 connection pool."""
        if self._shared_llm is None:
            self._shared_llm = LLMClient(
                provider=self.config.llm_provider,
                model=self.config.llm_model,
                api_key=self.config.llm_api_key,
                fast_model=self.config.llm_fast_model,
            )
        return self._shared_llm

    async def initiate_call(
        self, phone_number: str, subscriber_data: dict
    ) -> dict:
        """
        Place an outbound call via Twilio REST API.

        Pre-warms STT/TTS WebSockets and fetches cached greeting in parallel
        while Twilio is ringing the subscriber — giving us a 3-5s head start.
        """
        call_id = str(uuid.uuid4())[:8]
        subscriber = Subscriber(**subscriber_data)
        lang_config = get_language_config(subscriber.language)

        conversation = ConversationManager(
            subscriber=subscriber,
            company_name=self.config.company_name,
            agent_name=self.config.agent_name,
        )
        stt = SarvamSTTClient(
            language=lang_config["stt_code"],
            api_key=self.config.sarvam_api_key,
        )
        tts = SarvamTTSClient(
            language=lang_config["stt_code"],
            voice=lang_config["tts_voice"],
            api_key=self.config.sarvam_api_key,
        )
        llm = self._get_shared_llm()
        twilio_handler = TwilioMediaStreamHandler()

        twiml_url = f"{self.config.server_url}/twiml/{call_id}"
        call = self.twilio_client.calls.create(
            to=phone_number,
            from_=self.config.twilio_from_number,
            url=twiml_url,
            method="POST",
        )

        session = CallSession(
            call_sid=call.sid,
            subscriber=subscriber,
            conversation=conversation,
            stt=stt,
            tts=tts,
            llm=llm,
            twilio_handler=twilio_handler,
        )
        self.active_calls[call_id] = session

        # Fire off pre-warming in background — don't block the REST response.
        asyncio.create_task(self._prewarm_session(session))

        logger.info(
            f"Call initiated: call_id={call_id}, call_sid={call.sid}, "
            f"to={phone_number}, lang={subscriber.language}"
        )

        return {
            "call_id": call_id,
            "call_sid": call.sid,
            "status": call.status,
            "to": phone_number,
        }

    async def _prewarm_session(self, session: CallSession) -> None:
        """
        Pre-open STT/TTS WebSockets and fetch cached greeting in parallel.
        Runs while Twilio is ringing — saves 500ms-1s of setup time.
        """
        lang = session.subscriber.language
        voice = session.tts.voice
        try:
            await self.greeting_cache.connect()

            # Parallel: STT connect, TTS connect, greeting fetch.
            results = await asyncio.gather(
                session.stt.connect(),
                session.tts.connect(),
                self.greeting_cache.get(
                    lang, voice, self.config.company_name, self.config.agent_name
                ),
                return_exceptions=True,
            )

            greeting = results[2]
            if isinstance(greeting, bytes):
                session.cached_greeting = greeting
                session.cached_greeting_text = await self.greeting_cache.get_text(
                    lang, voice, self.config.company_name, self.config.agent_name
                )
                logger.info(
                    f"Pre-warm complete with cached greeting "
                    f"({len(greeting)} bytes, voice={voice}) "
                    f"for call_sid={session.call_sid}"
                )
            else:
                logger.info(
                    f"Pre-warm complete (no cached greeting) "
                    f"for call_sid={session.call_sid}"
                )
        except Exception as e:
            logger.warning(f"Pre-warm failed: {e}")

    async def handle_media_stream(
        self, websocket: WebSocket, call_id: Optional[str] = None
    ) -> None:
        """Main WebSocket handler for Twilio bidirectional media stream."""
        await websocket.accept()

        session = self.active_calls.get(call_id)
        if not session:
            logger.error(f"No session found for call_id={call_id}")
            await websocket.close()
            return

        try:
            # If pre-warm hasn't finished (very fast answer), connect now.
            if not session.stt.is_open:
                await session.stt.connect()
            if not session.tts.is_open:
                await session.tts.connect()

            stt_task = asyncio.create_task(
                self._safe_task("stt_events", self._process_stt_events(session, websocket))
            )
            greeting_task = asyncio.create_task(
                self._safe_task("greeting", self._send_greeting(session, websocket))
            )

            await self._receive_twilio_audio(session, websocket)

            stt_task.cancel()
            greeting_task.cancel()

        except WebSocketDisconnect:
            logger.info(f"Twilio WebSocket disconnected: call_id={call_id}")
        except Exception as e:
            logger.error(f"Media stream error: {e}", exc_info=True)
        finally:
            await self._cleanup_session(session, call_id)

    async def _receive_twilio_audio(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """Receive audio from Twilio and forward to Sarvam STT."""
        handler = session.twilio_handler

        while True:
            try:
                raw = await websocket.receive_text()
                message = handler.parse_message(raw)
                event = message.get("event")

                if event == "media":
                    mulaw_audio = message.get("_decoded_audio")
                    if mulaw_audio:
                        pcm_audio = mulaw_to_pcm(mulaw_audio)
                        await session.stt.send_audio(pcm_audio)
                        # Barge-in is handled in _process_stt_events on the
                        # STT speech_start event (real speech, not silence).
                        # DO NOT cancel TTS here — Twilio sends audio frames
                        # constantly, including silence, which would interrupt
                        # the agent immediately after it starts speaking.

                elif event == "stop":
                    break

            except WebSocketDisconnect:
                break

    async def _trigger_barge_in(
        self,
        session: CallSession,
        websocket: WebSocket,
        reason: str,
    ) -> None:
        """
        Interrupt any in-flight agent response.

        Idempotent — safe to call multiple times in a row. If the agent
        isn't currently speaking, this is a no-op aside from the log.
        """
        if session.pending_llm_task and not session.pending_llm_task.done():
            session.pending_llm_task.cancel()
        if session.conversation.is_agent_speaking:
            logger.info(f"Barge-in [{reason}] — cancelling agent speech")
            session.conversation.handle_barge_in()
            session.tts.cancel()
            await session.twilio_handler.send_clear(websocket)

    async def _process_stt_events(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """
        Process STT events with two barge-in triggers:

        1. **Sarvam VAD** (`speech_start` event) — ideal, fires as soon as
           Sarvam detects voice activity.
        2. **Transcript fallback** — if any transcript (partial or final)
           arrives while the agent is speaking, treat it as barge-in.
           This covers the case where Sarvam's WS endpoint doesn't emit
           `speech_start` reliably — we at least interrupt when we see proof
           the user spoke.
        """
        async for event in session.stt.receive_events():
            event_type = event.get("type", "")

            # (1) Primary barge-in trigger: Sarvam VAD.
            if event_type == "speech_start":
                logger.info("STT: speech_start (user talking)")
                await self._trigger_barge_in(session, websocket, "speech_start")
                continue

            if event_type == "speech_end":
                logger.info("STT: speech_end")
                continue

            # Transcript event: {"type":"data", "data":{"transcript":"...", ...}}
            if event_type == "data":
                data = event.get("data") or {}
                transcript = (data.get("transcript") or "").strip()
                if not transcript:
                    continue
                is_final = data.get("is_final", True)  # default: treat as final

                # (2) Fallback barge-in: any transcript while agent is speaking
                # means the user was talking during our reply — interrupt now.
                if session.conversation.is_agent_speaking:
                    await self._trigger_barge_in(session, websocket, "transcript")

                if is_final:
                    logger.info(f"STT final transcript: {transcript!r}")
                    session.last_partial_text = ""
                    await self._run_turn(session, websocket, transcript, fast=False)
                else:
                    logger.info(f"STT partial: {transcript!r}")
                    session.last_partial_text = transcript
                    session.last_partial_time = time.monotonic()

    async def _run_turn(
        self,
        session: CallSession,
        websocket: WebSocket,
        user_text: str,
        fast: bool = False,
    ) -> None:
        """Generate + speak the agent's response for one turn."""
        messages = session.conversation.build_messages(user_text)
        logger.info(f"LLM turn starting (fast={fast}, user={user_text!r})")

        # What the LLM *generated* (debug only) vs. what the caller actually *heard*.
        generated_full = ""
        heard_chunks: list[str] = []

        session.conversation.is_agent_speaking = True

        try:
            async for token in session.llm.chat_completion_stream(
                messages=messages,
                temperature=0.5,
                max_tokens=60,
                fast=fast,
            ):
                if not session.conversation.is_agent_speaking:
                    break
                chunk = session.conversation.accumulate_token(token)
                generated_full += token
                if chunk:
                    _, heard = await self._speak(session, websocket, chunk)
                    if heard:
                        heard_chunks.append(heard)

            remaining = session.conversation.flush_accumulated()
            if remaining and session.conversation.is_agent_speaking:
                _, heard = await self._speak(session, websocket, remaining)
                if heard:
                    heard_chunks.append(heard)

            # Record only what actually reached the caller (full chunks plus
            # any word-aligned prefix of an interrupted chunk).
            heard_response = " ".join(heard_chunks).strip()
            logger.info(
                f"LLM turn complete: generated={generated_full!r}, "
                f"heard={heard_response!r}"
            )

            if heard_response:
                session.conversation.record_agent_turn(heard_response)
        except asyncio.CancelledError:
            logger.debug("Turn cancelled (likely barge-in)")
            heard_response = " ".join(heard_chunks).strip()
            if heard_response:
                session.conversation.record_agent_turn(heard_response)
            raise
        finally:
            session.conversation.is_agent_speaking = False

    async def _send_greeting(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """
        Deliver the opening greeting.

        Fast path: cached mulaw audio from Redis → stream directly (near-zero latency).
        Slow path: generate via fast LLM + TTS on the fly, then cache for next time.
        """
        # Small delay to let Twilio complete its connection handshake.
        await asyncio.sleep(0.3)

        # Fast path: cached audio, stream directly to Twilio.
        if session.cached_greeting:
            logger.info(
                f"Serving cached greeting ({len(session.cached_greeting)} bytes)"
            )
            session.conversation.is_agent_speaking = True
            chunk_size = 640  # 80ms of mulaw @ 8kHz
            try:
                for i in range(0, len(session.cached_greeting), chunk_size):
                    if not session.conversation.is_agent_speaking:
                        break
                    chunk = session.cached_greeting[i : i + chunk_size]
                    await session.twilio_handler.send_audio(websocket, chunk)
                    await asyncio.sleep(0.08)  # pace to real-time playback

                if session.cached_greeting_text:
                    session.conversation.record_agent_turn(
                        session.cached_greeting_text
                    )
            finally:
                session.conversation.is_agent_speaking = False
            return

        # Slow path: live generation. Use fast model for low TTFT.
        logger.info(f"Generating live greeting for {session.subscriber.name}")
        greeting_prompt = (
            "[SYSTEM: Call connected. Say one short sentence: greet by name, "
            "say who you are from {company}, ask 'is this {name}?'. "
            "MAX 15 WORDS. No introductions like 'I am calling about' yet.]"
        ).format(
            company=self.config.company_name,
            name=session.subscriber.name,
        )
        session.conversation.message_history.append(
            {"role": "user", "content": greeting_prompt}
        )

        generated_full = ""
        heard_chunks: list[str] = []
        collected_audio = bytearray()
        total_audio_sent = 0
        any_interrupt = False
        session.conversation.is_agent_speaking = True

        async def _speak_greeting_chunk(chunk_text: str) -> tuple[bool, str]:
            """
            Stream one chunk; returns (spoke_fully, heard_text).
            heard_text is the chunk (if fully delivered) or a word-aligned prefix
            estimated from bytes streamed to Twilio (if interrupted).
            """
            nonlocal total_audio_sent
            interrupted = False
            bytes_sent_this_chunk = 0
            async for audio in session.tts.synthesize(chunk_text):
                collected_audio.extend(audio)
                if not session.conversation.is_agent_speaking:
                    interrupted = True
                    break
                await session.twilio_handler.send_audio(websocket, audio)
                total_audio_sent += len(audio)
                bytes_sent_this_chunk += len(audio)
            if interrupted:
                return False, self._estimate_heard_text(chunk_text, bytes_sent_this_chunk)
            return True, chunk_text

        try:
            async for token in session.llm.chat_completion_stream(
                messages=session.conversation.message_history,
                temperature=0.5,
                max_tokens=50,
                fast=True,
            ):
                if not session.conversation.is_agent_speaking:
                    logger.info("Greeting interrupted mid-generation")
                    any_interrupt = True
                    break
                chunk = session.conversation.accumulate_token(token)
                generated_full += token
                if chunk:
                    logger.info(f"Greeting chunk to TTS: {chunk!r}")
                    spoke_fully, heard = await _speak_greeting_chunk(chunk)
                    if not spoke_fully:
                        any_interrupt = True
                    if heard:
                        heard_chunks.append(heard)

            remaining = session.conversation.flush_accumulated()
            if remaining and session.conversation.is_agent_speaking:
                logger.info(f"Greeting final chunk to TTS: {remaining!r}")
                spoke_fully, heard = await _speak_greeting_chunk(remaining)
                if not spoke_fully:
                    any_interrupt = True
                if heard:
                    heard_chunks.append(heard)

            heard_response = " ".join(heard_chunks).strip()
            logger.info(
                f"Greeting done: heard={heard_response!r}, "
                f"generated={generated_full!r}, audio_sent={total_audio_sent}B"
            )

            if heard_response:
                session.conversation.message_history.pop(-1)
                session.conversation.record_agent_turn(heard_response)

                # Only cache fully-heard greetings (not interrupted ones).
                if collected_audio and not any_interrupt:
                    await self.greeting_cache.set(
                        session.subscriber.language,
                        session.tts.voice,
                        self.config.company_name,
                        self.config.agent_name,
                        bytes(collected_audio),
                    )
                    await self.greeting_cache.set_text(
                        session.subscriber.language,
                        session.tts.voice,
                        self.config.company_name,
                        self.config.agent_name,
                        heard_response,
                    )
        finally:
            session.conversation.is_agent_speaking = False

    # Twilio plays mulaw at 8000 bytes/sec; natural speech is ~2.5 words/sec.
    _TWILIO_BYTES_PER_SEC = 8000
    _WORDS_PER_SEC = 2.5
    # Twilio holds a small buffer. When we send `clear`, anything queued but
    # not yet played is discarded — subtract ~80ms to stay slightly conservative.
    _TWILIO_PLAYBACK_BUFFER_SEC = 0.08

    def _estimate_heard_text(self, text: str, bytes_sent: int) -> str:
        """
        Estimate which portion of `text` the caller actually heard based on
        how many mulaw bytes reached Twilio before we sent `clear`.

        Uses a word-boundary truncation so the stored agent turn remains
        grammatically coherent instead of cutting off mid-word.
        """
        words = text.split()
        if not words:
            return ""
        seconds_played = max(
            0.0, (bytes_sent / self._TWILIO_BYTES_PER_SEC) - self._TWILIO_PLAYBACK_BUFFER_SEC
        )
        words_heard = int(seconds_played * self._WORDS_PER_SEC)
        # At least report 0 words rather than inventing ones.
        words_heard = max(0, min(words_heard, len(words)))
        return " ".join(words[:words_heard])

    async def _speak(
        self, session: CallSession, websocket: WebSocket, text: str
    ) -> tuple[bool, str]:
        """
        Send text to TTS and stream audio back to Twilio.

        Returns:
            (spoke_fully, heard_text)
              spoke_fully — True if the entire chunk reached Twilio.
              heard_text  — full `text` if not interrupted, else a
                            word-aligned prefix estimated from bytes streamed.
        """
        mark_id = str(uuid.uuid4())[:8]
        sent = 0
        interrupted = False

        async for audio_chunk in session.tts.synthesize(text):
            if not session.conversation.is_agent_speaking:
                interrupted = True
                break
            await session.twilio_handler.send_audio(websocket, audio_chunk)
            sent += len(audio_chunk)

        if interrupted:
            heard = self._estimate_heard_text(text, sent)
            logger.debug(
                f"Spoke (interrupted): {text!r} -> heard {heard!r} ({sent}B)"
            )
        else:
            heard = text
            logger.debug(f"Spoke: {text!r} ({sent}B)")

        await session.twilio_handler.send_mark(websocket, mark_id)
        return (not interrupted), heard

    async def _cleanup_session(self, session: CallSession, call_id: str) -> None:
        """Close all connections and clean up session state."""
        if session.pending_llm_task and not session.pending_llm_task.done():
            session.pending_llm_task.cancel()
        await session.stt.close()
        await session.tts.close()
        session.transcript = session.conversation.get_transcript_text()
        session.status = "completed"
        logger.info(f"Session cleaned up: call_id={call_id}")

    async def end_call(self, call_id: str) -> None:
        """Programmatically end an active call via Twilio API."""
        session = self.active_calls.get(call_id)
        if session:
            self.twilio_client.calls(session.call_sid).update(status="completed")
            logger.info(f"Call ended via API: call_id={call_id}")

    def get_session(self, call_id: str) -> Optional[CallSession]:
        return self.active_calls.get(call_id)

    def get_twiml(self, call_id: str) -> str:
        ws_url = f"{self.config.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}/media-stream/{call_id}"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
