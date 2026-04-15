import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Integer, Float, Text, DateTime, JSON, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


def _uuid():
    return str(uuid.uuid4())


class SubscriberRecord(Base):
    __tablename__ = "subscribers"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String, default="")
    subscription_id = Column(String, nullable=False, unique=True)
    subscription_type = Column(String, nullable=False)
    renewal_date = Column(String, nullable=False)
    amount = Column(String, default="")
    language = Column(String, default="hi-IN")
    metadata_ = Column("metadata", JSON, default=dict)
    status = Column(String, default="NEW")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    calls = relationship("CallRecord", back_populates="subscriber")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "phone": self.phone,
            "email": self.email or "",
            "subscription_id": self.subscription_id,
            "subscription_type": self.subscription_type,
            "renewal_date": self.renewal_date,
            "amount": self.amount or "",
            "language": self.language,
            "metadata": self.metadata_ or {},
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CallRecord(Base):
    __tablename__ = "calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    call_id = Column(String, unique=True, nullable=False)
    call_sid = Column(String, default="")
    subscriber_id = Column(String, ForeignKey("subscribers.id"), nullable=False)
    status = Column(String, default="initiated")
    transcript = Column(Text, default="")
    summary = Column(Text, default="")
    response = Column(String, default="")
    justification = Column(Text, default="")
    next_steps = Column(Text, default="")
    duration = Column(Float, default=0.0)
    created_at = Column(DateTime, default=_utcnow)

    subscriber = relationship("SubscriberRecord", back_populates="calls")

    def to_dict(self):
        return {
            "id": self.id,
            "call_id": self.call_id,
            "call_sid": self.call_sid,
            "subscriber_id": self.subscriber_id,
            "subscriber_name": self.subscriber.name if self.subscriber else "",
            "status": self.status,
            "transcript": self.transcript or "",
            "summary": self.summary or "",
            "response": self.response or "",
            "justification": self.justification or "",
            "next_steps": self.next_steps or "",
            "duration": self.duration or 0.0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)
    company_name = Column(String, default="Your Company")
    agent_name = Column(String, default="Subscription Specialist")
    default_language = Column(String, default="hi-IN")
    days_before_renewal = Column(Integer, default=30)
    days_between_calls = Column(Integer, default=7)

    def to_dict(self):
        return {
            "company_name": self.company_name,
            "agent_name": self.agent_name,
            "default_language": self.default_language,
            "days_before_renewal": self.days_before_renewal,
            "days_between_calls": self.days_between_calls,
        }
