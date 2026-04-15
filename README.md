# Subscription Reminder Voice Agent

An AI-powered voice agent SaaS that automatically calls subscribers to remind them about upcoming subscription renewals. Built with a custom voice pipeline using **Twilio** (telephony), **Sarvam AI** (STT/TTS), and **Groq** (LLM).

## Features

- **Custom Voice Pipeline** - No dependency on managed platforms like Vapi or Retell. Full control over STT, TTS, LLM, and call flow
- **Indian Language Support** - Hindi, Gujarati, Tamil, Telugu, Bengali, Marathi, Kannada, Malayalam, Punjabi, Odia, and English (11 languages)
- **Real-time Streaming** - Sentence-boundary streaming for low-latency conversations (~200-400ms perceived latency)
- **Barge-in Detection** - Callers can interrupt the agent mid-sentence
- **Post-Call Analysis** - AI analyzes transcripts to classify subscriber responses
- **Pluggable Data Source** - Interface-based design; bring your own database

## Architecture

```
Twilio (outbound call)
   │
   ▼
wss://server/media-stream/{call_id}    ← Single unified WebSocket
   │
   │  Audio IN:  PCM/μ-law frames (caller voice)
   │  Audio OUT: Binary TTS frames (agent voice)
   │  JSON:      VAD events, barge-in, call metadata
   │
   ▼  (server-side)
   │
   ├──→ Sarvam STT  (wss://api.sarvam.ai/speech-to-text/ws)
   ├──→ Groq LLM    (streaming SSE, TTFT < 100ms)
   └──→ Sarvam TTS  (wss://api.sarvam.ai/text-to-speech/ws → mulaw 8kHz)
```

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (for dependency management)
- Twilio account with a phone number
- Sarvam AI API key
- Groq API key

## Quick Setup

1. **Clone and install**
   ```bash
   git clone https://github.com/Vatsa10/rem0
   cd rem0
   uv venv
   source .venv/bin/activate        # Linux/macOS
   # .venv\Scripts\activate         # Windows
   uv pip install -e .
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Fill in your API keys:
   ```
   TWILIO_ACCOUNT_SID=...
   TWILIO_AUTH_TOKEN=...
   TWILIO_FROM_NUMBER=+91...
   SARVAM_API_KEY=...
   GROQ_API_KEY=...
   SERVER_URL=https://your-ngrok-url.ngrok.io
   ```

3. **Run the server**
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```

4. **Expose publicly** (required for Twilio WebSocket callbacks)
   ```bash
   ngrok http 8000
   ```
   Update `SERVER_URL` in `.env` with the ngrok HTTPS URL.

## Usage

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/calls/initiate` | POST | Start calling subscribers |
| `/media-stream/{call_id}` | WebSocket | Twilio bidirectional audio stream |
| `/twiml/{call_id}` | POST | TwiML for Twilio callback (internal) |
| `/subscriptions/due` | GET | Preview subscriptions due for reminder |
| `/calls/{call_id}/status` | GET | Check call status |

### Example Requests

**Call subscribers directly (no data source needed):**
```json
POST /calls/initiate
{
  "subscribers": [
    {
      "id": "1",
      "name": "Raj Patel",
      "phone": "+919876543210",
      "subscription_id": "SUB-001",
      "subscription_type": "Netflix",
      "renewal_date": "2026-05-15",
      "amount": "₹649/month",
      "language": "gu-IN"
    }
  ]
}
```

**Load from data source by IDs:**
```json
POST /calls/initiate
{
  "subscription_ids": ["1", "2", "5"]
}
```

**Load subscriptions due within N days:**
```json
POST /calls/initiate
{
  "due_within_days": 30
}
```

### Call Results

After each call, the system generates:

| Field | Example |
|-------|---------|
| Status | RENEWED / FOLLOW_UP_NEEDED / NOT_INTERESTED / etc. |
| Response | Confirmed Renewal / Interested / Reschedule / Not Interested |
| Call Summary | AI-generated conversation summary |
| Notes | Justification for the response classification |
| Next Steps | Agreed follow-up actions |
| Transcript | Full call transcript |

### Supported Languages

| Code | Language |
|------|----------|
| `hi-IN` | Hindi |
| `gu-IN` | Gujarati |
| `en-IN` | English (Indian) |
| `ta-IN` | Tamil |
| `te-IN` | Telugu |
| `bn-IN` | Bengali |
| `mr-IN` | Marathi |
| `kn-IN` | Kannada |
| `ml-IN` | Malayalam |
| `pa-IN` | Punjabi |
| `od-IN` | Odia |

Set the `language` field on each subscriber to control the call language.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio account SID | |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | |
| `TWILIO_FROM_NUMBER` | Twilio phone number (E.164) | |
| `SARVAM_API_KEY` | Sarvam AI API key | |
| `LLM_PROVIDER` | LLM provider (`groq` or `sarvam`) | `groq` |
| `LLM_MODEL` | LLM model ID | `llama-3.3-70b-versatile` |
| `GROQ_API_KEY` | Groq API key | |
| `COMPANY_NAME` | Your company name | `Your Company` |
| `AGENT_NAME` | Voice agent's name | `Subscription Specialist` |
| `SERVER_URL` | Public server URL (for Twilio callbacks) | |
| `DEFAULT_LANGUAGE` | Default call language | `hi-IN` |

## Project Structure

```
├── app.py                              # FastAPI app with WebSocket endpoint
├── pyproject.toml                      # Project metadata and dependencies
├── .env.example                        # Environment variable template
└── src/
    ├── config.py                       # CallConfig, language mappings
    ├── automation.py                   # SubscriptionReminderAutomation
    ├── utils.py                        # Shared helpers
    ├── models/
    │   └── subscriber.py               # Subscriber model, SubscriptionStatus
    ├── data/
    │   └── base.py                     # SubscriptionLoaderBase (abstract)
    ├── providers/
    │   ├── base_agent.py               # BaseVoiceAgent (abstract)
    │   └── twilio_sarvam/
    │       ├── agent.py                # TwilioSarvamAgent (orchestrator)
    │       ├── twilio_handler.py       # Twilio WebSocket protocol
    │       ├── stt_client.py           # Sarvam STT streaming client
    │       ├── tts_client.py           # Sarvam TTS streaming client
    │       ├── llm_client.py           # Groq/Sarvam LLM streaming client
    │       └── audio_utils.py          # mulaw/PCM conversion
    ├── conversation/
    │   ├── manager.py                  # ConversationManager (state, transcript)
    │   └── prompts.py                  # Voice agent + analysis prompts
    └── tools/
        └── call_analysis.py            # Post-call transcript analysis
```

## Customization

### Bring Your Own Data Source

Implement `SubscriptionLoaderBase` to connect your database:

```python
from src.data.base import SubscriptionLoaderBase

class PostgresLoader(SubscriptionLoaderBase):
    def fetch_subscriptions(self, ids=None, status=None, due_within_days=None):
        # Query your database
        ...

    def update_subscription(self, subscription_id, updates):
        # Update your database
        ...
```

Then pass it when creating the automation:

```python
from src.automation import SubscriptionReminderAutomation
from src.config import CallConfig

config = CallConfig.from_env()
loader = PostgresLoader(connection_string="...")
automation = SubscriptionReminderAutomation(config=config, data_source=loader)
```

### Customize Prompts

Edit `src/conversation/prompts.py` to change the voice agent's behavior, conversation flow, or analysis criteria.

### Switch LLM Provider

Set `LLM_PROVIDER=sarvam` and provide the Sarvam API key to use Sarvam's Indian-language-optimized LLMs instead of Groq.
