from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.database import get_db, SubscriberRecord, CallRecord

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    # Total subscribers
    total = (
        await db.execute(select(func.count(SubscriberRecord.id)))
    ).scalar() or 0

    # Active subscribers (not expired/do-not-call/invalid)
    inactive_statuses = ["EXPIRED", "DO_NOT_CALL", "INVALID_CONTACT"]
    active = (
        await db.execute(
            select(func.count(SubscriberRecord.id)).where(
                SubscriberRecord.status.notin_(inactive_statuses)
            )
        )
    ).scalar() or 0

    # Calls today
    calls_today = (
        await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.created_at >= today_start
            )
        )
    ).scalar() or 0

    # Calls this week
    calls_week = (
        await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.created_at >= week_start
            )
        )
    ).scalar() or 0

    # Renewal rate
    total_contacted = (
        await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.status == "completed"
            )
        )
    ).scalar() or 0
    total_renewed = (
        await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.response == "Confirmed Renewal"
            )
        )
    ).scalar() or 0
    renewal_rate = (total_renewed / total_contacted * 100) if total_contacted > 0 else 0

    # Status breakdown
    status_rows = (
        await db.execute(
            select(
                SubscriberRecord.status, func.count(SubscriberRecord.id)
            ).group_by(SubscriberRecord.status)
        )
    ).all()
    status_breakdown = {row[0]: row[1] for row in status_rows}

    # Recent calls (top 5)
    recent_q = (
        select(CallRecord)
        .options(joinedload(CallRecord.subscriber))
        .order_by(CallRecord.created_at.desc())
        .limit(5)
    )
    recent_result = await db.execute(recent_q)
    recent_calls = [r.to_dict() for r in recent_result.scalars().unique().all()]

    # Upcoming renewals (next 5 by date)
    today_str = now.strftime("%Y-%m-%d")
    upcoming_q = (
        select(SubscriberRecord)
        .where(
            SubscriberRecord.renewal_date >= today_str,
            SubscriberRecord.status.notin_(inactive_statuses),
        )
        .order_by(SubscriberRecord.renewal_date.asc())
        .limit(5)
    )
    upcoming_result = await db.execute(upcoming_q)
    upcoming_renewals = [r.to_dict() for r in upcoming_result.scalars().all()]

    return {
        "total_subscribers": total,
        "active_subscribers": active,
        "calls_today": calls_today,
        "calls_this_week": calls_week,
        "renewal_rate": round(renewal_rate, 1),
        "status_breakdown": status_breakdown,
        "recent_calls": recent_calls,
        "upcoming_renewals": upcoming_renewals,
    }
