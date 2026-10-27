from __future__ import annotations

import contextvars
import hashlib
from collections.abc import Callable
from contextvars import Token
from typing import Any

from approval_policies.models import TaskExecutionState


CURRENT_TASK: contextvars.ContextVar[TaskExecutionState | None] = contextvars.ContextVar(
    "approval_policy_task_state", default=None
)


def stable_task_id(description: str) -> str:
    return "task-" + hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def task_state_scope(
    task_id: str,
    description: str,
    escalation_budget: int,
    case_id: str | None = None,
) -> tuple[TaskExecutionState, Token[TaskExecutionState | None]]:
    state = TaskExecutionState(task_id, description, escalation_budget, case_id=case_id)
    return state, CURRENT_TASK.set(state)


def resolve_task_id(
    query: str,
    resolver: Callable[[str], str] | None = None,
) -> str:
    if resolver is not None:
        return resolver(query)
    return stable_task_id(query)


def benchmark_case_identity(query: str, resolver: Callable[[str], str] | None = None) -> tuple[str, str]:
    """Return a visible task ID and an opaque case ID from AgentDojo's run logger.

    Injection IDs are hashed and retained only for accounting. They are never placed in
    ToolContext or sent to a policy/reviewer.
    """
    try:
        from agentdojo.logging import Logger

        context: dict[str, Any] = getattr(Logger.get(), "context", {})
    except (ImportError, AttributeError):
        context = {}
    suite = context.get("suite_name")
    user_task = context.get("user_task_id")
    if suite and user_task:
        injection_task = context.get("injection_task_id") or "no-injection"
        raw_case = f"{suite}\0{user_task}\0{injection_task}"
        return str(user_task), "case-" + hashlib.sha256(raw_case.encode("utf-8")).hexdigest()[:20]
    task_id = resolve_task_id(query, resolver)
    return task_id, "case-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:20]
