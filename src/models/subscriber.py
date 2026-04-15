from pydantic import BaseModel
from typing import Optional
from enum import Enum


class SubscriptionStatus(str, Enum):
    NEW = "NEW"
    CONTACTED = "CONTACTED"
    RENEWED = "RENEWED"
    FOLLOW_UP_NEEDED = "FOLLOW_UP_NEEDED"
    CALLBACK_SCHEDULED = "CALLBACK_SCHEDULED"
    NOT_INTERESTED = "NOT_INTERESTED"
    NO_DECISION = "NO_DECISION"
    INVALID_CONTACT = "INVALID_CONTACT"
    EXPIRED = "EXPIRED"
    DO_NOT_CALL = "DO_NOT_CALL"


class Subscriber(BaseModel):
    id: str
    name: str
    phone: str
    email: Optional[str] = ""
    subscription_id: str
    subscription_type: str
    renewal_date: str
    amount: Optional[str] = ""
    language: str = "hi-IN"
    metadata: Optional[dict] = {}
