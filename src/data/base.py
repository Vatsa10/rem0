from abc import ABC, abstractmethod
from typing import List, Optional

from src.models.subscriber import SubscriptionStatus


class SubscriptionLoaderBase(ABC):
    available_statuses = [s.value for s in SubscriptionStatus]

    @abstractmethod
    def fetch_subscriptions(
        self,
        subscription_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        due_within_days: Optional[int] = None,
    ) -> List[dict]:
        """
        Fetch subscription records with optional filtering.

        Args:
            subscription_ids: Specific IDs to fetch. If None, fetch all matching.
            status: Filter by subscription status.
            due_within_days: Only return subscriptions renewing within N days.

        Returns:
            List of dicts, each representing a subscription record.
        """
        pass

    @abstractmethod
    def update_subscription(
        self, subscription_id: str, updates: dict
    ) -> Optional[dict]:
        """
        Update a subscription record with call results.

        Args:
            subscription_id: The ID of the subscription to update.
            updates: Dict of field names to new values.

        Returns:
            Updated record dict, or None on failure.
        """
        pass
