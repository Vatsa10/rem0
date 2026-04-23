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

            # Log each sub-result explicitly — return_exceptions=True turns
            # failures into values that would otherwise disappear silently.
            stt_result, tts_result, greeting = results
            if isinstance(stt_result, Exception):
                logger.error(
                    f"Pre-warm STT connect failed for call_sid={session.call_sid}: "
                    f"{stt_result!r}"
                )
            if isinstance(tts_result, Exception):
                logger.error(
                    f"Pre-warm TTS connect failed for call_sid={session.call_sid}: "
                    f"{tts_result!r}"
                )

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
                    f"stt_ok={not isinstance(stt_result, Exception)} "
                    f"tts_ok={not isinstance(tts_result, Exception)} "
                    f"for call_sid={session.call_sid}"
                )
        except Exception as e:
            logger.warning(f"Pre-warm failed: {e}", exc_info=True)

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
            # If pre-warm didn't land (very fast pickup, or failed silently),
            # try again here with bounded retries. If still failing, end the
            # call cleanly — don't hang or crash the WebSocket handler.
            try:
                if not session.stt.is_open:
                    await session.stt.connect()
                if not session.tts.is_open:
                    await session.tts.connect()
            except Exception as conn_err:
                logger.error(
                    f"Fatal: STT/TTS connect failed after retries for "
                    f"call_sid={session.call_sid}: {conn_err!r} — "
                    f"ending call gracefully"
                )
                return

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
                    # Use _run_turn's default (fast=True → 8B instant model)
                    # for low latency; 8B is plenty for our short replies.
                    await self._run_turn(session, websocket, transcript)
                else:
                    logger.info(f"STT partial: {transcript!r}")
                    session.last_partial_text = transcript
                    session.last_partial_time = time.monotonic()

    async def _run_turn(
        self,
        session: CallSession,
        websocket: WebSocket,
        user_text: str,
        fast: bool = True,
    ) -> None:
        """
        Full-response-then-speak turn, with state-aware memory.

        Uses the fast model (llama-3.1-8b-instant) by default for low latency —
        the 8B model is plenty smart for the short, focused replies we ask for.

          1. Collect the complete LLM response (no TTS during generation).
          2. Speak it as a single utterance (still interruptible via barge-in).
          3. Record outcome via record_turn_result — captures both what was
             generated and what the caller actually heard, plus updates the
             state machine and barge-in memory for the next turn.
        """
        messages = session.conversation.build_messages(user_text)
        logger.info(
            f"LLM turn starting (fast={fast}, state={session.conversation.state.value}, "
            f"user={user_text!r})"
        )

        # Phase 1 — collect full LLM response (no audio yet).
        full_response = ""
        try:
            async for token in session.llm.chat_completion_stream(
                messages=messages,
                temperature=0.5,
                max_tokens=60,
                fast=fast,
            ):
                full_response += token
        except asyncio.CancelledError:
            logger.debug("LLM generation cancelled before speak phase")
            raise

        reply = full_response.strip()
        logger.info(f"LLM turn generated: {reply!r}")

        if not reply:
            return

        # Phase 2 — speak the complete reply as one utterance.
        session.conversation.is_agent_speaking = True
        heard = ""
        try:
            _, heard = await self._speak(session, websocket, reply)
            logger.info(f"LLM turn heard: {heard!r}")
        except asyncio.CancelledError:
            logger.debug("Turn cancelled during speak phase")
            raise
        finally:
            session.conversation.is_agent_speaking = False
            # Always update state + barge-in memory, even on cancellation.
            session.conversation.record_turn_result(
                user_text=user_text,
                generated=reply,
                heard=heard,
            )

    async def _send_greeting(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """
        Deliver the opening greeting.

        Waits for Twilio's `start` event (stream_sid available) before sending
        any audio — otherwise send_audio silently drops frames.

        Fast path: cached mulaw audio from Redis → stream directly.
        Slow path: generate full LLM response, then synthesize + speak as one utterance.
        """
        # Critical: wait for Twilio to send the `start` event before producing
        # any audio. Without stream_sid, send_audio is a no-op and the greeting
        # would be lost.
        ready = await session.twilio_handler.wait_until_ready(timeout=5.0)
        if not ready:
            logger.warning(
                f"Twilio stream never became ready; skipping greeting "
                f"for call_sid={session.call_sid}"
            )
            return

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
                    session.conversation.record_turn_result(
                        user_text="",
                        generated=session.cached_greeting_text,
                        heard=session.cached_greeting_text,
                    )
            finally:
                session.conversation.is_agent_speaking = False
            return

        # Slow path: full-response-then-speak.
        logger.info(f"Generating live greeting for {session.subscriber.name}")
        greeting_prompt = (
            "[SYSTEM: Call connected. Say one short sentence: greet by name, "
            f"say who you are from {self.config.company_name}, "
            f"ask 'is this {session.subscriber.name}?'. MAX 15 WORDS.]"
        )
        session.conversation.message_history.append(
            {"role": "user", "content": greeting_prompt}
        )

        # Phase 1 — collect full greeting text (no audio yet).
        full_response = ""
        try:
            async for token in session.llm.chat_completion_stream(
                messages=session.conversation.message_history,
                temperature=0.5,
                max_tokens=50,
                fast=True,
            ):
                full_response += token
        except Exception as e:
            logger.error(f"Greeting LLM failed: {e}")
            session.conversation.message_history.pop(-1)
            return

        reply = full_response.strip()
        logger.info(f"Greeting generated: {reply!r}")
        if not reply:
            session.conversation.message_history.pop(-1)
            return

        # Phase 2 — synthesize full utterance, stream to Twilio, and collect
        # raw mulaw bytes for caching.
        session.conversation.is_agent_speaking = True
        collected_audio = bytearray()
        sent = 0
        interrupted = False
        mark_id = str(uuid.uuid4())[:8]

        try:
            async for audio_chunk in session.tts.synthesize(reply):
                collected_audio.extend(audio_chunk)
                if not session.conversation.is_agent_speaking:
                    interrupted = True
                    break
                await session.twilio_handler.send_audio(websocket, audio_chunk)
                sent += len(audio_chunk)
            await session.twilio_handler.send_mark(websocket, mark_id)

            heard = reply if not interrupted else self._estimate_heard_text(reply, sent)
            logger.info(
                f"Greeting done: heard={heard!r}, audio_sent={sent}B, "
                f"interrupted={interrupted}"
            )

            # Replace the fake prompt in history with the real turn outcome,
            # and let record_turn_result update state + barge-in memory.
            session.conversation.message_history.pop(-1)
            if heard:
                session.conversation.record_turn_result(
                    user_text="",  # greeting has no user text
                    generated=reply,
                    heard=heard,
                )

            # Only cache greetings that played fully through (consistent audio ↔ text).
            if not interrupted and collected_audio and heard:
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
                    heard,
                )
        finally:
            session.conversation.is_agent_speaking = False

    # Twilio plays mulaw at 8000 bytes/sec; natural speech is ~2.5 words/sec.
    _TWILIO_BYTES_PER_SEC = 8000
    _WORDS_PER_SEC = 2.5
    _TWILIO_PLAYBACK_BUFFER_SEC = 0.08  # frames in Twilio buffer, cleared on barge-in

    def _estimate_heard_text(self, text: str, bytes_sent: int) -> str:
        """
        Estimate how much of `text` the caller actually heard, based on mulaw
        bytes that reached Twilio before `clear` was sent. Truncates to a word
        boundary so the stored agent turn is grammatically clean.
        """
        words = text.split()
        if not words:
            return ""
        seconds_played = max(
            0.0,
            (bytes_sent / self._TWILIO_BYTES_PER_SEC) - self._TWILIO_PLAYBACK_BUFFER_SEC,
        )
        words_heard = int(seconds_played * self._WORDS_PER_SEC)
        words_heard = max(0, min(words_heard, len(words)))
        return " ".join(words[:words_heard])

    async def _speak(
        self, session: CallSession, websocket: WebSocket, text: str
    ) -> tuple[bool, str]:
        """
        Stream text to TTS → Twilio as a single utterance.

        Returns:
            (spoke_fully, heard_text)
              spoke_fully — True if the entire utterance reached Twilio.
              heard_text  — full `text` if uninterrupted, else a word-aligned
                            prefix estimated from bytes streamed.
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
