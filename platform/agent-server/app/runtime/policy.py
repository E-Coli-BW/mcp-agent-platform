"""Runtime policy decision contract.

This first slice is deliberately small and log-only. It lets us attach a
queryable policy decision to tool events before we start enforcing approvals
or denials in the execution path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os

from app.runtime.tool_invocation import ToolInvocationEnvelope, ToolRisk


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    REDACT = "redact"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class PolicyRule:
    tool_name: str | None = None
    risk: ToolRisk | None = None
    action: PolicyAction = PolicyAction.ALLOW
    reason: str = ""

    def matches(self, envelope: ToolInvocationEnvelope) -> bool:
        if self.tool_name is not None and self.tool_name != envelope.tool_name:
            return False
        if self.risk is not None and self.risk != envelope.risk:
            return False
        return True


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    risk: ToolRisk
    policy_version: str = "runtime-default-v1"
    matched_rules: list[str] = field(default_factory=list)

    @property
    def allows_execution(self) -> bool:
        return self.action == PolicyAction.ALLOW

    def to_event_payload(self) -> dict:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "policy_version": self.policy_version,
            "matched_rules": list(self.matched_rules),
        }


class RuntimePolicyEvaluator:
    """Evaluate tool invocation policy in priority order."""

    def __init__(
        self,
        *,
        rules: list[PolicyRule] | None = None,
        policy_version: str = "runtime-default-v1",
    ):
        if rules is None:
            forced = {
                name.strip() for name in os.environ.get("AGENT_TEST_REQUIRE_APPROVAL_TOOLS", "").split(",")
                if name.strip()
            }
            rules = [PolicyRule(tool_name=name, action=PolicyAction.REQUIRE_APPROVAL, reason="test approval gate") for name in forced]
        self._rules = rules
        self._policy_version = policy_version

    def evaluate(self, envelope: ToolInvocationEnvelope) -> PolicyDecision:
        for idx, rule in enumerate(self._rules):
            if rule.matches(envelope):
                return PolicyDecision(
                    action=rule.action,
                    reason=rule.reason or "matched runtime policy rule",
                    risk=envelope.risk,
                    policy_version=self._policy_version,
                    matched_rules=[f"rule:{idx}"],
                )

        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason="default allow (log-only)",
            risk=envelope.risk,
            policy_version=self._policy_version,
            matched_rules=[],
        )


_evaluator = RuntimePolicyEvaluator()


def get_policy_evaluator() -> RuntimePolicyEvaluator:
    return _evaluator
