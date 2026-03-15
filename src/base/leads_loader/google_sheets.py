import os
from datetime import datetime, timedelta
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from .lead_loader_base import LeadLoaderBase

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_google_credentials():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return creds


class GoogleSheetLeadLoader(LeadLoaderBase):
    def __init__(self, spreadsheet_id, sheet_name=None):
        self.sheet_service = build("sheets", "v4", credentials=get_google_credentials())
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name or self._get_sheet_name_from_id()

    def fetch_records(self, lead_ids=None, status="NEW"):
        """
        Fetches leads from Google Sheets. If lead IDs are provided, fetch those specific records.
        Otherwise, fetch leads matching the given status.
        """
        try:
            result = (
                self.sheet_service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=self.sheet_name)
                .execute()
            )
            rows = result.get("values", [])
            headers = rows[0]
            records = []

            for i, row in enumerate(rows[1:], start=2):  # Start from row 2 for data
                record = dict(zip(headers, row))
                record["id"] = f"{i}"  # Add row number as an ID

                if lead_ids:
                    if record["id"] in lead_ids:
                        records.append(record)
                elif record.get("Status") == status:
                    records.append(record)

            return records
        except HttpError as e:
            print(f"Error fetching records from Google Sheets: {e}")
            return []

    def update_record(self, lead_id, updates: dict):
        """
        Updates a record in Google Sheets, adding or modifying specified fields.

        Args:
            lead_id (int): The row number of the record to update.
            updates (dict): A dictionary of fields to update or add.

        Returns:
            dict: The updated record ID and fields if successful, None otherwise.
        """
        try:
            # Fetch the header row to identify column indices
            result = (
                self.sheet_service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=self.sheet_name)
                .execute()
            )
            rows = result.get("values", [])
            headers = rows[0]

            # Prepare the update body for all specified fields
            updates_batch = []
            for field, value in updates.items():
                if field in headers:
                    col_index = headers.index(field)
                    col_letter = chr(65 + col_index)  # Convert index to column letter
                    range_ = f"{self.sheet_name}!{col_letter}{lead_id}"
                    updates_batch.append(
                        {
                            "range": range_,
                            "values": [[value]],
                        }
                    )

            # Execute batch update for efficiency
            if updates_batch:
                body = {"valueInputOption": "RAW", "data": updates_batch}
                self.sheet_service.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body=body
                ).execute()
            return {"id": lead_id, "updated_fields": updates}
        except HttpError as e:
            print(f"Error updating Google Sheets record: {e}")
            return None

    def update_records_batch(self, leads):
        """
        Updates multiple records in Google Sheets based on a list of Lead objects.
        """
        updated_records = []
        for lead in leads:
            try:
                updated_record = self.update_record(lead["id"], lead["updates"])
                if updated_record:
                    updated_records.append(updated_record)
            except Exception as e:
                print(f"Skipping lead ID {lead['id']}: {e}")
        return updated_records

    def _get_sheet_name_from_id(self):
        """
        Retrieves the default sheet name if not explicitly provided.
        """
        try:
            result = (
                self.sheet_service.spreadsheets()
                .get(spreadsheetId=self.spreadsheet_id)
                .execute()
            )
            sheets = result.get("sheets", [])
            if not sheets:
                raise ValueError("No sheets found in the spreadsheet.")
            return sheets[0]["properties"]["title"]  # Default to the first sheet
        except HttpError as e:
            print(f"Error fetching sheet name: {e}")
            raise

    def fetch_policies_due_for_reminder(
        self, days_before_renewal=30, days_after_last_call=7
    ):
        """
        Fetch policies that need renewal reminders.

        Args:
            days_before_renewal: Include policies renewing within this many days
            days_after_last_call: Skip policies called within this many days

        Returns:
            List of policy records due for reminder
        """
        try:
            result = (
                self.sheet_service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=self.sheet_name)
                .execute()
            )
            rows = result.get("values", [])
            if not rows or len(rows) < 2:
                return []

            headers = rows[0]
            today = datetime.now().date()
            policies = []

            renewal_date_col = None
            last_reminder_col = None
            status_col = None

            for idx, header in enumerate(headers):
                if header.lower() in [
                    "renewal date",
                    "renewal_date",
                    "policy renewal date",
                ]:
                    renewal_date_col = idx
                elif header.lower() in [
                    "last reminder date",
                    "last_reminder_date",
                    "last called",
                ]:
                    last_reminder_col = idx
                elif header.lower() in ["status", "reminder status"]:
                    status_col = idx

            if renewal_date_col is None:
                print("Warning: No 'Renewal Date' column found in sheet")
                return []

            for i, row in enumerate(rows[1:], start=2):
                if len(row) <= max(
                    renewal_date_col or 0, last_reminder_col or 0, status_col or 0
                ):
                    continue

                record = dict(zip(headers, row))
                record["id"] = f"{i}"

                renewal_date_str = (
                    row[renewal_date_col] if renewal_date_col is not None else ""
                )
                if not renewal_date_str:
                    continue

                try:
                    renewal_date = datetime.strptime(
                        renewal_date_str, "%Y-%m-%d"
                    ).date()
                except:
                    try:
                        renewal_date = datetime.strptime(
                            renewal_date_str, "%m/%d/%Y"
                        ).date()
                    except:
                        continue

                days_until_renewal = (renewal_date - today).days

                if days_until_renewal < -30:
                    continue

                if days_until_renewal > days_before_renewal:
                    continue

                if last_reminder_col is not None and last_reminder_col < len(row):
                    last_call_date_str = row[last_reminder_col]
                    if last_call_date_str:
                        try:
                            last_call_date = datetime.strptime(
                                last_call_date_str, "%Y-%m-%d"
                            ).date()
                            days_since_call = (today - last_call_date).days
                            if days_since_call < days_after_last_call:
                                continue
                        except:
                            pass

                if status_col is not None and status_col < len(row):
                    status = row[status_col].upper()
                    if status in ["COMPLETED", "NOT INTERESTED", "DO NOT CALL"]:
                        continue

                policies.append(record)

            policies.sort(
                key=lambda p: (
                    datetime.strptime(
                        p.get("Renewal Date", "2099-12-31"), "%Y-%m-%d"
                    ).date()
                    if p.get("Renewal Date")
                    else datetime(2099, 12, 31).date()
                )
            )

            return policies

        except HttpError as e:
            print(f"Error fetching policies due for reminder: {e}")
            return []
