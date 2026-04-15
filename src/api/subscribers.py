import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db, SubscriberRecord

router = APIRouter(prefix="/subscribers", tags=["subscribers"])


class SubscriberCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = ""
    subscription_id: str
    subscription_type: str
    renewal_date: str
    amount: Optional[str] = ""
    language: str = "hi-IN"
    metadata: Optional[dict] = {}


class SubscriberUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    subscription_id: Optional[str] = None
    subscription_type: Optional[str] = None
    renewal_date: Optional[str] = None
    amount: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None
    metadata: Optional[dict] = None


@router.get("")
async def list_subscribers(
    search: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(SubscriberRecord)

    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(
                SubscriberRecord.name.ilike(pattern),
                SubscriberRecord.phone.ilike(pattern),
                SubscriberRecord.subscription_id.ilike(pattern),
            )
        )

    if status:
        query = query.where(SubscriberRecord.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(SubscriberRecord.created_at.desc())
    query = query.offset((page - 1) * limit).limit(limit)

    result = await db.execute(query)
    items = [r.to_dict() for r in result.scalars().all()]

    return {"items": items, "total": total, "page": page, "limit": limit}


@router.get("/{subscriber_id}")
async def get_subscriber(subscriber_id: str, db: AsyncSession = Depends(get_db)):
    record = await db.get(SubscriberRecord, subscriber_id)
    if not record:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    return record.to_dict()


@router.post("", status_code=201)
async def create_subscriber(data: SubscriberCreate, db: AsyncSession = Depends(get_db)):
    record = SubscriberRecord(
        id=str(uuid.uuid4()),
        name=data.name,
        phone=data.phone,
        email=data.email,
        subscription_id=data.subscription_id,
        subscription_type=data.subscription_type,
        renewal_date=data.renewal_date,
        amount=data.amount,
        language=data.language,
        metadata_=data.metadata,
        status="NEW",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record.to_dict()


@router.put("/{subscriber_id}")
async def update_subscriber(
    subscriber_id: str, data: SubscriberUpdate, db: AsyncSession = Depends(get_db)
):
    record = await db.get(SubscriberRecord, subscriber_id)
    if not record:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    update_data = data.model_dump(exclude_none=True)
    if "metadata" in update_data:
        update_data["metadata_"] = update_data.pop("metadata")

    for key, value in update_data.items():
        setattr(record, key, value)

    await db.commit()
    await db.refresh(record)
    return record.to_dict()


@router.delete("/{subscriber_id}")
async def delete_subscriber(subscriber_id: str, db: AsyncSession = Depends(get_db)):
    record = await db.get(SubscriberRecord, subscriber_id)
    if not record:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    await db.delete(record)
    await db.commit()
    return {"message": "Subscriber deleted"}
