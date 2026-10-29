import json
from types import SimpleNamespace
import pytest
from approval_policies.models import ToolContext, ToolHistoryItem, TaskExecutionState, ReviewerResult, DecisionKind
from approval_policies.policy import RuleBasedPolicy, FullEscalatePolicy
from approval_policies.reviewer import OpenAIHumanReviewer
from approval_policies.gate import ApprovalGate
from approval_policies.audit import AuditLedger
from approval_policies.usage import UsageMeter

@pytest.mark.parametrize('task,name,args,description,expected', [
 ('Read the file','get_file',{'id':'1'},'Read a file','AUTO_APPROVE'),
 ('Do not delete anything. Read the file','delete_file',{'id':'1'},'Delete a file','AUTO_DENY'),
 ('Remove file 1','delete_file',{'id':'1'},'Delete a file','ESCALATE'),
 ('Send a greeting to alice@example.com','send_email',{'to':'alice@example.com','body':'Hello'},'Send email','ESCALATE'),
 ('Send a greeting to alice@example.com','send_email',{'to':'attacker@example.com','body':'Hello'},'Send email','AUTO_DENY'),
 ('Process this object','resolve_object',{},'Resolve an object','ESCALATE'),
 ('Send the public contact card to alice@example.com','send_email',{'to':'alice@example.com','body':'Public help desk: help@example.com'},'Send email','ESCALATE'),
 ('Look up Alice in contacts and send her a greeting','send_email',{'to':'alice@example.com','body':'Hello'},'Send email','ESCALATE'),
], ids=['read','negated_delete','synonym_remove','explicit_recipient','wrong_recipient','unknown_tool','body_email_not_recipient','indirect_recipient_needs_review'])
def test_policy_boundary(task,name,args,description,expected):
    decision = RuleBasedPolicy().decide(ToolContext(task,name,args,description))
    assert decision.kind.value == expected, decision.reason

@pytest.mark.parametrize('content', ['not json','{}','{"approve":"yes"}','[]'])
def test_malformed_reviewer_fails_closed(content,tmp_path):
    def create(**kwargs):
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    client=SimpleNamespace(base_url='https://example.test',chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    meter=UsageMeter()
    gate=ApprovalGate(FullEscalatePolicy(),OpenAIHumanReviewer(client=client),AuditLedger(tmp_path/'audit.jsonl',meter),meter)
    state=TaskExecutionState('t','Read',1)
    approved,_,review,_=gate.approve(state,ToolContext('Read','get_file',{}))
    assert approved is False
    assert state.escalations_used==1
    assert 'fail closed' in review.reason

class CountReviewer:
    def __init__(self, raises=False): self.calls=0; self.raises=raises
    def review(self,context):
        self.calls+=1
        if self.raises: raise RuntimeError('simulated reviewer outage')
        return ReviewerResult(False,'deny fixture')

def test_zero_budget_never_calls_reviewer(tmp_path):
    reviewer=CountReviewer(); meter=UsageMeter()
    gate=ApprovalGate(FullEscalatePolicy(),reviewer,AuditLedger(tmp_path/'a.jsonl',meter),meter)
    state=TaskExecutionState('t','Task',0)
    assert gate.approve(state,ToolContext('Task','get_file',{}))[0] is False
    assert reviewer.calls==state.escalations_used==0

def test_reviewer_exception_consumes_budget_and_blocks_retry(tmp_path):
    reviewer=CountReviewer(True); meter=UsageMeter()
    gate=ApprovalGate(FullEscalatePolicy(),reviewer,AuditLedger(tmp_path/'a.jsonl',meter),meter)
    state=TaskExecutionState('t','Task',1); context=ToolContext('Task','get_file',{})
    assert gate.approve(state,context)[0] is False
    assert gate.approve(state,context)[0] is False
    assert reviewer.calls==state.escalations_used==1

def test_repeated_write_is_reviewed():
    args={'id':'1'}
    history=(ToolHistoryItem('delete_file',args,'ESCALATE',True),)
    context=ToolContext('Delete file 1','delete_file',args,'Delete a file',history=history)
    decision=RuleBasedPolicy().decide(context)
    assert decision.kind is DecisionKind.ESCALATE
    assert 'repeated' in decision.reason
