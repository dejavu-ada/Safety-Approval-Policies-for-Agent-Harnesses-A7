from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DecisionKind(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    AUTO_DENY = "AUTO_DENY"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class ToolHistoryItem:
    tool_name: str
    arguments: dict[str, Any]
    decision: str
    executed: bool


@dataclass(frozen=True)
class ToolContext:
    """Decision-time information, deliberately excluding task IDs and benchmark labels."""

    task_description: str
    tool_name: str
    arguments: dict[str, Any]
    tool_description: str = ""
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    history: tuple[ToolHistoryItem, ...] = ()


@dataclass(frozen=True)
class ApprovalDecision:
    kind: DecisionKind
    reason: str
    risk_score: float


@dataclass(frozen=True)
class ReviewerResult:
    approved: bool
    reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    model: str | None = None
    invoked: bool = True


@dataclass
class TaskExecutionState:
    task_id: str
    task_description: str
    escalation_budget: int
    case_id: str | None = None
    escalations_used: int = 0
    history: list[ToolHistoryItem] = field(default_factory=list)
    usage_start_snapshot: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens. Leave rates unset to report token counts only."""

    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    cached_input_usd_per_million: float | None = None

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float | None:
        if self.input_usd_per_million is None or self.output_usd_per_million is None:
            return None
        cached_rate = self.cached_input_usd_per_million
        cached_cost = 0.0
        if cached_input_tokens and cached_rate is not None:
            cached_input_tokens = min(input_tokens, cached_input_tokens)
            cached_cost = cached_input_tokens * cached_rate
        else:
            cached_input_tokens = 0
        return (
            (input_tokens - cached_input_tokens) * self.input_usd_per_million
            + cached_cost
            + output_tokens * self.output_usd_per_million
        ) / 1_000_000
