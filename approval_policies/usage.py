from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from approval_policies.models import ModelPricing


@dataclass(frozen=True)
class TokenCounts:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


class UsageMeter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counts = {"policy": TokenCounts(), "reviewer": TokenCounts(), "agent": TokenCounts()}

    def add(
        self, component: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
    ) -> None:
        if component not in self._counts:
            raise ValueError(f"Unknown token usage component: {component}")
        with self._lock:
            old = self._counts[component]
            self._counts[component] = TokenCounts(
                old.input_tokens + max(0, input_tokens),
                old.output_tokens + max(0, output_tokens),
                old.cached_input_tokens + max(0, cached_input_tokens),
            )

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                key: {
                    "input_tokens": counts.input_tokens,
                    "output_tokens": counts.output_tokens,
                    "cached_input_tokens": counts.cached_input_tokens,
                }
                for key, counts in self._counts.items()
            }

    def costs(self, pricing: dict[str, ModelPricing]) -> dict[str, float | None]:
        snapshot = self.snapshot()
        return {
            component: pricing.get(component, ModelPricing()).cost(
                counts["input_tokens"],
                counts["output_tokens"],
                counts["cached_input_tokens"],
            )
            for component, counts in snapshot.items()
        }


def is_openrouter_client(client) -> bool:
    return "openrouter.ai" in str(getattr(client, "base_url", "")).lower()


def add_openrouter_usage_parameter(kwargs: dict, enabled: bool) -> dict:
    if not enabled:
        return kwargs
    kwargs = dict(kwargs)
    extra_body = kwargs.get("extra_body")
    extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
    usage = extra_body.get("usage")
    usage = dict(usage) if isinstance(usage, dict) else {}
    usage["include"] = True
    extra_body["usage"] = usage
    kwargs["extra_body"] = extra_body
    return kwargs


class _TrackedCompletions:
    def __init__(self, wrapped, meter: UsageMeter, include_openrouter_usage: bool):
        self._wrapped = wrapped
        self._meter = meter
        self._include_openrouter_usage = include_openrouter_usage

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def create(self, *args, **kwargs):
        kwargs = add_openrouter_usage_parameter(kwargs, self._include_openrouter_usage)
        response = self._wrapped.create(*args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._meter.add(
                "agent",
                getattr(usage, "prompt_tokens", 0),
                getattr(usage, "completion_tokens", 0),
                getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0),
            )
        return response


class _TrackedChat:
    def __init__(self, wrapped, meter: UsageMeter, include_openrouter_usage: bool):
        self._wrapped = wrapped
        self.completions = _TrackedCompletions(wrapped.completions, meter, include_openrouter_usage)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class TrackedOpenAIClient:
    """Narrow proxy for AgentDojo OpenAILLM's chat completion call."""

    def __init__(self, wrapped, meter: UsageMeter):
        self._wrapped = wrapped
        self.chat = _TrackedChat(wrapped.chat, meter, is_openrouter_client(wrapped))

    def __getattr__(self, name):
        return getattr(self._wrapped, name)
