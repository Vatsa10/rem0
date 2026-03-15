# Rem0 - Insurance Policy Renewal Reminder AI Voice Agent

An AI-powered voice agent system that automatically calls policyholders to remind them about upcoming insurance policy renewals. Built with Vapi/Retell AI for voice interactions and Google Sheets as the data source.

## Features

- **Automatic Policy Filtering** - Smart scheduling identifies policies due for reminder (renewals within 30 days)
- **Multi-Provider Support** - Works with Vapi or Retell AI for voice calls
- **Google Sheets Integration** - Reads policy data and updates with call results
- **Post-Call Analysis** - AI analyzes conversations to determine customer response
- **Automated CRM Updates** - Call results written back to Google Sheets

## Prerequisites

- Python 3.9+
- Vapi or Retell AI account
- Google Sheets with policy data
- OpenAI API key (for call analysis)

## Quick Setup

1. **Clone and install**
   ```bash
   git clone https://github.com/Vatsa10/rem0
   cd rem0
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Fill in your keys (see `.env.example` for options)

3. **Set up Google Sheets**

   Option A - Auto-create with script (recommended):
   ```bash
   python scripts/create_google_sheet.py
   ```

   Option B - Use your existing sheet with these columns:
   | Column | Description |
   |--------|-------------|
   | Name | Policyholder name |
   | Phone | Contact number |
   | Policy Number | Insurance policy # |
   | Policy Type | Auto, Home, Life, etc. |
   | Renewal Date | YYYY-MM-DD |
   | Premium | Monthly premium (optional) |
   | Coverage Amount | Coverage details (optional) |
   | Status | Reminder status |

4. **Get Google credentials**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a project and enable Google Sheets API
   - Create OAuth credentials and save as `credentials.json`
   - First run will generate `token.json`

5. **Set up Vapi**

   **Option A - Use Vapi's free US numbers (easy):**
   - Go to Vapi Dashboard → Phone Numbers → Create
   - Get a free US number

   **Option B - Import Twilio number (for international):**
   - Buy a number in Twilio console
   - Run: `python scripts/import_twilio_number.py +1234567890`

   Get your `ASSISTANT_ID` and `PHONE_ID` and add to `.env`

6. **Run the server**
   ```bash
   python app.py
   ```

   For webhook support, expose publicly with ngrok:
   ```bash
   ngrok http 8000
   ```

## Usage

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/execute` | POST | Start calling policies |
| `/webhook` | POST | Receive Vapi callbacks |
| `/policies/due` | GET | Preview policies to call |

### Example Requests

**Smart filter - auto get policies due for reminder:**
```json
{
  "use_smart_filter": true,
  "days_before_renewal": 30
}
```

**Call specific policies:**
```json
{
  "policy_ids": ["2", "3", "5"]
}
```

### Call Results

After each call, the system updates Google Sheets with:
- `Status` → RENEWED / FOLLOW-UP NEEDED / NOT INTERESTED / etc.
- `Last Reminder Date`
- `Response` → Confirmed Renewal / Interested / Reschedule / Not Interested
- `Call Summary` → AI-generated summary
- `Notes` → Response justification

## Configuration

Key environment variables:
- `SHEET_ID` - Your Google Sheets ID (from URL)
- `SHEET_NAME` - Sheet tab name (default: "Policies")
- `COMPANY_NAME` - Your insurance company name
- `VAPI_API_KEY`, `VAPI_PHONE_ID`, `VAPI_ASSISTANT_ID` - Vapi credentials
- `DAYS_BEFORE_RENEWAL` - How far ahead to find policies (default: 30)
- `DAYS_BETWEEN_CALLS` - Avoid calling same person within X days (default: 7)

## Customization

Edit these files to customize:
- `src/prompts.py` - Voice agent script and analysis prompt
- `src/vapi_automation.py` - Call flow and CRM updates
- `src/tools/call_analysis.py` - Response classification