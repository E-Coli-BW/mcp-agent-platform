from __future__ import annotations

from app.runtime.policy import PolicyAction, PolicyRule, RuntimePolicyEvaluator
from app.runtime.tool_invocation import ToolRisk, make_tool_invocation_envelope
import os


def _envelope(tool_name: str):
    return make_tool_invocation_envelope(
        run_id="run-1",
        request_id="req-1",
        session_id="tenant:s1",
        tenant_id="tenant",
        step_seq=1,
        tool_name=tool_name,
        arguments={},
    )


def test_policy_default_allows_in_log_only_mode():
    decision = RuntimePolicyEvaluator().evaluate(_envelope("file_read"))

    assert decision.action == PolicyAction.ALLOW
    assert decision.allows_execution is True
    assert decision.to_event_payload()["reason"] == "default allow (log-only)"


def test_policy_can_require_approval_by_risk():
    evaluator = RuntimePolicyEvaluator(
        rules=[
            PolicyRule(
                risk=ToolRisk.EXECUTE,
                action=PolicyAction.REQUIRE_APPROVAL,
                reason="execution tools need human approval",
            )
        ],
        policy_version="test-v1",
    )

    decision = evaluator.evaluate(_envelope("code_shell"))

    assert decision.action == PolicyAction.REQUIRE_APPROVAL
    assert decision.allows_execution is False
    assert decision.risk == ToolRisk.EXECUTE
    assert decision.policy_version == "test-v1"
    assert decision.matched_rules == ["rule:0"]


def test_policy_tool_name_rule_takes_priority_order():
    evaluator = RuntimePolicyEvaluator(
        rules=[
            PolicyRule(
                tool_name="git_commit",
                action=PolicyAction.DENY,
                reason="commits disabled",
            ),
            PolicyRule(
                risk=ToolRisk.WRITE,
                action=PolicyAction.REQUIRE_APPROVAL,
                reason="writes need approval",
            ),
        ]
    )

    decision = evaluator.evaluate(_envelope("git_commit"))

    assert decision.action == PolicyAction.DENY
    assert decision.reason == "commits disabled"
    assert decision.matched_rules == ["rule:0"]


def test_policy_deny_does_not_allow_execution():
    evaluator = RuntimePolicyEvaluator(
        rules=[PolicyRule(tool_name="code_shell", action=PolicyAction.DENY, reason="shell disabled")]
    )
    decision = evaluator.evaluate(_envelope("code_shell"))
    assert decision.action == PolicyAction.DENY
    assert decision.allows_execution is False
    assert decision.reason == "shell disabled"
