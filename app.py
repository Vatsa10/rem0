import asyncio
import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, Response

from src.cache import get_greeting_cache
from src.config import CallConfig
from src.automation import SubscriptionReminderAutomation
from src.database import init_db, async_session, CallRecord
from src.data.sqlite_loader import SQLiteLoader
from src.api.subscribers import router as subscribers_router
from src.api.calls import router as calls_router
from src.api.settings import router as settings_router
from src.api.dashboard import router as dashboard_router

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

# API routers
app.include_router(subscribers_router, prefix="/api")
app.include_router(calls_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")

# Global automation instance
_automation: SubscriptionReminderAutomation = None


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("Database initialized")

    # Pre-connect greeting cache (Memurai/Redis on localhost:6379 by default).
    cache = get_greeting_cache()
    await cache.connect()

    # Pre-warm the automation + LLM HTTP connection pool.
    automation = await get_automation()
    try:
        await automation.agent._get_shared_llm().warmup()
        logger.info("LLM client pre-warmed")
    except Exception as e:
        logger.warning(f"LLM warmup skipped: {e}")


@app.on_event("shutdown")
async def shutdown():
    # Delegate full agent teardown (active sessions + shared LLM client)
    # to the agent's own shutdown() — see TwilioSarvamAgent.shutdown().
    if _automation is not None:
        try:
            await _automation.agent.shutdown()
        except Exception as e:
            logger.warning(f"Agent shutdown failed: {e}")
    try:
        await get_greeting_cache().close()
    except Exception:
        pass


async def get_automation() -> SubscriptionReminderAutomation:
    global _automation
    if _automation is None:
        async with async_session() as db:
            config = await CallConfig.from_db(db)
        loader = SQLiteLoader(async_session)
        _automation = SubscriptionReminderAutomation(config=config, data_source=loader)
    return _automation


@app.get("/")
async def root():
    return RedirectResponse("/docs")


@app.post("/calls/initiate")
async def initiate_calls(payload: dict):
    """
    Start calling subscribers.

    Payload options:
    - {"subscribers": [...]}           — direct subscriber list
    - {"subscription_ids": ["1","2"]}  — load from DB by IDs
    - {"due_within_days": 30}          — load from DB by renewal proximity
    """
    automation = await get_automation()

    subscribers_data = payload.get("subscribers", [])
    subscription_ids = payload.get("subscription_ids")
    due_within_days = payload.get("due_within_days")

    if subscribers_data:
        from src.models.subscriber import Subscriber
        subscribers = [Subscriber(**s) for s in subscribers_data]
    elif subscription_ids or due_within_days:
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

            # Save call record to DB
            async with async_session() as db:
                call_record = CallRecord(
                    call_id=result["call_id"],
                    call_sid=result.get("call_sid", ""),
                    subscriber_id=subscriber.id,
                    status="initiated",
                )
                db.add(call_record)
                await db.commit()

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
    automation = await get_automation()
    twiml = automation.get_twiml(call_id)
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream/{call_id}")
async def media_stream(websocket: WebSocket, call_id: str):
    """Unified bidirectional WebSocket for Twilio media stream."""
    automation = await get_automation()
    await automation.agent.handle_media_stream(websocket, call_id=call_id)

    # Post-call: analyze transcript and update data source + DB
    try:
        updates = await automation.evaluate_call_and_update(call_id)
        if updates:
            async with async_session() as db:
                from sqlalchemy import select
                query = select(CallRecord).where(CallRecord.call_id == call_id)
                result = await db.execute(query)
                record = result.scalar_one_or_none()
                if record:
                    record.status = "completed"
                    record.transcript = updates.get("Transcript", "")
                    record.summary = updates.get("Call Summary", "")
                    record.response = updates.get("Response", "")
                    record.justification = updates.get("Notes", "")
                    record.next_steps = updates.get("Next Steps", "")
                    await db.commit()
            logger.info(f"Post-call analysis saved: call_id={call_id}")
    except Exception as e:
        logger.error(f"Post-call analysis failed: {e}")
    finally:
        # Free the in-memory session whether analysis succeeded or not;
        # without this, active_calls grows unbounded.
        automation.agent.release_session(call_id)


@app.get("/subscriptions/due")
async def get_subscriptions_due(days: int = 30):
    automation = await get_automation()
    subscribers = automation.load_subscribers(due_within_days=days)
    return {
        "count": len(subscribers),
        "subscriptions": [s.model_dump() for s in subscribers],
    }


@app.get("/calls/{call_id}/status")
async def get_call_status(call_id: str):
    automation = await get_automation()
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
