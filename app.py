import os
import time
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from src.base.leads_loader.google_sheets import GoogleSheetLeadLoader
from src.vapi_automation import InsuranceReminderAutomation
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Insurance Policy Renewal Reminder System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


def get_automation():
    """Initialize the automation with Google Sheets lead loader."""
    lead_loader = GoogleSheetLeadLoader(
        spreadsheet_id=os.getenv("SHEET_ID"),
        sheet_name=os.getenv("SHEET_NAME", "Policies"),
    )
    return InsuranceReminderAutomation(lead_loader)


@app.get("/")
async def redirect_root_to_docs():
    return RedirectResponse("/docs")


@app.post("/execute")
async def execute(payload: dict):
    """
    Trigger the policy reminder workflow.

    Payload options:
    - {"policy_ids": ["1", "2"]} - Call specific policies by row number
    - {"use_smart_filter": true, "days_before_renewal": 30} - Auto-filter policies due for reminder
    """
    try:
        automation = get_automation()

        policy_ids = payload.get("policy_ids")
        use_smart_filter = payload.get("use_smart_filter", False)
        days_before_renewal = payload.get("days_before_renewal", 30)

        if use_smart_filter:
            policies = automation.load_policies(
                use_smart_filter=True, days_before_renewal=days_before_renewal
            )
        elif policy_ids:
            policies = automation.load_policies(policy_ids=policy_ids)
        else:
            return {
                "message": "Please provide policy_ids or set use_smart_filter to true."
            }

        if not policies:
            return {"message": "No policies found to call."}

        print(f"Found {len(policies)} policies to process")

        results = []
        for policy in policies:
            automation.pre_call_processing(policy)
            call_params = automation.get_call_input_params(policy)
            print(
                f"Calling {policy.name} ({policy.phone}) - Policy #{policy.policy_number}"
            )

            output = await automation.make_call(call_params)
            results.append(
                {
                    "policy_id": policy.id,
                    "policy_number": policy.policy_number,
                    "name": policy.name,
                    "call_response": output,
                }
            )

            time.sleep(1)

        return {
            "message": f"Successfully initiated calls for {len(policies)} policies.",
            "results": results,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(
            status_code=500, detail="An error occurred while executing the workflow"
        )


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handle incoming webhook requests from Vapi.
    """
    try:
        automation = get_automation()
        response = await automation.handle_webhook_call(request)
        return response
    except Exception as e:
        print(f"Error processing webhook: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook payload")


@app.get("/policies/due")
async def get_policies_due():
    """
    Get list of policies due for reminder (without calling them).
    """
    try:
        automation = get_automation()
        policies = automation.load_policies(
            use_smart_filter=True, days_before_renewal=30
        )
        return {
            "count": len(policies),
            "policies": [
                {
                    "id": p.id,
                    "name": p.name,
                    "phone": p.phone,
                    "policy_number": p.policy_number,
                    "policy_type": p.policy_type,
                    "renewal_date": p.renewal_date,
                    "premium": p.premium,
                }
                for p in policies
            ],
        }
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
