import logging
from datetime import datetime
from enum import Enum
from typing import List, Optional

from src.models.subscriber import Subscriber
from src.config import get_language_config
from .prompts import get_system_prompt

logger = logging.getLogger(__name__)

# Hard sentence terminators — always flush on these.
SENTENCE_TERMINATORS = (".", "?", "!", "।", "?", "؟")

# Soft phrase boundaries — flush if accumulated text has enough words.
PHRASE_BOUNDARIES = (",", ";", ":", "—", "–")

# Minimum word count before a phrase boundary triggers a flush.
# Too low = choppy speech. Too high = no latency benefit.
MIN_PHRASE_WORDS = 6


class ConversationState(str, Enum):
    GREETING = "greeting"
    ACTIVE = "active"
    CLOSING = "closing"
    ENDED = "ended"


class ConversationManager:
    """Manages conversation state, message history, and transcript for a single call."""

    def __init__(
        self,
        subscriber: Subscriber,
        company_name: str,
        agent_name: str,
    ):
        self.subscriber = subscriber
        self.company_name = company_name
        self.agent_name = agent_name
        self.language = subscriber.language
        self.lang_config = get_language_config(self.language)

        self.state = ConversationState.GREETING
        self.transcript: List[dict] = []
        self.message_history: List[dict] = []
        self.is_agent_speaking = False
        self._accumulated_text = ""

        system_prompt = get_system_prompt(
            subscriber=subscriber,
            company_name=company_name,
            agent_name=agent_name,
            language_hint=self.lang_config["llm_hint"],
        )
        self.message_history.append({"role": "system", "content": system_prompt})

    def build_messages(self, user_text: str) -> List[dict]:
        """Append user turn to history and return full message list for LLM."""
        self.transcript.append(
            {
                "role": "user",
                "text": user_text,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self.message_history.append({"role": "user", "content": user_text})
        self.state = ConversationState.ACTIVE
        return self.message_history

    def record_agent_turn(self, text: str) -> None:
        """Record what the agent said for transcript and message history."""
        self.transcript.append(
            {
                "role": "agent",
                "text": text,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self.message_history.append({"role": "assistant", "content": text})

    def accumulate_token(self, token: str) -> Optional[str]:
        """
        Accumulate LLM tokens and return a speakable chunk when a boundary is hit.

        Priority:
        1. Hard sentence terminator (. ? ! etc.) — always flush.
        2. Phrase boundary (, ; :) — flush if chunk has >= MIN_PHRASE_WORDS.

        Returns the chunk text when ready, or None if still accumulating.
        """
        self._accumulated_text += token

        # Hard boundary — always flush.
        for term in SENTENCE_TERMINATORS:
            if term in self._accumulated_text:
                idx = self._accumulated_text.rindex(term)
                chunk = self._accumulated_text[: idx + 1].strip()
                self._accumulated_text = self._accumulated_text[idx + 1 :].strip()
                if chunk:
                    return chunk

        # Soft phrase boundary — flush if long enough for natural cadence.
        for boundary in PHRASE_BOUNDARIES:
            if boundary in self._accumulated_text:
                idx = self._accumulated_text.rindex(boundary)
                candidate = self._accumulated_text[: idx + 1].strip()
                if len(candidate.split()) >= MIN_PHRASE_WORDS:
                    self._accumulated_text = self._accumulated_text[idx + 1 :].strip()
                    return candidate

        return None

    def flush_accumulated(self) -> Optional[str]:
        """Flush any remaining accumulated text (end of LLM response)."""
        text = self._accumulated_text.strip()
        self._accumulated_text = ""
        return text if text else None

    def handle_barge_in(self) -> None:
        """User spoke while agent was speaking — mark for interruption handling."""
        self.is_agent_speaking = False
        self._accumulated_text = ""
        logger.debug("Barge-in detected")

    def get_transcript_text(self) -> str:
        """Return full transcript as formatted text for post-call analysis."""
        lines = []
        for entry in self.transcript:
            role = "Agent" if entry["role"] == "agent" else "Customer"
            lines.append(f"{role}: {entry['text']}")
        return "\n".join(lines)

    def get_transcript(self) -> List[dict]:
        """Return raw transcript entries."""
        return self.transcript
