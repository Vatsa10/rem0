from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db, Settings

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    company_name: Optional[str] = None
    agent_name: Optional[str] = None
    default_language: Optional[str] = None
    days_before_renewal: Optional[int] = None
    days_between_calls: Optional[int] = None


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)):
    settings = await db.get(Settings, 1)
    return settings.to_dict() if settings else {}


@router.put("")
async def update_settings(data: SettingsUpdate, db: AsyncSession = Depends(get_db)):
    settings = await db.get(Settings, 1)
    if not settings:
        settings = Settings(id=1)
        db.add(settings)

    update_data = data.model_dump(exclude_none=True)
    for key, value in update_data.items():
        setattr(settings, key, value)

    await db.commit()
    await db.refresh(settings)

    # Reset automation singleton so it picks up new settings
    import app as app_module
    app_module._automation = None

    return settings.to_dict()
