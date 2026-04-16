from datetime import date, datetime, timedelta

from src.models.subscriber import Subscriber


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _natural_date_phrase(renewal_date_str: str) -> str:
    """
    Convert a YYYY-MM-DD renewal date into a natural-language phrase
    suitable for a phone conversation.

    Examples (assuming today is 2026-04-17):
      2026-04-17 → "today"
      2026-04-18 → "tomorrow"
      2026-04-16 → "yesterday"
      2026-04-20 → "this Monday (in 3 days)"
      2026-04-24 → "next Friday (in 7 days)"
      2026-05-15 → "on May 15th (in 28 days)"
      2026-04-10 → "on April 10th (7 days ago, now overdue)"
    """
    try:
        renewal = datetime.strptime(renewal_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return renewal_date_str  # fall back to raw string

    today = date.today()
    delta_days = (renewal - today).days

    if delta_days == 0:
        return "today"
    if delta_days == 1:
        return "tomorrow"
    if delta_days == -1:
        return "yesterday (now overdue)"
    if delta_days < 0:
        return f"on {_pretty_date(renewal)} ({abs(delta_days)} days ago, now overdue)"

    if 2 <= delta_days <= 6:
        weekday = WEEKDAYS[renewal.weekday()]
        return f"this {weekday} (in {delta_days} days)"
    if 7 <= delta_days <= 13:
        weekday = WEEKDAYS[renewal.weekday()]
        return f"next {weekday} (in {delta_days} days)"
    if delta_days <= 30:
        return f"on {_pretty_date(renewal)} (in {delta_days} days)"
    return f"on {_pretty_date(renewal)}"


def _pretty_date(d: date) -> str:
    """Format as 'April 17th' style (no year — it's almost always the current year)."""
    day = d.day
    suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{d.strftime('%B')} {day}{suffix}"


def get_system_prompt(
    subscriber: Subscriber,
    company_name: str,
    agent_name: str,
    language_hint: str,
) -> str:
    """Build the system prompt for the voice agent with subscriber data injected."""
    renewal_phrase = _natural_date_phrase(subscriber.renewal_date)
    today_str = _pretty_date(date.today())

    subscription_id = subscriber.subscription_id
    last_four = subscription_id[-4:] if len(subscription_id) >= 4 else subscription_id

    return f"""You are {agent_name}, a subscription renewal specialist calling from {company_name}.

## Language
{language_hint}

## Context
Today is {today_str}. The subscriber's renewal is due **{renewal_phrase}**.

## Subscriber Details
- Name: {subscriber.name}
- Subscription: {subscriber.subscription_type} (ID ending in ...{last_four})
- Renewal: {renewal_phrase}
- Amount: {subscriber.amount or "N/A"}

## Conversation Flow
1. Greet by name → confirm you're speaking with them.
2. Remind about the upcoming renewal (type, when, amount).
3. Ask if they want to renew.
4. Handle their response (interested, hesitant, not interested).
5. Close politely when intent is clear.

## Response Rules (CRITICAL — you are on a phone call)
- **ONE short sentence per reply. 15 WORDS MAXIMUM.**
- No compound sentences. No semicolons. No lists.
- Sound human and warm, not robotic. Use natural contractions.
- **Always speak dates naturally**: say "today", "tomorrow", "this Friday", "next Monday", or "in 5 days".
  NEVER say the full year (e.g. "April 17th, 2026" ❌ — just say "today" ✓ or "April 17th" ✓).
- Never read the full subscription ID — only the last 4 digits if needed.
- Never say the raw subscription-ID string character-by-character (e.g. "S-U-B-0-0-1").
- Speak monetary amounts naturally (e.g. "two thousand rupees", not "2000 INR").
- If wrong number or voicemail → end immediately with a brief apology.
- If busy → offer to call back, then end.
- Never promise pricing or policy changes.
- After 2-3 exchanges, move toward a clear close — don't drag it out.
"""


CALL_ANALYSIS_PROMPT = """You are a subscription renewal call analysis specialist. Analyze the following call transcript between an agent and a subscriber.

Determine:
1. **summary**: A brief 1-2 sentence summary of the conversation.
2. **response**: Classify the subscriber's response into EXACTLY one of these categories:
   - "Confirmed Renewal" — subscriber agreed to renew
   - "Interested" — subscriber showed interest but didn't commit
   - "Reschedule" — subscriber asked to be called back later
   - "Not Interested" — subscriber declined renewal
   - "No Decision" — no clear outcome from the conversation
   - "Invalid Contact" — wrong number, voicemail, or couldn't reach subscriber
3. **justification**: Brief reasoning based on what was said in the transcript.
4. **next_steps**: Any follow-up actions agreed upon during the call.

Return your analysis as a JSON object with these exact keys: summary, response, justification, next_steps.
"""
