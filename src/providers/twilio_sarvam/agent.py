import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from starlette.websockets import WebSocket, WebSocketDisconnect
from twilio.rest import Client as TwilioClient

from src.config import CallConfig, get_language_config
from src.models.subscriber import Subscriber
from src.conversation.manager import ConversationManager
from src.providers.base_agent import BaseVoiceAgent
from .twilio_handler import TwilioMediaStreamHandler
from .stt_client import SarvamSTTClient
from .tts_client import SarvamTTSClient
from .llm_client import LLMClient
from .audio_utils import mulaw_to_pcm

logger = logging.getLogger(__name__)


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
    transcript: str = ""
    status: str = "in_progress"


class TwilioSarvamAgent(BaseVoiceAgent):
    """
    Voice agent that orchestrates Twilio Media Streams with Sarvam STT/TTS and Groq LLM.

    Single WebSocket connection from Twilio carries all audio and events.
    Server-side, we manage separate Sarvam STT, TTS, and LLM connections.
    """

    def __init__(self, config: CallConfig):
        self.config = config
        self.twilio_client = TwilioClient(
            config.twilio_account_sid, config.twilio_auth_token
        )
        self.active_calls: Dict[str, CallSession] = {}

    async def initiate_call(
        self, phone_number: str, subscriber_data: dict
    ) -> dict:
        """
        Place an outbound call via Twilio REST API.
        Twilio will request TwiML from /twiml/{call_id} which sets up the media stream.
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
        llm = LLMClient(
            provider=self.config.llm_provider,
            model=self.config.llm_model,
            api_key=self.config.llm_api_key,
        )
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

    async def handle_media_stream(self, websocket: WebSocket, call_id: str = None) -> None:
        """
        Main WebSocket handler for a Twilio bidirectional media stream.

        This is the unified channel: audio in, audio out, VAD, barge-in — all on one WS.
        """
        await websocket.accept()

        session = self.active_calls.get(call_id)
        if not session:
            logger.error(f"No session found for call_id={call_id}")
            await websocket.close()
            return

        try:
            await session.stt.connect()
            await session.tts.connect()

            stt_task = asyncio.create_task(
                self._process_stt_events(session, websocket)
            )
            greeting_task = asyncio.create_task(
                self._send_greeting(session, websocket)
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

                        if session.conversation.is_agent_speaking:
                            session.conversation.handle_barge_in()
                            session.tts.cancel()
                            await handler.send_clear(websocket)

                elif event == "stop":
                    break

            except WebSocketDisconnect:
                break

    async def _process_stt_events(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """Process STT transcript events and generate LLM + TTS responses."""
        async for event in session.stt.receive_events():
            event_type = event.get("type", "")
            transcript = event.get("transcript", "")

            if not transcript or not transcript.strip():
                continue

            logger.debug(f"STT transcript: {transcript}")

            messages = session.conversation.build_messages(transcript)

            full_response = ""
            session.conversation.is_agent_speaking = True

            async for token in session.llm.chat_completion_stream(
                messages=messages, temperature=0.7, max_tokens=256
            ):
                if not session.conversation.is_agent_speaking:
                    break

                sentence = session.conversation.accumulate_token(token)
                full_response += token

                if sentence:
                    await self._speak(session, websocket, sentence)

            remaining = session.conversation.flush_accumulated()
            if remaining and session.conversation.is_agent_speaking:
                full_response += ""
                await self._speak(session, websocket, remaining)

            if full_response:
                session.conversation.record_agent_turn(full_response)

            session.conversation.is_agent_speaking = False

    async def _send_greeting(
        self, session: CallSession, websocket: WebSocket
    ) -> None:
        """Generate and speak the initial greeting when the call connects."""
        await asyncio.sleep(1.0)

        greeting_prompt = (
            "Generate your opening greeting for this call. "
            "Introduce yourself and the company, and ask to confirm you're speaking with the right person."
        )
        messages = session.conversation.build_messages(greeting_prompt)
        messages[-1]["role"] = "user"
        messages[-1]["content"] = "[SYSTEM: Call connected. Deliver your opening greeting.]"

        full_response = ""
        session.conversation.is_agent_speaking = True

        async for token in session.llm.chat_completion_stream(
            messages=messages, temperature=0.7, max_tokens=150
        ):
            sentence = session.conversation.accumulate_token(token)
            full_response += token
            if sentence:
                await self._speak(session, websocket, sentence)

        remaining = session.conversation.flush_accumulated()
        if remaining:
            await self._speak(session, websocket, remaining)

        if full_response:
            session.conversation.record_agent_turn(full_response)
            session.conversation.message_history.pop(-2)
            session.conversation.message_history[-1] = {
                "role": "assistant",
                "content": full_response,
            }

        session.conversation.is_agent_speaking = False

    async def _speak(
        self, session: CallSession, websocket: WebSocket, text: str
    ) -> None:
        """Send text to TTS and stream audio back to Twilio."""
        mark_id = str(uuid.uuid4())[:8]

        async for audio_chunk in session.tts.synthesize(text):
            if not session.conversation.is_agent_speaking:
                break
            await session.twilio_handler.send_audio(websocket, audio_chunk)

        await session.twilio_handler.send_mark(websocket, mark_id)

    async def _cleanup_session(self, session: CallSession, call_id: str) -> None:
        """Close all connections and clean up session state."""
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
        """Get a call session by ID."""
        return self.active_calls.get(call_id)

    def get_twiml(self, call_id: str) -> str:
        """Generate TwiML that connects the call to our WebSocket media stream."""
        ws_url = f"{self.config.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}/media-stream/{call_id}"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
</Response>"""
