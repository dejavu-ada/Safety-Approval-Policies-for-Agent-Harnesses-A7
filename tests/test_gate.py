import json
from types import SimpleNamespace

from pathlib import Path

from pydantic import BaseModel
from openai import OpenAI

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor
from agentdojo.functions_runtime import Function, FunctionCall, FunctionsRuntime
from agentdojo.types import ChatAssistantMessage, ChatMessage, text_content_block_from_string

from approval_policies.audit import AuditLedger
from approval_policies.gate import ApprovalGate, GatedToolsExecutor, build_gated_pipeline
from approval_policies.models import (
    DecisionKind,
    ModelPricing,
    ReviewerResult,
    TaskExecutionState,
    ToolContext,
)
from approval_policies.policy import FullApprovePolicy, FullEscalatePolicy, RuleBasedPolicy
from approval_policies.reviewer import OpenAIHumanReviewer
from approval_policies.state import CURRENT_TASK
from approval_policies.usage import TrackedOpenAIClient, UsageMeter


class CallArgs(BaseModel):
    target: str


class NestedArgs(BaseModel):
    value: str


class FixedReviewer:
    def __init__(self, approved: bool):
        self.approved = approved
        self.calls = 0

    def review(self, context):
        self.calls += 1
        return ReviewerResult(self.approved, "fixed test result", 7, 3, 0, "test-reviewer")


class ToolRequestLLM(BasePipelineElement):
    def __init__(self, call):
        self.call = call

    def query(self, query, runtime, env=None, messages=(), extra_args=None):
        message = ChatAssistantMessage(
            role="assistant", content=[text_content_block_from_string("done")], tool_calls=None
        )
        return query, runtime, env, [*messages, message], extra_args or {}


def make_runtime(calls):
    def perform(target: str) -> str:
        calls.append(target)
        return "performed"

    function = Function(
        name="delete_file",
        description="Delete a file by target.",
        parameters=CallArgs,
        dependencies={},
        run=perform,
        full_docstring="Delete a file.",
        return_type=str,
    )
    return FunctionsRuntime([function])


def make_call(target="file-1"):
    return FunctionCall(function="delete_file", args={"target": target}, id="call-1")


def make_gate(tmp_path: Path, policy, reviewer, meter=None):
    meter = meter or UsageMeter()
    ledger = AuditLedger(tmp_path / "audit.jsonl", meter, {"reviewer": ModelPricing(1, 2)})
    return ApprovalGate(policy, reviewer, ledger, meter)


def test_reviewer_approval_and_rejection_are_respected(tmp_path):
    context = ToolContext("Delete file-1", "delete_file", {"target": "file-1"})
    for result, expected in ((True, True), (False, False)):
        reviewer = FixedReviewer(result)
        meter = UsageMeter()
        gate = make_gate(tmp_path / str(result), FullEscalatePolicy(), reviewer, meter)
        approved, decision, review, _ = gate.approve(TaskExecutionState("task-1", "Delete file-1", 2), context)
        assert decision.kind is DecisionKind.ESCALATE
        assert approved is expected
        assert review is not None and review.approved is expected
        assert reviewer.calls == 1
        assert meter.snapshot()["reviewer"]["input_tokens"] == 7
        assert meter.snapshot()["reviewer"]["output_tokens"] == 3


def test_budget_exhaustion_denies_later_escalations(tmp_path):
    reviewer = FixedReviewer(True)
    gate = make_gate(tmp_path, FullEscalatePolicy(), reviewer)
    state = TaskExecutionState("task-1", "Task", 1)
    context = ToolContext("Task", "unknown", {})
    first = gate.approve(state, context)
    second = gate.approve(state, context)
    assert first[0] is True
    assert second[0] is False
    assert second[2] is not None and second[2].reason == "escalation budget exhausted"
    assert second[2].invoked is False
    assert state.escalations_used == 1
    assert reviewer.calls == 1


def test_full_approve_and_full_escalate_baselines(tmp_path):
    call_context = ToolContext("Task", "unknown", {})
    approve_gate = make_gate(tmp_path / "approve", FullApprovePolicy(), FixedReviewer(False))
    escalate_gate = make_gate(tmp_path / "escalate", FullEscalatePolicy(), FixedReviewer(True))
    assert approve_gate.approve(TaskExecutionState("task-1", "Task", 0), call_context)[0] is True
    assert escalate_gate.approve(TaskExecutionState("task-1", "Task", 1), call_context)[0] is True


def test_denied_tool_is_not_executed_and_is_logged(tmp_path):
    side_effects = []
    runtime = make_runtime(side_effects)
    gate = make_gate(tmp_path, RuleBasedPolicy(), FixedReviewer(True))
    executor = GatedToolsExecutor(gate, 1)
    call = make_call()
    message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
    result = executor.query("Summarize my files", runtime, None, [message])
    assert side_effects == []
    assert "Approval Gate denied" in result[3][-1]["error"]
    assert '"executed": false' in (tmp_path / "audit.jsonl").read_text()


