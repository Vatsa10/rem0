from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.database import get_db, CallRecord

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("")
async def list_calls(
    subscriber_id: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(CallRecord).options(joinedload(CallRecord.subscriber))

    if subscriber_id:
        query = query.where(CallRecord.subscriber_id == subscriber_id)
    if status:
        query = query.where(CallRecord.status == status)

    count_q = select(func.count()).select_from(
        select(CallRecord.id).where(
            *(
                [CallRecord.subscriber_id == subscriber_id] if subscriber_id else []
            ),
            *(
                [CallRecord.status == status] if status else []
            ),
        ).subquery()
    )
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(CallRecord.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)

    result = await db.execute(query)
    items = [r.to_dict() for r in result.scalars().unique().all()]

    return {"items": items, "total": total, "page": page, "limit": limit}


@router.get("/{call_id}")
async def get_call(call_id: str, db: AsyncSession = Depends(get_db)):
    query = (
        select(CallRecord)
        .options(joinedload(CallRecord.subscriber))
        .where(CallRecord.call_id == call_id)
    )
    result = await db.execute(query)
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="Call not found")
    return record.to_dict()
