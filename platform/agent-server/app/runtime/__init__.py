"""Runtime event recording primitives.

Phase 1 keeps this deliberately small: an in-memory recorder that mirrors
the current chat/graph execution without changing behavior. Later phases can
swap the recorder implementation for Postgres/Redis Streams while keeping the
same event contract.
"""

from app.runtime.recorder import (
    InMemoryRunRecorder,
    RunRecorder,
    RunEvent,
    RunStore,
    RunStatus,
    StoreBackedRunRecorder,
    build_run_recorder,
    get_run_recorder,
    reset_run_recorder_for_tests,
)
from app.runtime.store import RuntimeStore, SQLiteRuntimeStore
from app.runtime.idempotency import (
    IdempotencyClaim,
    IdempotencyDecision,
    IdempotencyRecord,
    IdempotencyStatus,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    get_idempotency_store,
    reset_idempotency_store_for_tests,
)
from app.runtime.policy import (
    PolicyAction,
    PolicyDecision,
    PolicyRule,
    RuntimePolicyEvaluator,
    get_policy_evaluator,
)
from app.runtime.tool_invocation import (
    ToolInvocationEnvelope,
    ToolRisk,
    classify_tool_risk,
    make_tool_invocation_envelope,
)

__all__ = [
    "IdempotencyClaim",
    "IdempotencyDecision",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "IdempotencyStore",
    "InMemoryRunRecorder",
    "InMemoryIdempotencyStore",
    "PolicyAction",
    "PolicyDecision",
    "PolicyRule",
    "RunEvent",
    "RunRecorder",
    "RunStore",
    "RunStatus",
    "RuntimeStore",
    "RuntimePolicyEvaluator",
    "SQLiteRuntimeStore",
    "StoreBackedRunRecorder",
    "build_run_recorder",
    "ToolInvocationEnvelope",
    "ToolRisk",
    "classify_tool_risk",
    "get_idempotency_store",
    "get_policy_evaluator",
    "get_run_recorder",
    "make_tool_invocation_envelope",
    "reset_idempotency_store_for_tests",
    "reset_run_recorder_for_tests",
]
