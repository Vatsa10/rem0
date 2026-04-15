import base64
import sys

if sys.version_info >= (3, 13):
    import audioop_lts as audioop
else:
    import audioop


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """Convert mulaw 8kHz (from Twilio) to PCM s16le 8kHz (for Sarvam STT)."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert PCM s16le to mulaw (fallback if TTS doesn't output mulaw)."""
    return audioop.lin2ulaw(pcm_bytes, 2)


def decode_twilio_audio(payload: str) -> bytes:
    """Decode base64 mulaw audio from a Twilio media message."""
    return base64.b64decode(payload)


def encode_for_twilio(audio_bytes: bytes) -> str:
    """Encode audio bytes as base64 for sending to Twilio."""
    return base64.b64encode(audio_bytes).decode("utf-8")
