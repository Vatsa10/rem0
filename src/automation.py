import logging
from typing import List, Optional

from src.config import CallConfig
from src.models.subscriber import Subscriber, SubscriptionStatus
from src.data.base import SubscriptionLoaderBase
from src.providers.twilio_sarvam.agent import TwilioSarvamAgent
from src.tools.call_analysis import analyze_call_transcript
from src.utils import get_current_date_time

logger = logging.getLogger(__name__)

RESPONSE_TO_STATUS = {
    "Confirmed Renewal": SubscriptionStatus.RENEWED,
    "Interested": SubscriptionStatus.FOLLOW_UP_NEEDED,
    "Reschedule": SubscriptionStatus.CALLBACK_SCHEDULED,
    "Not Interested": SubscriptionStatus.NOT_INTERESTED,
    "No Decision": SubscriptionStatus.NO_DECISION,
    "Invalid Contact": SubscriptionStatus.INVALID_CONTACT,
}


class SubscriptionReminderAutomation:
    """
    Orchestrates the subscription reminder workflow:
    load subscribers → initiate calls → analyze results → update data source.
    """

    def __init__(
        self,
        config: CallConfig,
        data_source: Optional[SubscriptionLoaderBase] = None,
    ):
        self.config = config
        self.data_source = data_source
        self.agent = TwilioSarvamAgent(config)

    def load_subscribers(
        self,
        subscription_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        due_within_days: Optional[int] = None,
    ) -> List[Subscriber]:
        """Load subscribers from the data source."""
        if not self.data_source:
            raise ValueError("No data source configured")

        records = self.data_source.fetch_subscriptions(
            subscription_ids=subscription_ids,
            status=status,
            due_within_days=due_within_days,
        )

        subscribers = []
        for record in records:
            try:
                subscriber = Subscriber(**record)
                subscribers.append(subscriber)
            except Exception as e:
                logger.warning(f"Skipping invalid record: {e}")

        return subscribers

    async def initiate_call(self, subscriber: Subscriber) -> dict:
        """Initiate a call to a single subscriber."""
        return await self.agent.initiate_call(
            phone_number=subscriber.phone,
            subscriber_data=subscriber.model_dump(),
        )

    async def evaluate_call_and_update(self, call_id: str) -> Optional[dict]:
        """Analyze a completed call and update the data source."""
        session = self.agent.get_session(call_id)
        if not session or session.status != "completed":
            return None

        transcript = session.transcript
        if not transcript:
            logger.warning(f"No transcript for call_id={call_id}")
            return None

        analysis = await analyze_call_transcript(
            subscriber_name=session.subscriber.name,
            transcript=transcript,
        )

        status = RESPONSE_TO_STATUS.get(
            analysis.response, SubscriptionStatus.CONTACTED
        )

        updates = {
            "Status": status.value,
            "Last Reminder Date": get_current_date_time(),
            "Response": analysis.response,
            "Call Summary": analysis.summary,
            "Notes": analysis.justification,
            "Next Steps": analysis.next_steps,
            "Transcript": transcript,
            "Call ID": call_id,
        }

        if self.data_source:
            self.data_source.update_subscription(
                subscription_id=session.subscriber.subscription_id,
                updates=updates,
            )
            logger.info(
                f"Updated subscription {session.subscriber.subscription_id}: "
                f"status={status.value}, response={analysis.response}"
            )

        return updates

    def get_twiml(self, call_id: str) -> str:
        """Get TwiML for a call."""
        return self.agent.get_twiml(call_id)
