import os
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# Provider codes for the low-latency WS stack:
#   STT = Deepgram Nova    (stt_lang — Deepgram language code)
#   TTS = Cartesia Sonic   (tts_lang — Cartesia language code)
#
# Voice: Cartesia voices are MULTILINGUAL — one voice id speaks every Cartesia
# language via the `language` param — so the voice id comes from a single env
# var (CARTESIA_VOICE_ID), not per-language here.
#
# IMPORTANT coverage note: Cartesia Sonic supports only en + hi among Indian
# languages (full list: en, fr, de, es, pt, zh, ja, hi, it, ko, nl, pl, ru,
# sv, tr). Languages without native Cartesia support fall back to tts_lang
# "hi" so the call doesn't crash — the audio won't be correct for those. If
# you need Tamil/Telugu/etc TTS, a different provider (Azure/ElevenLabs) is
# required. Deepgram STT uses "multi" for Indian code-switching (Hinglish).
LANGUAGE_CONFIGS = {
    "hi-IN": {
        "stt_lang": "hi",
        "tts_lang": "hi",
        "llm_hint": "Respond in Hindi (Devanagari script). Keep responses conversational and natural.",
    },
    "gu-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Gujarati — falls back to Hindi
        "llm_hint": "Respond in Gujarati (Gujarati script). Keep responses conversational and natural.",
    },
    "en-IN": {
        "stt_lang": "en-IN",
        "tts_lang": "en",
        "llm_hint": "Respond in English with an Indian conversational style.",
    },
    "ta-IN": {
        "stt_lang": "ta",
        "tts_lang": "hi",  # Cartesia: no Tamil — falls back to Hindi
        "llm_hint": "Respond in Tamil (Tamil script). Keep responses conversational and natural.",
    },
    "te-IN": {
        "stt_lang": "te",
        "tts_lang": "hi",  # Cartesia: no Telugu — falls back to Hindi
        "llm_hint": "Respond in Telugu (Telugu script). Keep responses conversational and natural.",
    },
    "bn-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Bengali — falls back to Hindi
        "llm_hint": "Respond in Bengali (Bengali script). Keep responses conversational and natural.",
    },
    "mr-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Marathi — falls back to Hindi
        "llm_hint": "Respond in Marathi (Devanagari script). Keep responses conversational and natural.",
    },
    "kn-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Kannada — falls back to Hindi
        "llm_hint": "Respond in Kannada (Kannada script). Keep responses conversational and natural.",
    },
    "ml-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Malayalam — falls back to Hindi
        "llm_hint": "Respond in Malayalam (Malayalam script). Keep responses conversational and natural.",
    },
    "pa-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Punjabi — falls back to Hindi
        "llm_hint": "Respond in Punjabi (Gurmukhi script). Keep responses conversational and natural.",
    },
    "od-IN": {
        "stt_lang": "multi",
        "tts_lang": "hi",  # Cartesia: no Odia — falls back to Hindi
        "llm_hint": "Respond in Odia (Odia script). Keep responses conversational and natural.",
    },
}


class CallConfig(BaseModel):
    language: str = "hi-IN"
    # STT = Deepgram, TTS = Cartesia (low-latency WS stack). sarvam_api_key
    # kept optional for backward compat / cached-greeting reuse only.
    sarvam_api_key: str = ""
    deepgram_api_key: str = ""
    cartesia_api_key: str = ""
    cartesia_voice_id: str = ""
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_fast_model: str = "llama-3.1-8b-instant"
    llm_api_key: str
    server_url: str
    company_name: str = "Your Company"
    agent_name: str = "Subscription Specialist"

    @classmethod
    def from_env(cls, language: str = "hi-IN") -> "CallConfig":
        return cls(
            language=language,
            sarvam_api_key=os.getenv("SARVAM_API_KEY", ""),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            cartesia_api_key=os.getenv("CARTESIA_API_KEY", ""),
            cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID", ""),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            twilio_from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
            llm_provider=os.getenv("LLM_PROVIDER", "groq"),
            llm_model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            llm_fast_model=os.getenv("LLM_FAST_MODEL", "llama-3.1-8b-instant"),
            llm_api_key=os.getenv("GROQ_API_KEY", ""),
            server_url=os.getenv("SERVER_URL", ""),
            company_name=os.getenv("COMPANY_NAME", "Your Company"),
            agent_name=os.getenv("AGENT_NAME", "Subscription Specialist"),
        )

    @classmethod
    async def from_db(cls, db_session) -> "CallConfig":
        """Load settings from DB, API keys from .env."""
        from src.database.models import Settings
        settings = await db_session.get(Settings, 1)
        return cls(
            language=settings.default_language if settings else "hi-IN",
            company_name=settings.company_name if settings else "Your Company",
            agent_name=settings.agent_name if settings else "Subscription Specialist",
            sarvam_api_key=os.getenv("SARVAM_API_KEY", ""),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", ""),
            cartesia_api_key=os.getenv("CARTESIA_API_KEY", ""),
            cartesia_voice_id=os.getenv("CARTESIA_VOICE_ID", ""),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            twilio_from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
            llm_provider=os.getenv("LLM_PROVIDER", "groq"),
            llm_model=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            llm_fast_model=os.getenv("LLM_FAST_MODEL", "llama-3.1-8b-instant"),
            llm_api_key=os.getenv("GROQ_API_KEY", ""),
            server_url=os.getenv("SERVER_URL", ""),
        )


def get_language_config(language: str) -> dict:
    return LANGUAGE_CONFIGS.get(language, LANGUAGE_CONFIGS["hi-IN"])
