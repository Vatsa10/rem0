from typing import Optional
from pydantic import BaseModel, Field
from src.utils import invoke_llm
from src.prompts import CALL_ANALYSIS_PROMPT


class CallAnalysisOutput(BaseModel):
    summary: str = Field(
        ...,
        description="A concise summary highlighting the key talking points and interactions during the call.",
    )
    response: str = Field(
        ...,
        description="Customer response category: 'Confirmed Renewal', 'Interested - Needs Follow-up', 'Reschedule', 'Not Interested', 'No Decision', or 'Invalid Contact'.",
    )
    justification: Optional[str] = Field(
        default=None,
        description="Justification for the response evaluation, providing context from the call transcript.",
    )
    next_steps: Optional[str] = Field(
        default=None,
        description="Any agreed next steps, callback time, or specific requests from the customer.",
    )


def analyze_call_transcript(policy_holder_name, transcript):
    inputs = (
        f"# Policy Holder Name: {policy_holder_name}\n# Call Transcript:\n {transcript}"
    )
    call_analysis = invoke_llm(
        system_prompt=CALL_ANALYSIS_PROMPT,
        user_message=inputs,
        response_format=CallAnalysisOutput,
        json_output=True,
    )

    return call_analysis
