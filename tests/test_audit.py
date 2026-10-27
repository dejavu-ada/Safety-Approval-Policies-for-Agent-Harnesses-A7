import json

from approval_policies.audit import AuditLedger
from approval_policies.models import TaskExecutionState
from approval_policies.usage import UsageMeter


def test_audit_redacts_credentials_but_keeps_token_usage(tmp_path):
    meter = UsageMeter()
    ledger = AuditLedger(tmp_path / "audit.jsonl", meter)
    meter.add("agent", input_tokens=120, output_tokens=30, cached_input_tokens=20)
    state = TaskExecutionState(
        "task-1",
        "Read a report",
        2,
        case_id="case-1",
        usage_start_snapshot={
            "policy": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            "reviewer": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            "agent": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
        },
    )

    ledger.tool_call(
        task_id=state.task_id,
        case_id=state.case_id,
        tool_name="read_report",
        arguments={"access_token": "credential-value", "input_tokens": 7},
        policy_name="rule_based",
        policy_decision="AUTO_APPROVE",
        policy_reason="read-only",
        risk_score=0.1,
        reviewer_result=None,
        approval_outcome="approve",
        executed=True,
        execution_error=None,
        escalations_used=0,
        escalation_budget=2,
        token_usage_delta={
            "agent": {"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 20},
            "reviewer": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            "policy": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
        },
    )
    ledger.task_summary(
        state,
        remaining_agent_usage={"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
    )

    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert events[0]["parameters"]["access_token"] == "[REDACTED]"
    assert events[0]["parameters"]["input_tokens"] == 7
    assert events[0]["token_usage"]["agent"]["input_tokens"] == 120
    assert events[1]["token_usage_total"]["agent"]["input_tokens"] == 120
    assert events[1]["remaining_agent_usage"]["output_tokens"] == 0
