# Rem0 — Subscription Reminder Voice Agent

An AI-powered voice agent SaaS that automatically calls subscribers to remind them about upcoming subscription renewals. Built with a custom low-latency voice pipeline using **Twilio** (telephony), **Sarvam AI** (STT/TTS for 11 Indian languages), and **Groq** (fast LLM).

Ships with a **Next.js dashboard** (shadcn/ui) and **SQLite** persistence out of the box.

---

## Highlights

- **~500ms–1.5s perceived latency** per conversational turn (see [Latency](#latency))
- **Instant greeting** via Redis-cached TTS audio (zero generation cost after first call)
- **11 Indian languages** — Hindi, Gujarati, Tamil, Telugu, Bengali, Marathi, Kannada, Malayalam, Punjabi, Odia, and English
- **Single WebSocket design** — one bidirectional channel carries audio, VAD, barge-in, and metadata
- **Full-stack SaaS UI** — Dashboard, Subscribers CRUD, Call History with transcripts, Settings
- **Quick Call** — place an ad-hoc call from the UI; subscriber auto-saved so you can call again from history
- **Post-call analysis** — LLM classifies each call (Confirmed Renewal / Interested / Reschedule / Not Interested / etc.) and stores transcript + analysis

---

## Architecture

```
Browser  ←── Next.js Dashboard (Tailwind + shadcn/ui)
  │
  │  fetch /api/*
  ▼
FastAPI Backend (Python)
  │
  ├─ SQLite (subscribers · calls · settings)
  ├─ Redis/Memurai (greeting audio cache)
  └─ Twilio REST API (outbound calls)

Twilio  ←──  wss://server/media-stream/{call_id}  (single unified WebSocket)
                │  IN:  caller audio (mulaw 8kHz, base64)
                │  OUT: agent audio (mulaw 8kHz, base64)
                │  JSON: VAD events, barge-in, call metadata
                ▼
         ┌──────────────────┬──────────────────┐
         │                  │                  │
         ▼                  ▼                  ▼
    Sarvam STT WS      Groq LLM (SSE)      Sarvam TTS WS
    (saaras:v3)        (llama-3.3-70b /     (bulbul:v2,
                        llama-3.1-8b)        mulaw 8kHz)
```

---

## Latency

**Total perceived latency** (caller finishes speaking → hears agent reply start):

| Path | Typical Latency |
|------|----------------|
| **First response (cached greeting)** | **~0 ms** (served from Redis — no LLM/TTS call) |
| **First response (cold / new config)** | ~800–1200 ms |
| **Subsequent turns (first audio heard)** | ~500–1000 ms |
| **Barge-in reaction** | <100 ms (VAD-triggered clear) |

### What makes it fast

| Optimization | Savings |
|--------------|---------|
| **Pre-warmed WebSockets** — STT/TTS connected during Twilio ring time | ~500ms–1s |
| **Cached greeting audio** in Redis — keyed by `(lang, voice, company, agent)` | Full greeting TTS cost eliminated after first call |
| **Fast model for greetings** — `llama-3.1-8b-instant` (TTFT ~40ms) with fallback to main model | ~60ms vs 70B |
| **Phrase-boundary streaming** — flushes to TTS on commas after 6+ words, not just periods | ~300–500ms per long reply |
| **Persistent httpx client with HTTP/2** — one pooled connection across calls | ~50–100ms per request (skip TLS handshake) |
| **Native mulaw 8kHz from TTS** — zero audio conversion on the outbound path | ~20ms per chunk |
| **VAD-based turn detection** — Sarvam's `speech_start`/`speech_end` events drive barge-in | <100ms reaction |
| **STT audio buffering** (200ms chunks) — avoids sub-100ms fragments that can't be transcribed | Unlocks reliable recognition |

---

## Prerequisites

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** for Python dependency management
- **Node.js 18+** (for the frontend)
- **Redis** or **Memurai** running on `localhost:6379` (for the greeting cache)
- **Twilio account** with a phone number
- **Sarvam AI API key** (STT + TTS)
- **Groq API key** (LLM)

---

## Quick Setup

### 1. Backend

```bash
git clone https://github.com/Vatsa10/rem0
cd rem0
uv venv
source .venv/Scripts/activate     # Windows (Git Bash)
# source .venv/bin/activate       # Linux/macOS
uv pip install -e .
```

### 2. Environment

```bash
cp .env.example .env
```

Fill in `.env`:
```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...

SARVAM_API_KEY=sk_...

LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
LLM_FAST_MODEL=llama-3.1-8b-instant
GROQ_API_KEY=gsk_...

REDIS_URL=redis://localhost:6379/0
SERVER_URL=https://your-ngrok-url.ngrok.io
```

> Non-API-key settings (`COMPANY_NAME`, `AGENT_NAME`, `DEFAULT_LANGUAGE`, `DAYS_BEFORE_RENEWAL`, `DAYS_BETWEEN_CALLS`) are **stored in SQLite** and edited from the Settings page — not from `.env`.

### 3. Run the backend

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

On first run, `data.db` (SQLite) is auto-created with default settings.

### 4. Expose publicly for Twilio webhooks

```bash
ngrok http 8000
```
Paste the HTTPS URL into `SERVER_URL` in `.env` and restart the backend.

### 5. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs on `http://localhost:3000`.

---

## Features

### Frontend (Next.js + shadcn/ui)

| Page | What it does |
|------|--------------|
| **Dashboard** (`/`) | 4 stat cards (total subscribers, active, calls today/week, renewal rate), recent calls table with one-click "Call Again", upcoming renewals, and a **Quick Call** form |
| **Subscribers** (`/subscribers`) | Full CRUD — add/edit/delete, search, filter by status, status badges, per-row Call button |
| **Call History** (`/calls`) | All calls with expandable rows showing transcript (color-coded Agent/Customer), summary, justification, next steps, "Call Again" button |
| **Settings** (`/settings`) | Edit company name, agent name, default language (11 options), days before renewal, days between calls |

**Quick Call** flow: enter name + phone + subscription_id → backend persists the subscriber to SQLite → initiates the call → the call shows up in history so you can retry from there.

### Backend API

**Voice pipeline:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/calls/initiate` | POST | Start calling (direct payload, by IDs, or by due date) |
| `/twiml/{call_id}` | POST | TwiML response for Twilio (internal) |
| `/media-stream/{call_id}` | WebSocket | Unified audio + VAD + barge-in channel |
| `/calls/{call_id}/status` | GET | Check live/completed call status |
| `/subscriptions/due` | GET | Preview subscriptions due within N days |

**REST (CRUD for the frontend):**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/subscribers` | GET / POST | List (search, filter, paginate) / create |
| `/api/subscribers/{id}` | GET / PUT / DELETE | Get / update / delete |
| `/api/calls` | GET | Call history with filters |
| `/api/calls/{call_id}` | GET | One call with full transcript |
| `/api/settings` | GET / PUT | Read or update app settings (resets in-memory automation cache) |
| `/api/dashboard` | GET | Aggregate stats + recent calls + upcoming renewals |

---

## Example Requests

### Direct ad-hoc call (no DB record needed first)
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

### Call existing subscribers by DB ID
```json
POST /calls/initiate
{ "subscription_ids": ["1", "2", "5"] }
```

### Call all subscriptions renewing in next N days
```json
POST /calls/initiate
{ "due_within_days": 30 }
```

---

## Supported Languages & Voices

| Code | Language | Default Voice |
|------|----------|---------------|
| `hi-IN` | Hindi | anushka |
| `gu-IN` | Gujarati | anushka |
| `en-IN` | English (Indian) | hitesh |
| `ta-IN` | Tamil | anushka |
| `te-IN` | Telugu | anushka |
| `bn-IN` | Bengali | anushka |
| `mr-IN` | Marathi | anushka |
| `kn-IN` | Kannada | anushka |
| `ml-IN` | Malayalam | anushka |
| `pa-IN` | Punjabi | anushka |
| `od-IN` | Odia | anushka |

**Available bulbul:v2 voices** (Sarvam WebSocket TTS):
- **Female**: `anushka`, `manisha`, `vidya`, `arya`
- **Male**: `abhilash`, `hitesh`, `karun`

Edit `src/config.py` → `LANGUAGE_CONFIGS` to change voices per language.

---

## Configuration

### Environment Variables (API keys + infrastructure)

| Variable | Description | Default |
|----------|-------------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio account SID | — |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | — |
| `TWILIO_FROM_NUMBER` | Twilio caller number (E.164) | — |
| `SARVAM_API_KEY` | Sarvam AI key (STT + TTS) | — |
| `LLM_PROVIDER` | `groq` or `sarvam` | `groq` |
| `LLM_MODEL` | Main LLM for conversation | `llama-3.3-70b-versatile` |
| `LLM_FAST_MODEL` | Fast LLM for greetings (low TTFT) | `llama-3.1-8b-instant` |
| `GROQ_API_KEY` | Groq API key | — |
| `REDIS_URL` | Redis / Memurai URL for greeting cache | `redis://localhost:6379/0` |
| `SERVER_URL` | Public URL for Twilio webhooks | — |

### Runtime Settings (stored in SQLite, edited from `/settings` UI)

| Setting | Description | Default |
|---------|-------------|---------|
| `company_name` | Your business name (injected into prompts) | `Your Company` |
| `agent_name` | The voice agent's persona name | `Subscription Specialist` |
| `default_language` | Fallback call language | `hi-IN` |
| `days_before_renewal` | Start reminding this many days before renewal | `30` |
| `days_between_calls` | Minimum gap between calls to same subscriber | `7` |

---

## Project Structure

```
├── app.py                              # FastAPI app, startup, routers, WebSocket
├── pyproject.toml                      # Python deps (uv)
├── .env.example                        # Env var template
├── data.db                             # SQLite (auto-created)
├── src/
│   ├── config.py                       # CallConfig, LANGUAGE_CONFIGS
│   ├── automation.py                   # SubscriptionReminderAutomation orchestrator
│   ├── utils.py                        # Helpers
│   ├── models/subscriber.py            # Subscriber + SubscriptionStatus
│   ├── data/
│   │   ├── base.py                     # SubscriptionLoaderBase (abstract)
│   │   └── sqlite_loader.py            # SQLite implementation
│   ├── database/
│   │   ├── models.py                   # SQLAlchemy models: SubscriberRecord, CallRecord, Settings
│   │   └── engine.py                   # Async engine + init_db
│   ├── cache/
│   │   └── greeting_cache.py           # Redis/Memurai greeting audio cache
│   ├── api/
│   │   ├── subscribers.py              # CRUD router
│   │   ├── calls.py                    # Call history router
│   │   ├── settings.py                 # Settings router (invalidates cache on update)
│   │   └── dashboard.py                # Stats router
│   ├── providers/
│   │   ├── base_agent.py               # BaseVoiceAgent (abstract)
│   │   └── twilio_sarvam/
│   │       ├── agent.py                # TwilioSarvamAgent orchestrator
│   │       ├── twilio_handler.py       # Twilio protocol
│   │       ├── stt_client.py           # Sarvam STT (WAV-wrapped, buffered)
│   │       ├── tts_client.py           # Sarvam TTS (mulaw 8kHz, inactivity timeout)
│   │       ├── llm_client.py           # Groq LLM (HTTP/2, fast-model fallback)
│   │       └── audio_utils.py          # mulaw ↔ PCM
│   ├── conversation/
│   │   ├── manager.py                  # State + phrase-boundary streaming
│   │   └── prompts.py                  # Short-reply system prompt + analysis prompt
│   └── tools/
│       └── call_analysis.py            # Post-call classification
├── frontend/
│   ├── package.json
│   └── src/
│       ├── app/                        # Next.js pages (/, /subscribers, /calls, /settings)
│       ├── components/
│       │   ├── layout/sidebar.tsx      # Left nav
│       │   ├── dashboard/call-now-card.tsx
│       │   └── ui/                     # shadcn components
│       ├── hooks/use-call-subscriber.ts
│       └── lib/
│           ├── api.ts                  # Fetch wrappers
│           └── types.ts                # TS interfaces
```

---

## Customization

### Swap the data source

`SQLiteLoader` is the default; replace with any implementation of `SubscriptionLoaderBase`:

```python
from src.data.base import SubscriptionLoaderBase

class PostgresLoader(SubscriptionLoaderBase):
    def fetch_subscriptions(self, subscription_ids=None, status=None, due_within_days=None):
        ...
    def update_subscription(self, subscription_id, updates):
        ...
```

Wire it in `app.py`:
```python
loader = PostgresLoader(connection_string=...)
_automation = SubscriptionReminderAutomation(config=config, data_source=loader)
```

### Tweak the prompts

`src/conversation/prompts.py` holds two prompts:
- **System prompt** — instructions for the live voice agent (tone, response length, flow)
- **Call analysis prompt** — post-call classifier

Current default enforces "ONE short sentence per reply. 15 WORDS MAXIMUM" for phone-call-appropriate brevity.

### Switch voice

Edit `src/config.py` → `LANGUAGE_CONFIGS[lang]["tts_voice"]`. The greeting cache key includes the voice, so switching regenerates automatically on the next call.

### Switch LLM provider

Set `LLM_PROVIDER=sarvam` and use `sarvam-m` model, or point `LLM_MODEL` at any OpenAI-compatible endpoint.

---

## Implementation Notes

### Sarvam protocol quirks we hit & fixed

- **TTS WebSocket** expects `output_audio_codec` / `speech_sample_rate` (not `encoding` / `sample_rate`)
- **TTS** returns audio as **base64 inside JSON `{type:"audio",...}`** messages, not raw binary frames
- **TTS WebSocket** uses `bulbul:v2` regardless of `model` field; only 7 voices available
- **STT** requires `audio/wav` encoding — raw PCM is rejected (we wrap each chunk in a minimal 44-byte WAV header)
- **STT** returns transcripts nested: `event["data"]["transcript"]` under `type: "data"`
- **STT** doesn't support `ping` keepalives — the continuous Twilio audio keeps the socket alive

### Why buffer STT to 200ms chunks?

Twilio sends 20ms audio frames (160 bytes mulaw each). Sending each as a WAV message results in 50 tiny WAV files/sec — mostly header, too short for reliable recognition. We buffer to ~200ms (3200 bytes PCM @ 8kHz) before flushing.

### Why is TTS `synthesize()` timeout 400ms?

Sarvam TTS doesn't reliably send its documented `event_type: "final"` completion event. We use a **400ms inactivity** timeout — Sarvam streams audio in bursts, so a 400ms gap means the burst is done. This keeps sentence-to-sentence transitions tight.

---