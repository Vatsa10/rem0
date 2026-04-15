import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response

from src.config import CallConfig
from src.automation import SubscriptionReminderAutomation

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Subscription Reminder Voice Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global automation instance (initialized on first use)
_automation: SubscriptionReminderAutomation = None


def get_automation() -> SubscriptionReminderAutomation:
    """Get or create the automation singleton."""
    global _automation
    if _automation is None:
        config = CallConfig.from_env()
        # No data source by default — users plug in their own implementation.
        # Pass a SubscriptionLoaderBase implementation here when ready.
        _automation = SubscriptionReminderAutomation(config=config, data_source=None)
    return _automation


@app.get("/")
async def root():
    return RedirectResponse("/docs")


@app.post("/calls/initiate")
async def initiate_calls(payload: dict):
    """
    Start calling subscribers.

    Payload options:
    - {"subscribers": [{"id": "1", "name": "...", "phone": "+91...", ...}]}
      Direct subscriber list (when no data source configured).
    - {"subscription_ids": ["1", "2"]}
      Load from data source by IDs.
    - {"due_within_days": 30}
      Load from data source by renewal proximity.
    """
    automation = get_automation()

    subscribers_data = payload.get("subscribers", [])
    subscription_ids = payload.get("subscription_ids")
    due_within_days = payload.get("due_within_days")

    if subscribers_data:
        from src.models.subscriber import Subscriber
        subscribers = [Subscriber(**s) for s in subscribers_data]
    elif subscription_ids or due_within_days:
        if not automation.data_source:
            raise HTTPException(
                status_code=400,
                detail="No data source configured. Pass subscribers directly.",
            )
        subscribers = automation.load_subscribers(
            subscription_ids=subscription_ids,
            due_within_days=due_within_days,
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide 'subscribers', 'subscription_ids', or 'due_within_days'.",
        )

    if not subscribers:
        return {"message": "No subscribers found.", "results": []}

    results = []
    for subscriber in subscribers:
        try:
            result = await automation.initiate_call(subscriber)
            results.append({
                "subscriber_id": subscriber.id,
                "name": subscriber.name,
                "phone": subscriber.phone,
                **result,
            })
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Failed to call {subscriber.name}: {e}")
            results.append({
                "subscriber_id": subscriber.id,
                "name": subscriber.name,
                "error": str(e),
            })

    return {
        "message": f"Initiated calls for {len(results)} subscribers.",
        "results": results,
    }


@app.post("/twiml/{call_id}")
async def get_twiml(call_id: str):
    """Return TwiML that connects the call to our WebSocket media stream."""
    automation = get_automation()
    twiml = automation.get_twiml(call_id)
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream/{call_id}")
async def media_stream(websocket: WebSocket, call_id: str):
    """
    Unified bidirectional WebSocket for a Twilio media stream.

    Carries: audio in (mulaw), audio out (mulaw), VAD events,
    barge-in signals, and call metadata — all on one connection.
    """
    automation = get_automation()
    await automation.agent.handle_media_stream(websocket, call_id=call_id)

    # Post-call: analyze transcript and update data source
    try:
        updates = await automation.evaluate_call_and_update(call_id)
        if updates:
            logger.info(f"Post-call analysis complete: call_id={call_id}")
    except Exception as e:
        logger.error(f"Post-call analysis failed: {e}")


@app.get("/subscriptions/due")
async def get_subscriptions_due(days: int = 30):
    """Preview subscriptions due for reminder (without calling)."""
    automation = get_automation()
    if not automation.data_source:
        raise HTTPException(
            status_code=400, detail="No data source configured."
        )
    subscribers = automation.load_subscribers(due_within_days=days)
    return {
        "count": len(subscribers),
        "subscriptions": [s.model_dump() for s in subscribers],
    }


@app.get("/calls/{call_id}/status")
async def get_call_status(call_id: str):
    """Check status of an active or completed call."""
    automation = get_automation()
    session = automation.agent.get_session(call_id)
    if not session:
        raise HTTPException(status_code=404, detail="Call not found.")
    return {
        "call_id": call_id,
        "call_sid": session.call_sid,
        "status": session.status,
        "subscriber": session.subscriber.name,
        "has_transcript": bool(session.transcript),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
