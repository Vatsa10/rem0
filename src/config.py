import os
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


# Sarvam bulbul:v2 WebSocket TTS voices (April 2026):
# Female: anushka, manisha, vidya, arya
# Male:   abhilash, hitesh, karun
# (bulbul:v3 has 40+ voices but the WS endpoint currently forces v2)
LANGUAGE_CONFIGS = {
    "hi-IN": {
        "stt_code": "hi-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Hindi (Devanagari script). Keep responses conversational and natural.",
    },
    "gu-IN": {
        "stt_code": "gu-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Gujarati (Gujarati script). Keep responses conversational and natural.",
    },
    "en-IN": {
        "stt_code": "en-IN",
        "tts_voice": "hitesh",
        "llm_hint": "Respond in English with an Indian conversational style.",
    },
    "ta-IN": {
        "stt_code": "ta-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Tamil (Tamil script). Keep responses conversational and natural.",
    },
    "te-IN": {
        "stt_code": "te-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Telugu (Telugu script). Keep responses conversational and natural.",
    },
    "bn-IN": {
        "stt_code": "bn-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Bengali (Bengali script). Keep responses conversational and natural.",
    },
    "mr-IN": {
        "stt_code": "mr-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Marathi (Devanagari script). Keep responses conversational and natural.",
    },
    "kn-IN": {
        "stt_code": "kn-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Kannada (Kannada script). Keep responses conversational and natural.",
    },
    "ml-IN": {
        "stt_code": "ml-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Malayalam (Malayalam script). Keep responses conversational and natural.",
    },
    "pa-IN": {
        "stt_code": "pa-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Punjabi (Gurmukhi script). Keep responses conversational and natural.",
    },
    "od-IN": {
        "stt_code": "od-IN",
        "tts_voice": "anushka",
        "llm_hint": "Respond in Odia (Odia script). Keep responses conversational and natural.",
    },
}


class CallConfig(BaseModel):
    language: str = "hi-IN"
    sarvam_api_key: str
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
