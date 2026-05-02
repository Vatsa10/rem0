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
    # Time period captured once at session creation so cache GET (pre-warm)
    # and cache SET (post-gen) can't end up on different keys if the clock
    # rolls between morning→afternoon mid-call.
    time_period: str = "afternoon"
    cached_greeting: Optional[bytes] = None
    cached_greeting_text: Optional[str] = None
    transcript: str = ""
    # "in_progress" | "completed" | "prewarm_failed"
    status: str = "in_progress"
    pending_llm_task: Optional[asyncio.Task] = field(default=None)
    # asyncio.Lock can't have a default_factory in dataclass-with-defaults easily;
    # we'll lazily init it in the agent if None.
    _pending_task_lock: Optional[asyncio.Lock] = field(default=None)
    last_partial_text: str = ""
    last_partial_time: float = 0.0
    # Per-turn latency anchors. Set when the user's STT final transcript
    # arrives; turn code reads them to compute end-to-end latency_ms.
    last_stt_final_time: float = 0.0
    # Running estimate of how much of the current utterance the caller has
    # already heard (word-aligned prefix). Updated by `_speak` after each
    # chunk reaches Twilio, so a cancelled mid-speak turn can still record
    # the agent line into the transcript via `record_turn_result`. Without
    # this, an interrupted reply that finished speaking before the task got
    # cancelled disappears from call history entirely.
    last_speak_partial: str = ""


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

    @staticmethod
    def _log_task_exception(task: asyncio.Task) -> None:
        """
        Done-callback for create_task so exceptions surface instead of being
        silently swallowed when the task isn't explicitly awaited.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Task failed: {exc!r}", exc_info=exc)

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
            time_period=self._time_period(),  # fix cache-key drift
            _pending_task_lock=asyncio.Lock(),
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
        period = session.time_period  # memoized at session creation
        try:
            await self.greeting_cache.connect()

            # Parallel: STT connect, TTS connect, greeting fetch.
            results = await asyncio.gather(
                session.stt.connect(),
                session.tts.connect(),
                self.greeting_cache.get(
                    lang, voice, period,
                    self.config.company_name, self.config.agent_name,
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
            # If both pipes failed we can't run a call — mark session so
            # handle_media_stream fails fast instead of limping into a
            # broken call.
            if isinstance(stt_result, Exception) and isinstance(tts_result, Exception):
                session.status = "prewarm_failed"

            if isinstance(greeting, bytes):
                session.cached_greeting = greeting
                session.cached_greeting_text = await self.greeting_cache.get_text(
                    lang, voice, period,
                    self.config.company_name, self.config.agent_name,
                )
                logger.info(
                    f"Pre-warm complete with cached greeting "
                    f"({len(greeting)} bytes, voice={voice}, period={period}) "
                    f"for call_sid={session.call_sid}"
                )
            else:
                logger.info(
                    f"Pre-warm complete (no cached greeting, period={period}) "
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
        if session.status == "prewarm_failed":
            logger.error(
                f"Session {call_id} failed pre-warm (STT+TTS both down); closing"
            )
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

        Cancels the pending LLM turn task (kills generation) and, if the
        agent is mid-utterance, stops the TTS stream and flushes Twilio's
        audio buffer so the caller hears the interruption take effect.

        Serialized via session._pending_task_lock so rapid-fire triggers
        (speech_start + transcript arriving in the same tick) can't race
        on pending_llm_task assignment/cancellation.

        Idempotent — safe to call multiple times.
        """
        lock = session._pending_task_lock
        # Fallback if someone created a session without a lock (shouldn't happen).
        if lock is None:
            lock = asyncio.Lock()
            session._pending_task_lock = lock

        async with lock:
            cancelled_turn = False
            if session.pending_llm_task and not session.pending_llm_task.done():
                session.pending_llm_task.cancel()
                cancelled_turn = True

            if session.conversation.is_agent_speaking:
                logger.info(f"Barge-in [{reason}] — cancelling agent speech")
                session.conversation.handle_barge_in()
                session.tts.cancel()
                await session.twilio_handler.send_clear(websocket)
            elif cancelled_turn:
                logger.info(
                    f"Barge-in [{reason}] — cancelled pending LLM turn (pre-speak)"
                )

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
                    session.last_stt_final_time = time.monotonic()

                    # Cancel any in-flight turn — the caller has said
                    # something new, superseding whatever we were working on.
                    if (
                        session.pending_llm_task
                        and not session.pending_llm_task.done()
                    ):
                        logger.info(
                            "Cancelling previous turn for new transcript"
                        )
                        session.pending_llm_task.cancel()

                    # Run the turn as a BACKGROUND TASK so this event loop
                    # keeps polling STT — so we can detect barge-ins that
                    # happen during LLM generation / TTS streaming. Awaiting
                    # _run_turn inline would block this loop and make mid-turn
                    # barge-ins invisible until the turn finished.
                    #
                    # Direct create_task (no _safe_task wrapper) avoids the
                    # "coroutine was never awaited" warning when a task is
                    # cancelled in the same tick it's created. Assignment is
                    # guarded by the same lock barge-in uses.
                    lock = session._pending_task_lock or asyncio.Lock()
                    session._pending_task_lock = lock
                    async with lock:
                        turn_task = asyncio.create_task(
                            self._run_turn(session, websocket, transcript)
                        )
                        turn_task.add_done_callback(self._log_task_exception)
                        session.pending_llm_task = turn_task
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

        # Latency anchors (monotonic, seconds). Computed at end as ms deltas.
        t_stt = session.last_stt_final_time or time.monotonic()
        t_llm_start = time.monotonic()
        t_llm_first_token: Optional[float] = None
        t_llm_done: Optional[float] = None
        t_first_audio: Optional[float] = None

        # Phase 1 — collect full LLM response (no audio yet).
        # max_tokens=200 covers a 30-word reply with comfortable headroom.
        # The system prompt's "≤20 words per turn" rule already constrains
        # length; we just don't want a hard cap to ever cut a sentence.
        full_response = ""
        llm_failed = False
        try:
            async for token in session.llm.chat_completion_stream(
                messages=messages,
                temperature=0.5,
                max_tokens=200,
                fast=fast,
            ):
                if t_llm_first_token is None:
                    t_llm_first_token = time.monotonic()
                full_response += token
        except asyncio.CancelledError:
            logger.debug("LLM generation cancelled before speak phase")
            raise
        except Exception as e:
            # LLM provider error (rate-limit, 5xx, network hiccup). Don't
            # speak a half-generated reply — fall back to a short recovery
            # line so the caller hears *something* and the call doesn't
            # stall in dead air.
            logger.error(
                f"LLM generation failed (partial={len(full_response)} chars): {e!r}"
            )
            llm_failed = True

        t_llm_done = time.monotonic()

        if llm_failed:
            reply = "Sorry, I had a brief hiccup — could you repeat that?"
        else:
            reply = full_response.strip()

        logger.info(f"LLM turn generated: {reply!r}")

        if not reply:
            return

        # Phase 2 — speak the complete reply as one utterance.
        # Claim a new speak session id; barge-in increments past this id to
        # invalidate the speaker. Avoids the false-cancel race where finally:
        # blocks reset is_agent_speaking even on normal completion.
        local_id = session.conversation.bump_speak_session()
        session.conversation.is_agent_speaking = True
        heard = ""
        try:
            _, heard, t_first_audio = await self._speak(
                session, websocket, reply, speak_id=local_id
            )
            logger.info(f"LLM turn heard: {heard!r}")
        except asyncio.CancelledError:
            # _speak was cancelled mid-stream — pull the running partial it
            # published on the session so the agent's spoken prefix still
            # makes it into the transcript / call history.
            heard = session.last_speak_partial or ""
            logger.debug(f"Turn cancelled during speak phase; heard={heard!r}")
            raise
        finally:
            # Only flip the flag if we're still the active speaker; barge-in
            # already flipped it if it fired during the turn.
            if session.conversation.speak_session_id == local_id:
                session.conversation.is_agent_speaking = False
            # Always update state + barge-in memory, even on cancellation.
            session.conversation.record_turn_result(
                user_text=user_text,
                generated=reply,
                heard=heard,
            )
            t_done = time.monotonic()
            self._log_turn_latency(
                t_stt=t_stt,
                t_llm_start=t_llm_start,
                t_llm_first_token=t_llm_first_token,
                t_llm_done=t_llm_done,
                t_first_audio=t_first_audio,
                t_done=t_done,
                tag="turn",
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
            total_audio_bytes = len(session.cached_greeting)
            logger.info(f"Serving cached greeting ({total_audio_bytes} bytes)")
            local_id = session.conversation.bump_speak_session()
            session.conversation.is_agent_speaking = True
            chunk_size = 640  # 80ms of mulaw @ 8kHz
            bytes_sent = 0
            interrupted = False
            send_failed = False
            t_speak_start = time.monotonic()
            session.last_speak_partial = ""
            cached_text = session.cached_greeting_text or ""
            heard = ""
            try:
                for i in range(0, total_audio_bytes, chunk_size):
                    # Race-free barge-in check via session id.
                    if session.conversation.speak_session_id != local_id:
                        interrupted = True
                        try:
                            await session.twilio_handler.send_clear(websocket)
                        except Exception:
                            pass
                        break
                    chunk = session.cached_greeting[i : i + chunk_size]
                    try:
                        ok = await session.twilio_handler.send_audio(
                            websocket, chunk
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Greeting send_audio raised {e!r}")
                        send_failed = True
                        break
                    if not ok:
                        logger.warning("Greeting send_audio dropped frame")
                        send_failed = True
                        break
                    bytes_sent += len(chunk)
                    if cached_text:
                        session.last_speak_partial = self._estimate_heard_text(
                            cached_text, bytes_sent
                        )
                    await asyncio.sleep(0.08)  # pace to real-time playback

                # Truncation/short-audio telemetry.
                actual_sec = bytes_sent / self._TWILIO_BYTES_PER_SEC
                expected_sec = total_audio_bytes / self._TWILIO_BYTES_PER_SEC
                wall_sec = time.monotonic() - t_speak_start
                logger.info(
                    f"Cached greeting done: bytes_sent={bytes_sent}, "
                    f"audio_sec={actual_sec:.2f}, expected_sec={expected_sec:.2f}, "
                    f"wall_sec={wall_sec:.2f}, interrupted={interrupted}, "
                    f"send_failed={send_failed}"
                )

                if cached_text:
                    if interrupted or send_failed:
                        heard = self._estimate_heard_text(cached_text, bytes_sent)
                    else:
                        heard = cached_text
            except asyncio.CancelledError:
                # Mid-stream cancel — pick up whatever we've shipped so the
                # agent's greeting prefix still lands in the transcript.
                if cached_text:
                    heard = self._estimate_heard_text(cached_text, bytes_sent)
                raise
            finally:
                if session.conversation.speak_session_id == local_id:
                    session.conversation.is_agent_speaking = False
                # Record outside the inner try so any path (normal / barge-in /
                # cancel / send_failed) writes the agent turn into transcript.
                if cached_text and heard:
                    session.conversation.record_turn_result(
                        user_text="",
                        generated=cached_text,
                        heard=heard,
                    )
            return

        # Slow path: full-response-then-speak.
        logger.info(f"Generating live greeting for {session.subscriber.name}")
        period = session.time_period  # memoized at session creation

        # Clean per-period opener — one natural phrase, no "Hi, hello" mashups.
        time_opener = {
            "morning": "Good morning",
            "afternoon": "Good afternoon",
            "evening": "Good evening",
            "late": "Hello",  # outside civil hours — skip time-of-day
        }.get(period, "Hello")

        # First-name only sounds warmer than "Mr. FirstName LastName".
        first_name = (session.subscriber.name or "").split()[0] or "there"

        greeting_prompt = (
            "[SYSTEM: Call connected. Say exactly one warm, natural opener "
            f"in this shape: \"{time_opener}, am I speaking with {first_name}?\" "
            "Nothing else. No 'Hi', no double-greeting, no company pitch yet. "
            "Keep it under 10 words and sound like a real person, not a script.]"
        )
        session.conversation.message_history.append(
            {"role": "user", "content": greeting_prompt}
        )

        # Phase 1 — collect full greeting text (no audio yet).
        # 100 tokens covers a ~12-word opener with comfortable headroom; the
        # prompt itself caps to "under 10 words" so this is just safety.
        full_response = ""
        try:
            async for token in session.llm.chat_completion_stream(
                messages=session.conversation.message_history,
                temperature=0.5,
                max_tokens=100,
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
        local_id = session.conversation.bump_speak_session()
        session.conversation.is_agent_speaking = True
        collected_audio = bytearray()
        sent = 0
        interrupted = False
        send_failed = False
        mark_id = str(uuid.uuid4())[:8]
        t_speak_start = time.monotonic()
        session.last_speak_partial = ""
        # Pop the synthetic greeting prompt from history right away — we
        # never want it to leak into a subsequent LLM turn even if this
        # function is cancelled mid-stream.
        if (
            session.conversation.message_history
            and session.conversation.message_history[-1].get("role") == "user"
        ):
            session.conversation.message_history.pop(-1)
        heard = ""
        played_full = False

        try:
            async for audio_chunk in session.tts.synthesize(reply):
                collected_audio.extend(audio_chunk)
                if session.conversation.speak_session_id != local_id:
                    interrupted = True
                    break
                try:
                    ok = await session.twilio_handler.send_audio(
                        websocket, audio_chunk
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Greeting send_audio raised {e!r}")
                    send_failed = True
                    break
                if not ok:
                    logger.warning("Greeting send_audio dropped frame")
                    send_failed = True
                    break
                sent += len(audio_chunk)
                session.last_speak_partial = self._estimate_heard_text(reply, sent)
                # Real-time pacing (see _speak for rationale).
                await asyncio.sleep(len(audio_chunk) / self._TWILIO_BYTES_PER_SEC)
            try:
                await session.twilio_handler.send_mark(websocket, mark_id)
            except Exception as e:
                logger.debug(f"Greeting send_mark failed: {e}")

            words = max(1, len(reply.split()))
            expected_sec = words / self._WORDS_PER_SEC
            actual_sec = sent / self._TWILIO_BYTES_PER_SEC
            wall_sec = time.monotonic() - t_speak_start

            played_full = not (interrupted or send_failed)
            if played_full and actual_sec < 0.7 * expected_sec:
                logger.warning(
                    f"Greeting: short audio — {actual_sec:.2f}s shipped vs "
                    f"~{expected_sec:.2f}s expected for {words}-word reply "
                    f"{reply!r}; possible TTS truncation. Skipping cache write."
                )
                played_full = False  # don't cache a truncated greeting

            heard = reply if played_full else self._estimate_heard_text(reply, sent)
            logger.info(
                f"Greeting done: heard={heard!r}, audio_sent={sent}B, "
                f"audio_sec={actual_sec:.2f}, expected_sec={expected_sec:.2f}, "
                f"wall_sec={wall_sec:.2f}, interrupted={interrupted}, "
                f"send_failed={send_failed}"
            )

            # Only cache greetings that played fully through AND match the
            # expected duration (so a truncated stream isn't cached as canon).
            if played_full and collected_audio and heard:
                await self.greeting_cache.set(
                    session.subscriber.language,
                    session.tts.voice,
                    period,
                    self.config.company_name,
                    self.config.agent_name,
                    bytes(collected_audio),
                )
                await self.greeting_cache.set_text(
                    session.subscriber.language,
                    session.tts.voice,
                    period,
                    self.config.company_name,
                    self.config.agent_name,
                    heard,
                )
        except asyncio.CancelledError:
            # Mid-stream cancel — capture the prefix actually shipped so it
            # makes it into the transcript.
            heard = self._estimate_heard_text(reply, sent)
            raise
        finally:
            if session.conversation.speak_session_id == local_id:
                session.conversation.is_agent_speaking = False
            if heard:
                session.conversation.record_turn_result(
                    user_text="",  # greeting has no user text
                    generated=reply,
                    heard=heard,
                )

    # Twilio plays mulaw at 8000 bytes/sec; natural speech is ~2.5 words/sec.
    _TWILIO_BYTES_PER_SEC = 8000
    _WORDS_PER_SEC = 2.5
    _TWILIO_PLAYBACK_BUFFER_SEC = 0.08  # frames in Twilio buffer, cleared on barge-in

    @staticmethod
    def _time_period(hour: Optional[int] = None) -> str:
        """
        Return 'morning' / 'afternoon' / 'evening' / 'late' based on the hour.
        Used both for spoken greeting phrasing and as a cache-key discriminator
        so a morning-recorded greeting isn't reused in the evening.
        """
        from datetime import datetime
        h = datetime.now().hour if hour is None else hour
        if 5 <= h < 12:
            return "morning"
        if 12 <= h < 17:
            return "afternoon"
        if 17 <= h < 22:
            return "evening"
        return "late"

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
        self,
        session: CallSession,
        websocket: WebSocket,
        text: str,
        speak_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[float]]:
        """
        Stream text to TTS → Twilio as a single utterance.

        Args:
            speak_id: the speak_session_id captured at the start of the turn.
                Loop bails if `mgr.speak_session_id` no longer matches (barge-in
                bumped past us). If None, falls back to is_agent_speaking
                (legacy callers).

        Returns:
            (spoke_fully, heard_text, t_first_audio)
              spoke_fully    — True if the entire utterance reached Twilio.
              heard_text     — full `text` if uninterrupted, else a word-aligned
                               prefix estimated from bytes streamed.
              t_first_audio  — monotonic time when first byte was sent to Twilio
                               (for latency telemetry); None if nothing was sent.
        """
        mark_id = str(uuid.uuid4())[:8]
        sent = 0
        interrupted = False
        send_failed = False
        t_first_audio: Optional[float] = None
        t_speak_start = time.monotonic()
        # Reset the running partial-heard before this utterance starts so a
        # cancelled turn doesn't pick up a prior turn's prefix.
        session.last_speak_partial = ""

        async for audio_chunk in session.tts.synthesize(text):
            # Barge-in check via session id (race-free). Falls back to
            # is_agent_speaking for callers that didn't pass speak_id.
            if speak_id is not None:
                if session.conversation.speak_session_id != speak_id:
                    interrupted = True
                    break
            elif not session.conversation.is_agent_speaking:
                interrupted = True
                break
            try:
                ok = await session.twilio_handler.send_audio(websocket, audio_chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"_speak: send_audio raised {e!r}; aborting utterance")
                send_failed = True
                break
            if not ok:
                # stream_sid not yet ready or Twilio dropped the frame.
                logger.warning("_speak: send_audio dropped frame; aborting utterance")
                send_failed = True
                break
            if t_first_audio is None:
                t_first_audio = time.monotonic()
            sent += len(audio_chunk)
            # Publish running partial-heard so `_run_turn`'s finally can
            # still record the agent line if this task is cancelled before
            # we return cleanly. Word-aligned to keep the stored transcript
            # grammatical.
            session.last_speak_partial = self._estimate_heard_text(text, sent)
            # Pace at real-time (8000 mulaw bytes/sec). Without this we blast
            # the whole utterance into Twilio's buffer in milliseconds; barge-in
            # then has nothing to clear because Twilio already started playback,
            # and frame-pressure can produce choppy / "breaking" audio.
            await asyncio.sleep(len(audio_chunk) / self._TWILIO_BYTES_PER_SEC)

        # Truncation telemetry — compare actual vs expected speech duration.
        words = max(1, len(text.split()))
        expected_sec = words / self._WORDS_PER_SEC
        actual_sec = sent / self._TWILIO_BYTES_PER_SEC
        wall_sec = time.monotonic() - t_speak_start
        if not interrupted and not send_failed and actual_sec < 0.7 * expected_sec:
            logger.warning(
                f"_speak: short audio — {actual_sec:.2f}s shipped vs ~{expected_sec:.2f}s "
                f"expected for {words}-word reply {text!r}; possible TTS truncation"
            )

        if interrupted:
            heard = self._estimate_heard_text(text, sent)
        elif send_failed:
            heard = self._estimate_heard_text(text, sent)
        else:
            heard = text
        logger.info(
            f"_speak done: bytes={sent}, audio_sec={actual_sec:.2f}, "
            f"expected_sec={expected_sec:.2f}, wall_sec={wall_sec:.2f}, "
            f"interrupted={interrupted}, send_failed={send_failed}"
        )

        try:
            await session.twilio_handler.send_mark(websocket, mark_id)
        except Exception as e:
            logger.debug(f"_speak: send_mark failed: {e}")
        return (not (interrupted or send_failed)), heard, t_first_audio

    def _log_turn_latency(
        self,
        *,
        t_stt: float,
        t_llm_start: float,
        t_llm_first_token: Optional[float],
        t_llm_done: Optional[float],
        t_first_audio: Optional[float],
        t_done: float,
        tag: str,
    ) -> None:
        """
        One INFO line per turn with end-to-end latency breakdown.
        All numbers are ms; "—" means the anchor wasn't reached (e.g. LLM
        failed before yielding any token).
        """
        def ms(t0: Optional[float], t1: Optional[float]) -> str:
            if t0 is None or t1 is None:
                return "—"
            return f"{(t1 - t0) * 1000:.0f}"

        logger.info(
            f"latency_ms[{tag}] "
            f"stt_to_llm={ms(t_stt, t_llm_start)} "
            f"llm_first_token={ms(t_llm_start, t_llm_first_token)} "
            f"llm_total={ms(t_llm_start, t_llm_done)} "
            f"to_first_audio={ms(t_stt, t_first_audio)} "
            f"total={ms(t_stt, t_done)}"
        )

    async def _cleanup_session(self, session: CallSession, call_id: str) -> None:
        """
        Close all connections and mark session as completed.
        The session itself stays in active_calls until release_session() is
        called — usually right after post-call analysis finishes. This
        prevents a memory leak on repeated calls.
        """
        if session.pending_llm_task and not session.pending_llm_task.done():
            session.pending_llm_task.cancel()
        await session.stt.close()
        await session.tts.close()
        session.transcript = session.conversation.get_transcript_text()
        session.status = "completed"
        logger.info(f"Session cleaned up: call_id={call_id}")

    def release_session(self, call_id: str) -> None:
        """
        Remove a completed session from the in-memory dict.
        Call this after post-call analysis + DB update so the session can
        be garbage collected. Safe to call on unknown ids.
        """
        if self.active_calls.pop(call_id, None) is not None:
            logger.info(f"Session released from memory: call_id={call_id}")

    async def shutdown(self) -> None:
        """
        App-level teardown: close the shared LLM HTTP pool and any sessions
        still in memory. Called from the FastAPI shutdown event.
        """
        for cid in list(self.active_calls.keys()):
            session = self.active_calls.get(cid)
            if session:
                try:
                    await self._cleanup_session(session, cid)
                except Exception as e:
                    logger.warning(f"Shutdown: cleanup {cid} failed: {e}")
                self.active_calls.pop(cid, None)
        if self._shared_llm:
            try:
                await self._shared_llm.close()
            except Exception as e:
                logger.warning(f"Shutdown: LLM client close failed: {e}")
            self._shared_llm = None

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
