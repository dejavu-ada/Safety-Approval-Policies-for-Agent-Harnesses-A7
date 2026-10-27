from types import SimpleNamespace
from unittest.mock import Mock

from approval_policies.usage import TrackedOpenAIClient, UsageMeter


def test_openrouter_agent_request_asks_for_usage_and_records_it():
    meter = UsageMeter()
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        )
    )
    create = Mock(return_value=response)
    wrapped = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    client = TrackedOpenAIClient(wrapped, meter)
    client.chat.completions.create(
        model="openai/gpt-4o-mini",
        messages=[],
        extra_body={"custom_field": "keep"},
    )

    assert create.call_args.kwargs["extra_body"] == {
        "custom_field": "keep",
        "usage": {"include": True},
    }
    assert meter.snapshot()["agent"] == {
        "input_tokens": 100,
        "output_tokens": 25,
        "cached_input_tokens": 10,
    }
