import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.database.models import SubscriberRecord
from .base import SubscriptionLoaderBase

logger = logging.getLogger(__name__)


class SQLiteLoader(SubscriptionLoaderBase):
    """SubscriptionLoaderBase implementation backed by SQLite."""

    def __init__(self, session_factory: async_sessionmaker):
        self._session_factory = session_factory

    def _run_async(self, coro):
        """Run async code from sync context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        else:
            return asyncio.run(coro)

    def fetch_subscriptions(
        self,
        subscription_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        due_within_days: Optional[int] = None,
    ) -> List[dict]:
        return self._run_async(
            self._fetch_async(subscription_ids, status, due_within_days)
        )

    async def _fetch_async(
        self,
        subscription_ids: Optional[List[str]],
        status: Optional[str],
        due_within_days: Optional[int],
    ) -> List[dict]:
        async with self._session_factory() as session:
            query = select(SubscriberRecord)

            if subscription_ids:
                query = query.where(
                    or_(
                        SubscriberRecord.id.in_(subscription_ids),
                        SubscriberRecord.subscription_id.in_(subscription_ids),
                    )
                )

            if status:
                query = query.where(SubscriberRecord.status == status)

            if due_within_days:
                today = datetime.now().date()
                cutoff = today + timedelta(days=due_within_days)
                query = query.where(
                    SubscriberRecord.renewal_date <= cutoff.isoformat(),
                    SubscriberRecord.renewal_date >= today.isoformat(),
                ).where(
                    SubscriberRecord.status.notin_(
                        ["EXPIRED", "DO_NOT_CALL", "INVALID_CONTACT"]
                    )
                )

            result = await session.execute(query)
            records = result.scalars().all()
            return [r.to_dict() for r in records]

    def update_subscription(
        self, subscription_id: str, updates: dict
    ) -> Optional[dict]:
        return self._run_async(self._update_async(subscription_id, updates))

    async def _update_async(
        self, subscription_id: str, updates: dict
    ) -> Optional[dict]:
        async with self._session_factory() as session:
            query = select(SubscriberRecord).where(
                SubscriberRecord.subscription_id == subscription_id
            )
            result = await session.execute(query)
            record = result.scalar_one_or_none()

            if not record:
                logger.warning(f"Subscriber not found: {subscription_id}")
                return None

            if "Status" in updates:
                record.status = updates["Status"]

            await session.commit()
            await session.refresh(record)
            return record.to_dict()
