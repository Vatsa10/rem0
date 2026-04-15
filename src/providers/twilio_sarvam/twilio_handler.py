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

    async def send_audio(self, websocket: WebSocket, audio_bytes: bytes) -> None:
        """Send audio to Twilio as a media message."""
        if not self.state.stream_sid:
            return
        message = {
            "event": "media",
            "streamSid": self.state.stream_sid,
            "media": {
                "payload": encode_for_twilio(audio_bytes),
            },
        }
        await websocket.send_json(message)

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