def test_task_budgets_are_independent(tmp_path):
    reviewer = FixedReviewer(True)
    gate = make_gate(tmp_path, FullEscalatePolicy(), reviewer)
    context = ToolContext("Task", "unknown", {})
    state_a = TaskExecutionState("task-a", "Task A", 1)
    state_b = TaskExecutionState("task-b", "Task B", 1)
    assert gate.approve(state_a, context)[0]
    assert gate.approve(state_a, context)[0] is False
    assert gate.approve(state_b, context)[0]
    assert state_a.escalations_used == 1
    assert state_b.escalations_used == 1


def test_nested_agentdojo_calls_are_approved_separately(tmp_path):
    effects = []
    runtime = make_runtime(effects)

    def consume(value: str) -> str:
        effects.append("parent:" + value)
        return value

    parent = Function(
        name="consume_value",
        description="Consume a supplied value.",
        parameters=NestedArgs,
        dependencies={},
        run=consume,
        full_docstring="Consume a supplied value.",
        return_type=str,
    )
    runtime.register_function(parent)
    nested = FunctionCall(function="delete_file", args={"target": "file-1"})
    outer = FunctionCall(function="consume_value", args={"value": nested}, id="outer")
    message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[outer])
    gate = make_gate(tmp_path, RuleBasedPolicy(), FixedReviewer(True))
    GatedToolsExecutor(gate, 1).query("Summarize my files", runtime, None, [message])
    assert effects == []
    entries = [__import__("json").loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    calls = [entry for entry in entries if entry["event_type"] == "tool_call"]
    assert {entry["tool_name"] for entry in calls} == {"consume_value", "delete_file"}
    nested_call = next(entry for entry in calls if entry["tool_name"] == "delete_file")
    assert nested_call["policy_decision"] == "AUTO_DENY"
    assert nested_call["executed"] is False


def test_outer_tool_does_not_run_when_approved_nested_call_fails(tmp_path):
    effects = []

    def fail_nested(target: str) -> str:
        effects.append("nested")
        raise RuntimeError("nested failure")

    def consume(value: str) -> str:
        effects.append("parent")
        return value

    child = Function(
        name="get_value",
        description="Get a value.",
        parameters=CallArgs,
        dependencies={},
        run=fail_nested,
        full_docstring="Get a value.",
        return_type=str,
    )
    parent = Function(
        name="consume_value",
        description="Consume a supplied value.",
        parameters=NestedArgs,
        dependencies={},
        run=consume,
        full_docstring="Consume a supplied value.",
        return_type=str,
    )
    runtime = FunctionsRuntime([child, parent])
    nested = FunctionCall(function="get_value", args={"target": "item"})
    outer = FunctionCall(function="consume_value", args={"value": nested}, id="outer")
    message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[outer])
    gate = make_gate(tmp_path, FullApprovePolicy(), FixedReviewer(True))
    result = GatedToolsExecutor(gate, 0).query("Use this value", runtime, None, [message])

    assert effects == ["nested"]
    assert "Nested call" in result[3][-1]["error"]
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    parent_call = next(entry for entry in entries if entry.get("tool_name") == "consume_value")
    assert parent_call["executed"] is False


def test_rule_policy_escalates_requested_side_effect(tmp_path):
    side_effects = []
    runtime = make_runtime(side_effects)
    reviewer = FixedReviewer(True)
    gate = make_gate(tmp_path, RuleBasedPolicy(), reviewer)
    executor = GatedToolsExecutor(gate, 1)
    call = make_call()
    message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
    executor.query("Delete file-1", runtime, None, [message])
    assert side_effects == ["file-1"]
    assert reviewer.calls == 1


def test_agentdojo_executor_is_replaced_and_gate_controls_execution(tmp_path):
    effects = []
    runtime = make_runtime(effects)
    call = make_call()
    requested_message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
    base = AgentPipeline([ToolsExecutionLoop([ToolsExecutor(), ToolRequestLLM(call)])])
    config = PipelineConfig(
        llm="local", model_id="fake", defense=None, system_message_name=None, system_message="test"
    )
    meter = UsageMeter()
    ledger = AuditLedger(tmp_path / "integrated.jsonl", meter)
    reviewer = FixedReviewer(True)
    pipeline = build_gated_pipeline(
        config,
        policy=RuleBasedPolicy(),
        reviewer=reviewer,
        ledger=ledger,
        usage_meter=meter,
        escalation_budget=1,
        base_pipeline=base,
    )
    loop = next(item for item in pipeline.elements if isinstance(item, ToolsExecutionLoop))
    assert sum(isinstance(item, GatedToolsExecutor) for item in loop.elements) == 1
    assert not any(type(item) is ToolsExecutor for item in loop.elements)
    result = pipeline.query("Summarize my files", runtime, None, [requested_message])
    assert effects == []
    tool_result = next(item for item in result[3] if item["role"] == "tool")
    assert "Approval Gate denied" in tool_result["error"]


