from typing import Optional
from pydantic import BaseModel, Field
from src.utils import invoke_llm
from src.conversation.prompts import CALL_ANALYSIS_PROMPT


class CallAnalysisOutput(BaseModel):
    summary: str = Field(
        ...,
        description="A concise summary of the key points from the call.",
    )
    response: str = Field(
        ...,
        description="Subscriber response: 'Confirmed Renewal', 'Interested', 'Reschedule', 'Not Interested', 'No Decision', or 'Invalid Contact'.",
    )
    justification: Optional[str] = Field(
        default=None,
        description="Reasoning based on the call transcript.",
    )
    next_steps: Optional[str] = Field(
        default=None,
        description="Agreed follow-up actions or callback details.",
    )


async def analyze_call_transcript(subscriber_name: str, transcript: str) -> dict:
    """Analyze a call transcript and return structured analysis."""
    user_message = (
        f"Subscriber Name: {subscriber_name}\n\n"
        f"Call Transcript:\n{transcript}"
    )
    result = invoke_llm(
        system_prompt=CALL_ANALYSIS_PROMPT,
        user_message=user_message,
        model="groq/llama-3.3-70b-versatile",
        response_format=CallAnalysisOutput,
        json_output=True,
    )

    if isinstance(result, dict):
        return CallAnalysisOutput(**result)
    return result
