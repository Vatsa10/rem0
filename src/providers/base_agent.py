from abc import ABC, abstractmethod


class BaseVoiceAgent(ABC):
    """Abstract base class for voice agent implementations."""

    @abstractmethod
    async def initiate_call(self, phone_number: str, subscriber_data: dict) -> dict:
        """
        Start an outbound call to the given phone number.

        Args:
            phone_number: E.164 formatted number (e.g., +919876543210).
            subscriber_data: Dict with subscriber info for the conversation.

        Returns:
            Call metadata dict (call_sid, status, etc.).
        """
        pass

    @abstractmethod
    async def handle_media_stream(self, websocket) -> None:
        """
        Handle the bidirectional media stream for a live call.

        This is the main loop that orchestrates audio in/out,
        STT, LLM, TTS, VAD, and barge-in on a single WebSocket.

        Args:
            websocket: The FastAPI WebSocket connection from Twilio.
        """
        pass

    @abstractmethod
    async def end_call(self, call_id: str) -> None:
        """
        Programmatically end an active call.

        Args:
            call_id: The Twilio call SID.
        """
        pass
