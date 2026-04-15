import json
from datetime import datetime
from typing import Optional
from litellm import completion


def get_current_date_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def calculate_duration_in_minutes(started_at: str, ended_at: str) -> float:
    datetime_format = "%Y-%m-%dT%H:%M:%SZ"
    start_time = datetime.strptime(started_at, datetime_format)
    end_time = datetime.strptime(ended_at, datetime_format)
    return (end_time - start_time).total_seconds() / 60


def invoke_llm(
    system_prompt: str,
    user_message: str,
    model: str = "groq/llama-3.3-70b-versatile",
    response_format=None,
    json_output: bool = False,
):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    response = completion(
        model=model,
        messages=messages,
        temperature=0.1,
        response_format=response_format,
    )
    output = response.choices[0].message.content

    if json_output:
        return json.loads(output)
    return output
