from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from approval_policies.models import ModelPricing, TaskExecutionState
from approval_policies.usage import UsageMeter


_SENSITIVE_KEY = re.compile(
    r"(password|secret|api[_-]?key|authorization|credential|bearer|"
    r"(?:access|refresh|auth|session|id|private)[_-]?token|token[_-]?(?:value|secret|key)|^token$)",
    re.I,
)
_INLINE_SECRET = re.compile(r"(?i)(api[_ -]?key|password|secret|token|authorization)\s*[:=]\s*[^\s,;]+")
_KEY_LIKE_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b")


def _safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _safe(value.model_dump())
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            return _KEY_LIKE_VALUE.sub("[REDACTED]", _INLINE_SECRET.sub(r"\1=[REDACTED]", value))
        return value
    return str(value)


class AuditLedger:
    """Append-only JSONL audit log for tool calls and task-level usage summaries."""

    def __init__(
        self,
        path: str | Path,
        usage_meter: UsageMeter,
        pricing: dict[str, ModelPricing] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_meter = usage_meter
        self.pricing = pricing or {}
        self._lock = Lock()

    def write(self, event: dict[str, Any]) -> None:
        payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **_safe(event)}
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def tool_call(
        self,
        *,
        task_id: str,
        case_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
        policy_name: str,
        policy_decision: str,
        policy_reason: str,
        risk_score: float,
        reviewer_result: dict[str, Any] | None,
        approval_outcome: str,
        executed: bool,
        execution_error: str | None,
        escalations_used: int,
        escalation_budget: int,
        token_usage_delta: dict[str, dict[str, int]],
    ) -> None:
        costs = {
            component: self.pricing.get(component, ModelPricing()).cost(
                counts.get("input_tokens", 0),
                counts.get("output_tokens", 0),
                counts.get("cached_input_tokens", 0),
            )
            for component, counts in token_usage_delta.items()
        }
        self.write(
            {
                "event_type": "tool_call",
                "task_id": task_id,
                "case_id": case_id,
                "tool_name": tool_name,
                "parameters": arguments,
                "policy_name": policy_name,
                "policy_decision": policy_decision,
                "policy_reason": policy_reason,
                "risk_score": risk_score,
                "reviewer_result": reviewer_result,
                "approval_outcome": approval_outcome,
                "executed": executed,
                "execution_error": execution_error,
                "escalations_used": escalations_used,
                "escalation_budget": escalation_budget,
                "token_usage": token_usage_delta,
                "api_cost_usd": costs,
            }
        )

    def task_summary(self, state: TaskExecutionState, remaining_agent_usage: dict[str, int]) -> None:
        totals = self.usage_meter.snapshot()
        baseline = state.usage_start_snapshot or {
            component: {"input_tokens": 0, "output_tokens": 0} for component in totals
        }
        token_usage = {
            component: {
                key: max(0, totals[component][key] - baseline[component].get(key, 0))
                for key in ("input_tokens", "output_tokens", "cached_input_tokens")
            }
            for component in totals
        }
        costs = {
            component: self.pricing.get(component, ModelPricing()).cost(
                counts["input_tokens"], counts["output_tokens"], counts["cached_input_tokens"]
            )
            for component, counts in token_usage.items()
        }
        self.write(
            {
                "event_type": "task_summary",
                "task_id": state.task_id,
                "case_id": state.case_id,
                "escalations_used": state.escalations_used,
                "escalation_budget": state.escalation_budget,
                "remaining_agent_usage": remaining_agent_usage,
                "token_usage_total": token_usage,
                "api_cost_usd_total": costs,
            }
        )
