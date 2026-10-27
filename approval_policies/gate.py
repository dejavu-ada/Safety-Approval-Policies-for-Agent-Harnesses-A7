from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from weakref import WeakKeyDictionary

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
from agentdojo.agent_pipeline.errors import AbortAgentError
from agentdojo.logging import Logger
from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
from agentdojo.agent_pipeline.tool_execution import (
    EMPTY_FUNCTION_NAME,
    ToolsExecutionLoop,
    ToolsExecutor,
    is_string_list,
    tool_result_to_str,
)
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime
from agentdojo.types import ChatMessage, ChatToolResultMessage, text_content_block_from_string

from approval_policies.audit import AuditLedger
from approval_policies.models import (
    ApprovalDecision,
    DecisionKind,
    ReviewerResult,
    TaskExecutionState,
    ToolContext,
    ToolHistoryItem,
)
from approval_policies.policy import ApprovalPolicy
from approval_policies.reviewer import HumanReviewer
from approval_policies.state import CURRENT_TASK, benchmark_case_identity, task_state_scope
from approval_policies.usage import TokenCounts, TrackedOpenAIClient, UsageMeter


def _usage_delta(before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {
        component: {
            key: max(0, after[component][key] - before[component][key])
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")
        }
        for component in ("policy", "reviewer", "agent")
    }


class ApprovalGate:
    def __init__(
        self,
        policy: ApprovalPolicy,
        reviewer: HumanReviewer,
        ledger: AuditLedger,
        usage_meter: UsageMeter,
    ) -> None:
        self.policy = policy
        self.reviewer = reviewer
        self.ledger = ledger
        self.usage_meter = usage_meter

    def approve(self, state: TaskExecutionState, context: ToolContext) -> tuple[bool, ApprovalDecision, ReviewerResult | None, dict[str, dict[str, int]]]:
        before_usage = self.usage_meter.snapshot()
        try:
            decision = self.policy.decide(context)
        except Exception as error:
            decision = ApprovalDecision(DecisionKind.AUTO_DENY, f"policy error; fail closed: {error}", 1.0)

        reviewer_result: ReviewerResult | None = None
        if decision.kind is DecisionKind.AUTO_APPROVE:
            approved = True
        elif decision.kind is DecisionKind.AUTO_DENY:
            approved = False
        elif state.escalations_used >= state.escalation_budget:
            approved = False
            reviewer_result = ReviewerResult(False, "escalation budget exhausted", invoked=False)
        else:
            state.escalations_used += 1
            try:
                reviewer_result = self.reviewer.review(context)
            except Exception as error:
                reviewer_result = ReviewerResult(False, f"reviewer error; fail closed: {error}")
            self.usage_meter.add(
                "reviewer",
                reviewer_result.input_tokens,
                reviewer_result.output_tokens,
                reviewer_result.cached_input_tokens,
            )
            approved = reviewer_result.approved

        after_usage = self.usage_meter.snapshot()
        usage_delta = _usage_delta(before_usage, after_usage)
        # The agent completion that proposed this tool call occurred immediately before
        # approval. Attribute its unlogged token delta to this action.
        agent_totals = after_usage["agent"]
        agent_seen = getattr(state, "last_seen_agent_tokens", TokenCounts())
        usage_delta["agent"] = {
            "input_tokens": max(
                usage_delta["agent"]["input_tokens"], agent_totals["input_tokens"] - agent_seen.input_tokens
            ),
            "output_tokens": max(
                usage_delta["agent"]["output_tokens"], agent_totals["output_tokens"] - agent_seen.output_tokens
            ),
            "cached_input_tokens": max(
                usage_delta["agent"]["cached_input_tokens"],
                agent_totals["cached_input_tokens"] - agent_seen.cached_input_tokens,
            ),
        }
        state.last_seen_agent_tokens = TokenCounts(
            agent_totals["input_tokens"],
            agent_totals["output_tokens"],
            agent_totals["cached_input_tokens"],
        )
        return approved, decision, reviewer_result, usage_delta


class GatedToolsExecutor(ToolsExecutor):
    """AgentDojo executor that routes every proposed tool call through ApprovalGate."""

    def __init__(self, gate: ApprovalGate, escalation_budget: int, tool_output_formatter=tool_result_to_str):
        super().__init__(tool_output_formatter)
        self.gate = gate
        self.escalation_budget = escalation_budget

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = (),
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        if not messages or messages[-1]["role"] != "assistant":
            return query, runtime, env, messages, extra_args
        tool_calls = messages[-1].get("tool_calls")
        if not tool_calls:
            return query, runtime, env, messages, extra_args

        state = CURRENT_TASK.get()
        if state is None:
            task_id, case_id = benchmark_case_identity(query)
            state, token = task_state_scope(task_id, query, self.escalation_budget, case_id)
            state.usage_start_snapshot = self.gate.usage_meter.snapshot()
        else:
            token = None

        results: list[ChatToolResultMessage] = []
        try:
            for call in tool_calls:
                name = call.function
                args = dict(call.args)
                executed, result, error = self._execute_call(name, args, state, runtime, env, 0)
                content = self.output_formatter(result) if executed else ""
                result_msg = ChatToolResultMessage(
                    role="tool",
                    content=[text_content_block_from_string(content)],
                    tool_call_id=call.id,
                    tool_call=call,
                    error=error,
                )
                results.append(result_msg)
        finally:
            if token is not None:
                self.gate.ledger.task_summary(state, self._remaining_agent_usage(state))
                CURRENT_TASK.reset(token)

        return query, runtime, env, [*messages, *results], extra_args

    def _execute_call(
        self,
        name: str,
        args: dict[str, Any],
        state: TaskExecutionState,
        runtime: FunctionsRuntime,
        env: Env,
        depth: int,
    ) -> tuple[bool, Any, str | None]:
        from ast import literal_eval

        from agentdojo.functions_runtime import FunctionCall

        args = {
            key: literal_eval(value) if isinstance(value, str) and is_string_list(value) else value
            for key, value in args.items()
        }
        function = next((fn for fn in runtime.functions.values() if fn.name == name), None)
        if depth > 16:
            decision = ApprovalDecision(DecisionKind.AUTO_DENY, "maximum nested-call depth exceeded", 1.0)
            error = f"Approval Gate denied nested call: {decision.reason}."
            self.gate.ledger.tool_call(
                task_id=state.task_id,
                case_id=state.case_id,
                tool_name=name,
                arguments=args,
                policy_name=self.gate.policy.name,
                policy_decision=decision.kind.value,
                policy_reason=decision.reason,
                risk_score=decision.risk_score,
                reviewer_result=None,
                approval_outcome="deny",
                executed=False,
                execution_error=error,
                escalations_used=state.escalations_used,
                escalation_budget=state.escalation_budget,
                token_usage_delta={
                    component: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
                    for component in ("policy", "reviewer", "agent")
                },
            )
            state.history.append(ToolHistoryItem(name, args, decision.kind.value, False))
            return False, "", error

        if name == EMPTY_FUNCTION_NAME or function is None:
            decision = ApprovalDecision(
                DecisionKind.AUTO_DENY,
                "empty function name" if name == EMPTY_FUNCTION_NAME else "unknown tool",
                1.0,
            )
            reviewer_result = None
            approved = False
            usage_delta = {
                key: {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
                for key in ("policy", "reviewer", "agent")
            }
            result: Any = ""
            error = f"Approval Gate denied tool call: {decision.reason}."
            executed = False
        else:
            context = ToolContext(
                task_description=state.task_description,
                tool_name=name,
                arguments=args,
                tool_description=function.description,
                parameter_schema=function.parameters.model_json_schema(),
                history=tuple(state.history),
            )
            approved, decision, reviewer_result, usage_delta = self.gate.approve(state, context)
            result = ""
            error = None
            executed = False
            if not approved:
                error = f"Approval Gate denied tool call: {reviewer_result.reason if reviewer_result else decision.reason}."
            else:
                resolved_args: dict[str, Any] = {}
                for key, value in args.items():
                    if isinstance(value, FunctionCall):
                        nested_executed, nested_result, nested_error = self._execute_call(
                            value.function, dict(value.args), state, runtime, env, depth + 1
                        )
                        if not nested_executed or nested_error is not None:
                            error = f"Nested call for parameter '{key}' was denied or failed: {nested_error}"
                            break
                        resolved_args[key] = nested_result
                    else:
                        resolved_args[key] = value
                if error is None:
                    try:
                        result, error = runtime.run_function(env, name, resolved_args)
                        executed = True
                    except Exception as exception:
                        error = f"{type(exception).__name__}: {exception}"
                        executed = True

        reviewer_payload = None
        if reviewer_result is not None:
            reviewer_payload = {
                "decision": "approve" if reviewer_result.approved else "deny",
                "reason": reviewer_result.reason,
                "input_tokens": reviewer_result.input_tokens,
                "output_tokens": reviewer_result.output_tokens,
                "cached_input_tokens": reviewer_result.cached_input_tokens,
                "model": reviewer_result.model,
                "invoked": reviewer_result.invoked,
            }
        self.gate.ledger.tool_call(
            task_id=state.task_id,
            case_id=state.case_id,
            tool_name=name,
            arguments=args,
            policy_name=self.gate.policy.name,
            policy_decision=decision.kind.value,
            policy_reason=decision.reason,
            risk_score=decision.risk_score,
            reviewer_result=reviewer_payload,
            approval_outcome="approve" if approved else "deny",
            executed=executed,
            execution_error=error,
            escalations_used=state.escalations_used,
            escalation_budget=state.escalation_budget,
            token_usage_delta=usage_delta,
        )
        state.history.append(ToolHistoryItem(name, args, decision.kind.value, executed))
        return executed, result, error

    def _remaining_agent_usage(self, state: TaskExecutionState) -> dict[str, int]:
        totals = self.gate.usage_meter.snapshot()["agent"]
        seen = getattr(state, "last_seen_agent_tokens", TokenCounts())
        remaining = {
            "input_tokens": max(0, totals["input_tokens"] - seen.input_tokens),
            "output_tokens": max(0, totals["output_tokens"] - seen.output_tokens),
            "cached_input_tokens": max(0, totals["cached_input_tokens"] - seen.cached_input_tokens),
        }
        return remaining


class PolicyAgentPipeline(AgentPipeline):
    def __init__(
        self,
        elements,
        *,
        gate: ApprovalGate,
        escalation_budget: int,
        task_id_resolver: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(elements)
        self.gate = gate
        self.escalation_budget = escalation_budget
        self.task_id_resolver = task_id_resolver
        self._retry_states: WeakKeyDictionary[Any, TaskExecutionState] = WeakKeyDictionary()

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = (),
        extra_args: dict = {},
    ):
        logger = Logger.get()
        logger_context = getattr(logger, "context", None)
        retry_key = logger if logger_context is not None else None
        state = self._retry_states.get(retry_key) if retry_key is not None else None
        if state is None:
            task_id, case_id = benchmark_case_identity(query, self.task_id_resolver)
            state, token = task_state_scope(task_id, query, self.escalation_budget, case_id)
            state.usage_start_snapshot = self.gate.usage_meter.snapshot()
            if retry_key is not None:
                self._retry_states[retry_key] = state
        else:
            token = CURRENT_TASK.set(state)
        retrying = False
        try:
            try:
                return super().query(query, runtime, env, messages, extra_args)
            except AbortAgentError:
                # AgentDojo retries this run after an AbortAgentError. Keep the same
                # budget and history for that retry within the same TraceLogger scope.
                retrying = True
                raise
        finally:
            if not retrying:
                self.gate.ledger.task_summary(state, self._remaining_agent_usage(state))
                if retry_key is not None:
                    self._retry_states.pop(retry_key, None)
            CURRENT_TASK.reset(token)

    def _remaining_agent_usage(self, state: TaskExecutionState) -> dict[str, int]:
        totals = self.gate.usage_meter.snapshot()["agent"]
        seen = getattr(state, "last_seen_agent_tokens", TokenCounts())
        return {
            "input_tokens": max(0, totals["input_tokens"] - seen.input_tokens),
            "output_tokens": max(0, totals["output_tokens"] - seen.output_tokens),
            "cached_input_tokens": max(0, totals["cached_input_tokens"] - seen.cached_input_tokens),
        }


def build_gated_pipeline(
    config: PipelineConfig,
    *,
    policy: ApprovalPolicy,
    reviewer: HumanReviewer,
    ledger: AuditLedger,
    usage_meter: UsageMeter,
    escalation_budget: int,
    task_id_resolver: Callable[[str], str] | None = None,
    base_pipeline: AgentPipeline | None = None,
) -> PolicyAgentPipeline:
    """Build AgentDojo's real pipeline and replace its only tool executor with the gate."""
    if escalation_budget < 0:
        raise ValueError("escalation_budget must be non-negative")
    base = base_pipeline or AgentPipeline.from_config(config)
    gate = ApprovalGate(policy, reviewer, ledger, usage_meter)
    loops = [element for element in base.elements if isinstance(element, ToolsExecutionLoop)]
    if len(loops) != 1:
        raise ValueError(f"Expected one AgentDojo ToolsExecutionLoop, found {len(loops)}")
    replaced = 0
    for loop in loops:
        new_elements = []
        for element in loop.elements:
            if isinstance(element, ToolsExecutor):
                new_elements.append(GatedToolsExecutor(gate, escalation_budget, element.output_formatter))
                replaced += 1
            else:
                new_elements.append(element)
        loop.elements = new_elements
    if replaced != 1:
        raise ValueError(f"Expected to replace one AgentDojo ToolsExecutor, replaced {replaced}")

    # Track OpenAI AgentDojo usage where the built-in OpenAILLM exposes a client.
    for component in base.elements:
        if isinstance(component, ToolsExecutionLoop):
            for nested in component.elements:
                if isinstance(nested, OpenAILLM):
                    nested.client = TrackedOpenAIClient(nested.client, usage_meter)

    pipeline = PolicyAgentPipeline(
        base.elements,
        gate=gate,
        escalation_budget=escalation_budget,
        task_id_resolver=task_id_resolver,
    )
    pipeline.name = f"{base.name or 'agent'}-{policy.name}-approval"
    return pipeline
