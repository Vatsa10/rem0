import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds


def create_policy_sheet():
    """
    Creates a Google Sheet with the required policy columns.
    """
    try:
        creds = get_credentials()
        service = build("sheets", "v4", credentials=creds)

        spreadsheet = {
            "properties": {"title": "Insurance Policy Reminders"},
            "sheets": [
                {
                    "properties": {"title": "Policies"},
                    "data": [
                        {
                            "startRow": 0,
                            "startColumn": 0,
                            "gridProperties": {"rowCount": 1000, "columnCount": 12},
                        }
                    ],
                }
            ],
        }

        spreadsheet = service.spreadsheets().create(body=spreadsheet).execute()
        spreadsheet_id = spreadsheet.get("spreadsheetId")

        headers = [
            "Name",
            "Phone",
            "Email",
            "Policy Number",
            "Policy Type",
            "Renewal Date",
            "Premium",
            "Coverage Amount",
            "Agent Name",
            "Agent Phone",
            "Status",
            "Last Reminder Date",
            "Response",
            "Call Summary",
            "Notes",
            "Next Steps",
        ]

        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="Policies!1:1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()

        print(f"\n✅ Created successfully!")
        print(
            f"📊 Spreadsheet URL: https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        )
        print(f"\nAdd this to your .env file:")
        print(f"SHEET_ID={spreadsheet_id}")
        print(f"SHEET_NAME=Policies")

        return spreadsheet_id

    except HttpError as e:
        print(f"Error: {e}")
        return None


if __name__ == "__main__":
    print("Creating Google Sheet for Insurance Policy Reminders...\n")
    create_policy_sheet()
