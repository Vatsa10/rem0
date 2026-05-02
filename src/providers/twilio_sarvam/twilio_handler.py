import asyncio
import json
import logging
from typing import Optional
from dataclasses import dataclass, field

from starlette.websockets import WebSocket

from .audio_utils import decode_twilio_audio, encode_for_twilio

logger = logging.getLogger(__name__)


@dataclass
class TwilioStreamState:
    """Tracks the state of a Twilio media stream."""
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    account_sid: Optional[str] = None
    connected: bool = False
    custom_parameters: dict = field(default_factory=dict)


class TwilioMediaStreamHandler:
    """Handles Twilio WebSocket media stream protocol."""

    def __init__(self):
        self.state = TwilioStreamState()
        # Set once Twilio sends the `start` event (gives us stream_sid).
        # Producers must await this before calling send_audio/send_mark/send_clear
        # — otherwise send calls silently drop because stream_sid is None.
        self._ready_event = asyncio.Event()
        # Latch so we only WARN once per call about a missing stream_sid,
        # rather than spamming logs on every dropped frame.
        self._warned_no_stream_sid = False

    async def wait_until_ready(self, timeout: float = 5.0) -> bool:
        """Block until the `start` event has been seen (stream_sid available)."""
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def parse_message(self, raw_message: str) -> dict:
        """Parse a Twilio WebSocket message and update state."""
        message = json.loads(raw_message)
        event = message.get("event")

        if event == "connected":
            self.state.connected = True
            logger.info("Twilio stream connected")

        elif event == "start":
            start_data = message.get("start", {})
            self.state.stream_sid = message.get("streamSid")
            self.state.call_sid = start_data.get("callSid")
            self.state.account_sid = start_data.get("accountSid")
            self.state.custom_parameters = start_data.get("customParameters", {})
            self._ready_event.set()
            logger.info(
                f"Twilio stream started: stream_sid={self.state.stream_sid}, "
                f"call_sid={self.state.call_sid}"
            )

        elif event == "media":
            media = message.get("media", {})
            payload = media.get("payload", "")
            message["_decoded_audio"] = decode_twilio_audio(payload)

        elif event == "stop":
            self.state.connected = False
            logger.info(f"Twilio stream stopped: stream_sid={self.state.stream_sid}")

        elif event == "mark":
            logger.debug(f"Twilio mark received: {message.get('mark', {}).get('name')}")

        return message

    async def send_audio(self, websocket: WebSocket, audio_bytes: bytes) -> bool:
        """
        Send audio to Twilio as a media message.

        Returns True on a successful send, False if the stream_sid isn't
        available yet (frame silently dropped — caller can detect this and
        decide whether to retry, abort, or surface the issue).
        """
        if not self.state.stream_sid:
            if not self._warned_no_stream_sid:
                self._warned_no_stream_sid = True
                logger.warning(
                    "send_audio called before Twilio `start` event — "
                    "frame dropped; producer should await wait_until_ready()"
                )
            return False
        message = {
            "event": "media",
            "streamSid": self.state.stream_sid,
            "media": {
                "payload": encode_for_twilio(audio_bytes),
            },
        }
        await websocket.send_json(message)
        return True

    async def send_mark(self, websocket: WebSocket, mark_name: str) -> None:
        """Send a mark message to track audio playback completion."""
        if not self.state.stream_sid:
            return
        message = {
            "event": "mark",
            "streamSid": self.state.stream_sid,
            "mark": {"name": mark_name},
        }
        await websocket.send_json(message)

    async def send_clear(self, websocket: WebSocket) -> None:
        """Clear Twilio's audio queue (for barge-in)."""
        if not self.state.stream_sid:
            return
        message = {
            "event": "clear",
            "streamSid": self.state.stream_sid,
        }
        await websocket.send_json(message)
        logger.debug("Sent clear to Twilio (barge-in)")