def test_pipeline_resets_budget_between_task_runs(tmp_path):
    effects = []
    runtime = make_runtime(effects)
    first_task_calls = [make_call("one"), FunctionCall(function="delete_file", args={"target": "two"}, id="call-2")]
    first_message = ChatAssistantMessage(role="assistant", content=None, tool_calls=first_task_calls)
    second_message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[make_call("three")])
    base = AgentPipeline([ToolsExecutionLoop([ToolsExecutor(), ToolRequestLLM(make_call())])])
    config = PipelineConfig(
        llm="local", model_id="fake", defense=None, system_message_name=None, system_message="test"
    )
    meter = UsageMeter()
    ledger = AuditLedger(tmp_path / "budget.jsonl", meter)
    reviewer = FixedReviewer(True)
    pipeline = build_gated_pipeline(
        config,
        policy=FullEscalatePolicy(),
        reviewer=reviewer,
        ledger=ledger,
        usage_meter=meter,
        escalation_budget=1,
        task_id_resolver=lambda prompt: prompt,
        base_pipeline=base,
    )
    first_result = pipeline.query("task-a", runtime, None, [first_message])
    second_result = pipeline.query("task-b", runtime, None, [second_message])
    first_tools = [item for item in first_result[3] if item["role"] == "tool"]
    second_tools = [item for item in second_result[3] if item["role"] == "tool"]
    assert first_tools[0]["error"] is None
    assert "budget exhausted" in first_tools[1]["error"]
    assert second_tools[0]["error"] is None
    assert reviewer.calls == 2


def test_factory_builds_from_installed_agentdojo_config(tmp_path):
    meter = UsageMeter()
    ledger = AuditLedger(tmp_path / "factory.jsonl", meter)
    config = PipelineConfig(
        llm=OpenAILLM(OpenAI(api_key="offline-test-key"), "gpt-4o-mini"),
        model_id=None,
        defense=None,
        system_message_name=None,
        system_message=None,
    )
    pipeline = build_gated_pipeline(
        config,
        policy=RuleBasedPolicy(),
        reviewer=FixedReviewer(False),
        ledger=ledger,
        usage_meter=meter,
        escalation_budget=1,
    )
    loops = [element for element in pipeline.elements if isinstance(element, ToolsExecutionLoop)]
    assert len(loops) == 1
    assert sum(isinstance(element, GatedToolsExecutor) for element in loops[0].elements) == 1
    llm = next(element for element in loops[0].elements if isinstance(element, OpenAILLM))
    assert isinstance(llm.client, TrackedOpenAIClient)


def test_tool_parameters_are_redacted_in_audit_log(tmp_path):
    calls = []
    runtime = make_runtime(calls)
    runtime.functions["delete_file"].description = "Read a file by target."
    runtime.functions["delete_file"].name = "get_file"
    runtime.functions["get_file"] = runtime.functions.pop("delete_file")
    call = FunctionCall(function="get_file", args={"target": "doc", "api_key": "hidden"}, id="call-1")
    message = ChatAssistantMessage(role="assistant", content=None, tool_calls=[call])
    gate = make_gate(tmp_path, FullApprovePolicy(), FixedReviewer(False))
    GatedToolsExecutor(gate, 0).query("Read file", runtime, None, [message])
    log = (tmp_path / "audit.jsonl").read_text()
    assert "hidden" not in log
    assert "[REDACTED]" in log


def test_reviewer_gets_only_visible_context_and_redacts_credentials():
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            usage = SimpleNamespace(
                prompt_tokens=12,
                completion_tokens=4,
                prompt_tokens_details=SimpleNamespace(cached_tokens=2),
            )
            message = SimpleNamespace(content='{"approve": false, "reason": "unrequested recipient"}')
            return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message)])

    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=Completions()),
    )
    reviewer = OpenAIHumanReviewer(model="test-reviewer", client=client)
    result = reviewer.review(
        ToolContext(
            "Review this request; api_key=not-for-reviewer",
            "send_email",
            {"recipient": "external@example.com", "api_key": "secret-value"},
        )
    )
    payload = json.loads(captured["messages"][1]["content"])
    assert set(payload) == {"task", "proposed_tool_call", "prior_tool_actions"}
    assert "not-for-reviewer" not in captured["messages"][1]["content"]
    assert "secret-value" not in captured["messages"][1]["content"]
    assert "injection_task_id" not in captured["messages"][1]["content"]
    assert result.approved is False
    assert result.cached_input_tokens == 2
    assert captured["extra_body"]["usage"]["include"] is True
