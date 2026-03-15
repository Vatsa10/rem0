import os
from src.base.voice_agent_providers.vapi.vapi_ai import VapiAI
from dotenv import load_dotenv

load_dotenv()


def import_twilio_number(phone_number, twilio_account_sid=None, twilio_auth_token=None):
    """
    Import a Twilio phone number into Vapi.

    Args:
        phone_number: The phone number to import (e.g., "+1234567890")
        twilio_account_sid: Twilio Account SID (or use env vars)
        twilio_auth_token: Twilio Auth Token (or use env vars)
    """
    vapi_client = VapiAI()

    twilio_account_sid = twilio_account_sid or os.getenv("TWILIO_ACCOUNT_SID")
    twilio_auth_token = twilio_auth_token or os.getenv("TWILIO_AUTH_TOKEN")

    if not twilio_account_sid or not twilio_auth_token:
        print("Error: Twilio credentials not provided")
        print("Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in .env")
        return None

    request = {
        "provider": "twilio",
        "number": phone_number,
        "twilioAccountSid": twilio_account_sid,
        "twilioAuthToken": twilio_auth_token,
    }

    result = vapi_client.add_phone_number(request)
    print(f"Imported phone number: {result}")
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python import_twilio_number.py +1234567890")
        print("Make sure TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are set in .env")
        sys.exit(1)

    phone_number = sys.argv[1]
    import_twilio_number(phone_number)
