import os
from src.base.voice_agent_providers.vapi.vapi_ai import VapiAI
from src.tools.calendar_tool import book_appointement
from src.tools.call_analysis import analyze_call_transcript
from src.utils import PolicyHolder, get_current_date_time


TOOLS = {"bookAppointment": book_appointement}


class InsuranceReminderAutomation(VapiAI):
    def __init__(self, lead_loader):
        super().__init__(tools=TOOLS)
        self.lead_loader = lead_loader
        self.company_name = os.getenv("COMPANY_NAME", "Your Insurance Company")
        self.agent_name = os.getenv("AGENT_NAME", "Insurance Specialist")

    def load_policies(
        self, policy_ids=None, use_smart_filter=False, days_before_renewal=30
    ):
        """
        Load policies from the CRM/Google Sheets.

        Args:
            policy_ids: Specific policy IDs to load (optional)
            use_smart_filter: If True, use smart filtering to get policies due for reminder
            days_before_renewal: For smart filter, how many days before renewal to include

        Returns:
            List of PolicyHolder objects
        """
        if use_smart_filter:
            raw_policies = self.lead_loader.fetch_policies_due_for_reminder(
                days_before_renewal=days_before_renewal
            )
        elif policy_ids:
            raw_policies = self.lead_loader.fetch_records(lead_ids=policy_ids)
        else:
            raw_policies = self.lead_loader.fetch_records(status="NEW")

        if not raw_policies:
            return []

        policies = [
            PolicyHolder(
                id=policy["id"],
                name=policy.get("Name", policy.get("Policy Holder", "")),
                phone=policy.get("Phone", policy.get("Mobile", "")),
                email=policy.get("Email", ""),
                policy_number=policy.get("Policy Number", policy.get("Policy #", "")),
                policy_type=policy.get("Policy Type", policy.get("Type", "Insurance")),
                renewal_date=policy.get("Renewal Date", policy.get("renewal_date", "")),
                premium=policy.get("Premium", ""),
                coverage_amount=policy.get(
                    "Coverage Amount", policy.get("Coverage", "")
                ),
                agent_name=policy.get("Agent Name", self.agent_name),
                agent_phone=policy.get("Agent Phone", ""),
            )
            for policy in raw_policies
        ]

        print(f"Loaded {len(policies)} policies")
        return policies

    def load_leads(self, lead_ids):
        return self.load_policies(policy_ids=lead_ids)

    def pre_call_processing(self, payload):
        pass

    def get_call_input_params(self, policy_data: PolicyHolder) -> dict:
        """
        Build the payload required to initiate a call via Vapi.

        Args:
            policy_data: PolicyHolder data containing policy details.

        Returns:
            dict: A formatted payload to pass to the Vapi call API.
        """
        name_parts = policy_data.name.split(" ", 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        return {
            "phone_number_id": os.getenv("VAPI_PHONE_ID"),
            "assistant_id": os.getenv("VAPI_ASSISTANT_ID"),
            "customer": {
                "number": policy_data.phone,
            },
            "assistant_overrides": {
                "variable_values": {
                    "policyID": policy_data.id,
                    "firstName": first_name,
                    "lastName": last_name,
                    "name": policy_data.name,
                    "phone": policy_data.phone,
                    "email": policy_data.email,
                    "policyNumber": policy_data.policy_number,
                    "policyType": policy_data.policy_type,
                    "renewalDate": policy_data.renewal_date,
                    "premium": policy_data.premium,
                    "coverageAmount": policy_data.coverage_amount,
                    "companyName": self.company_name,
                    "agentName": policy_data.agent_name or self.agent_name,
                    "date": get_current_date_time(),
                }
            },
        }

    def process_call_outputs(self, response: dict) -> dict:
        """
        Process the response from a Vapi call and extract relevant details.

        Args:
            response: The raw response from the Vapi API.

        Returns:
            dict: Processed call outputs.
        """
        return {
            "call_id": response["call"]["id"],
            "status": response["call"]["status"],
            "duration": response["durationMinutes"],
            "cost": response["cost"],
            "ended_reason": response["endedReason"],
            "transcript": response["artifact"]["transcript"],
            "policy_info": response["call"]["assistantOverrides"]["variableValues"],
        }

    def evaluate_call_and_update_crm(self, call_outputs: dict) -> dict:
        """
        Perform post-call analysis and update the CRM with the results.

        Args:
            call_outputs: Processed call outputs.

        Returns:
            dict: Updated policy information.
        """
        policy_name = call_outputs["policy_info"].get("name", "Policy Holder")
        output = analyze_call_transcript(policy_name, call_outputs["transcript"])

        response = output.get("response", "No Decision")

        status_map = {
            "Confirmed Renewal": "RENEWED",
            "Interested - Needs Follow-up": "FOLLOW-UP NEEDED",
            "Reschedule": "CALLBACK SCHEDULED",
            "Not Interested": "NOT INTERESTED",
            "No Decision": "CONTACTED - NO DECISION",
            "Invalid Contact": "INVALID CONTACT",
        }

        updates = {
            "Status": status_map.get(response, "CONTACTED"),
            "Last Reminder Date": get_current_date_time(),
            "Response": response,
            "Call ID": call_outputs["call_id"],
            "Call Status": call_outputs["status"],
            "Duration (min)": str(call_outputs["duration"]),
            "Cost": call_outputs["cost"],
            "End Reason": call_outputs["ended_reason"],
            "Transcript": call_outputs["transcript"],
            "Call Summary": output.get("summary"),
            "Notes": output.get("justification"),
            "Next Steps": output.get("next_steps", ""),
        }

        self.lead_loader.update_record(call_outputs["policy_info"]["policyID"], updates)
        return updates

    def post_call_processing(self, call_outputs):
        """
        Post call analysis function invoked by the base VAPIAI class
        upon receiving the end call event for Vapi.

        Args:
            call_outputs: Processed call outputs from Vapi.
        """
        self.evaluate_call_and_update_crm(call_outputs)
