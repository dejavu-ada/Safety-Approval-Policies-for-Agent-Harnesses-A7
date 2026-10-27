from approval_policies.models import DecisionKind, ToolContext
from approval_policies.policy import FullApprovePolicy, FullEscalatePolicy, RuleBasedPolicy


def context(tool_name, task="Summarize my files", description="", arguments=None):
    return ToolContext(task, tool_name, arguments or {}, description)


def test_low_risk_read_is_auto_approved():
    decision = RuleBasedPolicy().decide(context("get_file", description="Get a file by ID."))
    assert decision.kind is DecisionKind.AUTO_APPROVE


def test_unrequested_destructive_call_is_auto_denied():
    decision = RuleBasedPolicy().decide(context("delete_file", description="Delete a file by ID."))
    assert decision.kind is DecisionKind.AUTO_DENY


def test_uncertain_call_is_escalated():
    decision = RuleBasedPolicy().decide(context("resolve_contact", description="Resolve a contact."))
    assert decision.kind is DecisionKind.ESCALATE


def test_snake_case_tool_descriptions_and_unrelated_task_verbs_are_parsed():
    policy = RuleBasedPolicy()
    send = context("send_email", task="Delete the obsolete draft", description="Send email to the recipient.")
    delete = context("delete_file", task="Delete file-7", description="Delete a file.")
    assert policy.decide(send).kind is DecisionKind.AUTO_DENY
    assert policy.decide(delete).kind is DecisionKind.ESCALATE


def test_negated_tool_description_does_not_trigger_side_effect_rule():
    decision = RuleBasedPolicy().decide(
        context("get_email", task="Show me the latest email", description="Read-only. Does not send or delete messages.")
    )
    assert decision.kind is DecisionKind.AUTO_APPROVE


def test_baseline_policies_have_expected_decisions():
    proposed = context("get_file")
    assert FullApprovePolicy().decide(proposed).kind is DecisionKind.AUTO_APPROVE
    assert FullEscalatePolicy().decide(proposed).kind is DecisionKind.ESCALATE
