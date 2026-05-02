import logging
import re
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
    GREETING = "greeting"                # opening, confirming identity
    RENEWAL_PITCH = "renewal_pitch"      # delivering the reminder
    HANDLING_OBJECTION = "handling_objection"  # addressing concerns / questions
    COLLECTING_PAYMENT = "collecting_payment"  # payment method flow
    CLOSING = "closing"                  # wrapping up
    ENDED = "ended"


# Heuristic state transitions based on what just happened.
# Runs after each turn; the LLM gets the new state in the next system prompt.
_STATE_KEYWORDS = {
    ConversationState.COLLECTING_PAYMENT: [
        "payment", "upi", "card", "gpay", "paytm", "phonepe", "net banking",
        "credit", "debit", "pay by", "link",
    ],
    ConversationState.CLOSING: [
        "bye", "thank you", "have a great", "that's all", "talk later",
        "no thanks",
    ],
    ConversationState.HANDLING_OBJECTION: [
        "why", "how much", "what is", "can i", "do you", "is there",
        "not interested", "expensive", "too much", "cancel",
    ],
}

# Caller said yes / agreed in some form. Used to advance RENEWAL_PITCH →
# COLLECTING_PAYMENT once the caller has signalled they want to renew, so
# the LLM stops re-pitching and moves to payment collection.
_AFFIRMATIVE_KEYWORDS = [
    "yes", "yeah", "yep", "yup", "sure", "okay", "ok", "alright",
    "absolutely", "definitely", "of course", "go ahead", "please do",
    "subscribe", "renew", "confirm", "agree", "agreed", "let's do it",
    "sounds good", "i'm in", "count me in",
]


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
        # Monotonic id incremented every time a barge-in fires. Speakers
        # capture the id at the start of an utterance and bail out if the
        # current id no longer matches — race-free vs. is_agent_speaking,
        # which gets reset by `finally:` blocks even on normal completion
        # and can be observed False mid-stream by concurrent tasks.
        self.speak_session_id: int = 0
        self._accumulated_text = ""

        # Barge-in / flow memory — populated after every agent turn.
        # Used to inject a "call state" block into the next system prompt
        # so the LLM knows what was said, what was cut off, and the current goal.
        self.last_turn_heard: str = ""          # what caller actually heard last turn
        self.last_turn_generated: str = ""      # what LLM produced last turn
        self.last_turn_interrupted: bool = False
        self.last_unsaid: str = ""              # portion generated but not heard
        self.goal: str = (
            "Remind the subscriber about their upcoming renewal and help them "
            "decide whether to renew."
        )

        system_prompt = get_system_prompt(
            subscriber=subscriber,
            company_name=company_name,
            agent_name=agent_name,
            language_hint=self.lang_config["llm_hint"],
        )
        self.message_history.append({"role": "system", "content": system_prompt})

    @staticmethod
    def _contains_any_word(text: str, keywords: list) -> bool:
        """
        Word-boundary keyword match. Prevents 'bye' in 'goodbye' or 'maybe'
        from triggering the CLOSING state. Multi-word keywords ('not interested')
        are matched as a phrase.
        """
        if not text:
            return False
        lowered = text.lower()
        for kw in keywords:
            if " " in kw:
                # Multi-word phrase — substring match is fine
                if kw in lowered:
                    return True
            else:
                # Single word — require word boundaries on both sides
                if re.search(rf"\b{re.escape(kw)}\b", lowered):
                    return True
        return False

    def _update_state(self, last_user_text: str, last_agent_text: str) -> None:
        """Advance the state machine based on the most recent exchange."""
        combined = f"{last_user_text} {last_agent_text}"
        # Hard priority: closing wins — but only on a word-boundary match
        # ('bye' must be a standalone word, not buried in 'maybe' etc).
        if self._contains_any_word(
            last_user_text, _STATE_KEYWORDS[ConversationState.CLOSING]
        ):
            self.state = ConversationState.CLOSING
            return

        # Caller affirmed during the renewal pitch → advance to payment.
        # Without this, the LLM stays in RENEWAL_PITCH and keeps re-asking
        # "would you like to go ahead?" even after the caller said yes.
        if self.state is ConversationState.RENEWAL_PITCH and self._contains_any_word(
            last_user_text, _AFFIRMATIVE_KEYWORDS
        ):
            self.state = ConversationState.COLLECTING_PAYMENT
            self.goal = (
                "Caller has confirmed renewal. Collect payment method next "
                "(UPI / card / net banking) and offer to send a payment link."
            )
            return

        for state, keywords in _STATE_KEYWORDS.items():
            if state is ConversationState.CLOSING:
                continue
            if self._contains_any_word(combined, keywords):
                self.state = state
                return
        # Default progression: GREETING → RENEWAL_PITCH after first exchange.
        if self.state is ConversationState.GREETING and last_user_text:
            self.state = ConversationState.RENEWAL_PITCH

    def state_block(self) -> str:
        """
        Render a compact state block to inject into the system prompt each turn.
        Gives the LLM explicit memory of where the call is and what was interrupted.
        """
        parts = [
            f"Current state: {self.state.value}",
            f"Goal: {self.goal}",
        ]
        if self.last_turn_interrupted and self.last_unsaid:
            parts.append(
                f"Your last reply was INTERRUPTED. You already said: "
                f"{self.last_turn_heard!r}. You were cut off before saying: "
                f"{self.last_unsaid!r}. Don't repeat what was already said; "
                f"address the caller's interruption, then continue the flow "
                f"only if still relevant."
            )
        elif self.last_turn_heard:
            parts.append(
                f"Your last reply (fully delivered): {self.last_turn_heard!r}. "
                f"DO NOT repeat it or re-pitch the same content. Move the call "
                f"forward — answer the caller's latest input or advance the goal."
            )
        return "\n".join(f"- {p}" for p in parts)

    def build_messages(self, user_text: str) -> List[dict]:
        """
        Append user turn to history and return message list for LLM with a
        fresh "Call State" block injected so the LLM always has current goal
        and any interruption memory.

        Consecutive user turns get COLLAPSED into one message — Sarvam STT
        often splits a continuous utterance ("Yes, I would like to subscribe.")
        into multiple final transcripts as VAD fires on micro-pauses
        ("Yes, I would like." → "Like to subscribe."). Treating each as a
        separate user turn produces consecutive `role: user` messages, which
        confuse the LLM into re-pitching instead of advancing the call.
        """
        self.transcript.append(
            {
                "role": "user",
                "text": user_text,
                "timestamp": datetime.now().isoformat(),
            }
        )

        if self.message_history and self.message_history[-1]["role"] == "user":
            prev = self.message_history[-1]["content"].strip()
            curr = user_text.strip()
            # Common STT cascade: each new transcript is a longer/cleaner
            # version of the prior one. Prefer the longer text.
            if curr.lower().startswith(prev.lower()) or len(curr) >= len(prev):
                self.message_history[-1]["content"] = curr
            elif prev.lower().startswith(curr.lower()):
                # Already have superset; keep it.
                pass
            else:
                # Genuinely additive — concatenate so the LLM sees the full
                # statement rather than fragments.
                self.message_history[-1]["content"] = f"{prev} {curr}"
        else:
            self.message_history.append({"role": "user", "content": user_text})

        # Clone history with a state-augmented system prompt so the LLM always
        # has fresh memory of where the call is — without mutating the stored
        # history (which would bloat every subsequent call).
        augmented = list(self.message_history)
        base_system = augmented[0]["content"] if augmented else ""
        augmented[0] = {
            "role": "system",
            "content": f"{base_system}\n\n## Call State (live)\n{self.state_block()}",
        }
        return augmented

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

    def record_turn_result(
        self,
        user_text: str,
        generated: str,
        heard: str,
    ) -> None:
        """
        Record a full turn outcome (both what the LLM produced and what
        the caller actually heard). Updates barge-in memory + state machine.

        Call this once per turn AFTER _speak returns.
        """
        generated = (generated or "").strip()
        heard = (heard or "").strip()

        self.last_turn_generated = generated
        self.last_turn_heard = heard
        self.last_turn_interrupted = bool(heard) and heard != generated
        # The tail of `generated` not present in `heard`.
        if self.last_turn_interrupted:
            if generated.startswith(heard):
                self.last_unsaid = generated[len(heard):].lstrip(" ,.;:—–-")
            else:
                # Word-boundary fallback if the prefix isn't exact.
                heard_words = heard.split()
                gen_words = generated.split()
                if len(gen_words) > len(heard_words):
                    self.last_unsaid = " ".join(gen_words[len(heard_words):])
                else:
                    self.last_unsaid = ""
        else:
            self.last_unsaid = ""

        if heard:
            self.record_agent_turn(heard)

        self._update_state(user_text, heard)

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

    def bump_speak_session(self) -> int:
        """
        Increment the speak session id. Returns the NEW id. Any speaker
        currently looping on the previous id will see the mismatch on its
        next iteration and stop.
        """
        self.speak_session_id += 1
        return self.speak_session_id

    def handle_barge_in(self) -> None:
        """User spoke while agent was speaking — mark for interruption handling."""
        self.is_agent_speaking = False
        self.bump_speak_session()
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
