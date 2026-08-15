"""Bounded, observable recovery for one agent model-request step."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, messages_to_dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelFailure:
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None = None


@dataclass(frozen=True)
class ModelRetryPolicy:
    max_retries: int = 2
    initial_delay_ms: int = 500
    max_delay_ms: int = 10_000
    jitter_ratio: float = 0.1
    lease_seconds: int = 120

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 10:
            raise ValueError("model retry max_retries must be between 0 and 10")
        if self.initial_delay_ms < 0:
            raise ValueError("model retry initial_delay_ms must be non-negative")
        if self.max_delay_ms < self.initial_delay_ms:
            raise ValueError("model retry max_delay_ms must be >= initial_delay_ms")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("model retry jitter_ratio must be between 0 and 1")
        if self.lease_seconds < 1:
            raise ValueError("model retry lease_seconds must be positive")


class EmptyModelResponseError(RuntimeError):
    """A provider completed successfully without text, media, or tool calls."""


class ModelRequestFailedError(RuntimeError):
    """A classified model request failed or exhausted its retry budget."""

    def __init__(self, failure: ModelFailure, attempts: int):
        self.failure = failure
        self.attempts = attempts
        self.terminal_reason = (
            "model_retry_exhausted" if failure.retryable else "model_request_failed"
        )
        self.retryable = False
        super().__init__(
            f"{failure.code} after {attempts} attempt(s): {failure.message}"
        )


def policy_from_settings(settings: Any) -> ModelRetryPolicy:
    return ModelRetryPolicy(
        max_retries=int(settings.model_retry_max_retries),
        initial_delay_ms=int(settings.model_retry_initial_delay_ms),
        max_delay_ms=int(settings.model_retry_max_delay_ms),
        jitter_ratio=float(settings.model_retry_jitter_ratio),
        lease_seconds=max(60, int(settings.llm_timeout) + 30),
    )


class ModelRetryLeaseLostError(RuntimeError):
    """Another worker owns the durable retry checkpoint."""

    terminal_reason = "model_retry_lease_lost"
    retryable = True
    preserve_run = True


class ModelRetryFingerprintError(RuntimeError):
    """The resumed graph request does not match the durable retry request."""

    terminal_reason = "model_retry_fingerprint_mismatch"
    retryable = False


@dataclass
class _DurableRetry:
    store: Any
    run_id: str
    owner: str
    request_fingerprint: str
    attempts_failed: int

    @classmethod
    def start(
        cls,
        messages: Sequence[BaseMessage],
        policy: ModelRetryPolicy,
        *,
        context: Any | None = None,
    ) -> _DurableRetry | None:
        from app.config import settings
        from app.context.request_context import get_request_context

        context = context or get_request_context()
        if settings.runtime_store_backend.strip().lower() != "sqlite" or not context.run_id:
            return None

        from app.runtime.checkpoint import get_checkpoint_store

        store = get_checkpoint_store()
        fingerprint = _request_fingerprint(messages, context)
        owner = f"model:{os.getpid()}:{context.run_id}"
        checkpoint = store.get(context.run_id)
        if checkpoint is not None and checkpoint.state == "model_retry_executing":
            if checkpoint.payload.get("kind") != "model_retry":
                raise ModelRetryFingerprintError(
                    "active checkpoint is not a model retry"
                )
            if checkpoint.payload.get("request_fingerprint") != fingerprint:
                raise ModelRetryFingerprintError(
                    "resumed model request does not match its durable checkpoint"
                )
            if (
                checkpoint.lease_owner is None
                or checkpoint.lease_expires_at is None
                or checkpoint.lease_expires_at < time.time()
            ):
                raise ModelRetryLeaseLostError(
                    "model retry must be claimed before graph resume"
                )
            return cls(
                store=store,
                run_id=context.run_id,
                owner=owner,
                request_fingerprint=fingerprint,
                attempts_failed=int(checkpoint.payload.get("attempts_failed", 0)),
            )

        payload = {
            "kind": "model_retry",
            "session_id": context.session_id,
            "agent_id": context.agent_id,
            "prompt_version": context.prompt_version,
            "request_fingerprint": fingerprint,
            "attempts_failed": 0,
            "max_retries": policy.max_retries,
            "resume_at": 0,
        }
        checkpoint = store.save_next(
            run_id=context.run_id,
            state="model_retry_executing",
            tool_name="__model__",
            payload=payload,
        )
        claimed = store.claim(
            context.run_id,
            owner=owner,
            lease_seconds=policy.lease_seconds,
            expected_state="model_retry_executing",
        )
        if checkpoint is None or claimed is None:
            raise ModelRetryLeaseLostError(
                "could not claim the new model retry checkpoint"
            )
        return cls(store, context.run_id, owner, fingerprint, 0)

    def schedule(self, failure: ModelFailure, delay_ms: int, attempts_failed: int) -> None:
        checkpoint = self.store.transition(
            self.run_id,
            state="model_retry_waiting",
            payload={
                "attempts_failed": attempts_failed,
                "resume_at": time.time() + delay_ms / 1000,
                "failure": {
                    "code": failure.code,
                    "message": failure.message[:1000],
                    "retryable": failure.retryable,
                    "retry_after_ms": failure.retry_after_ms,
                },
            },
        )
        if checkpoint is None:
            raise ModelRetryLeaseLostError("model retry checkpoint disappeared")
        recovery_delay_seconds = max(1, math.ceil(delay_ms / 1000) + 5)
        if self.store.renew_lease(
            self.run_id,
            owner=self.owner,
            lease_seconds=recovery_delay_seconds,
        ) is None:
            raise ModelRetryLeaseLostError(
                "model retry lease expired before backoff was scheduled"
            )
        self.attempts_failed = attempts_failed

    def claim_next(self, policy: ModelRetryPolicy) -> None:
        claimed = self.store.renew_lease(
            self.run_id,
            owner=self.owner,
            lease_seconds=policy.lease_seconds,
        )
        if claimed is None:
            raise ModelRetryLeaseLostError(
                "another worker owns the model retry checkpoint"
            )
        checkpoint = self.store.transition(
            self.run_id,
            state="model_retry_executing",
        )
        if checkpoint is None:
            raise ModelRetryLeaseLostError("model retry checkpoint disappeared")

    def complete(self) -> None:
        checkpoint = self.store.transition(
            self.run_id,
            state="completed",
            payload={"completed_at": time.time()},
            clear_lease=True,
        )
        if checkpoint is None:
            raise ModelRetryLeaseLostError("model retry checkpoint disappeared")

    def fail(self, failure: ModelFailure, attempts: int) -> None:
        self.store.transition(
            self.run_id,
            state="failed",
            payload={
                "attempts_failed": attempts,
                "failure": {
                    "code": failure.code,
                    "message": failure.message[:1000],
                    "retryable": failure.retryable,
                },
            },
            clear_lease=True,
        )


def _request_fingerprint(messages: Sequence[BaseMessage], context: Any) -> str:
    payload = {
        "session_id": context.session_id,
        "agent_id": context.agent_id,
        "prompt_version": context.prompt_version,
        "messages": messages_to_dict(messages),
        "request_identity": getattr(context, "request_identity", None),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _status_code(error: Exception) -> int | None:
    direct = getattr(error, "status_code", None)
    if isinstance(direct, int):
        return direct
    response = getattr(error, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _retry_after_ms(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0, int(float(raw) * 1000))
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(raw).timestamp() - __import__("time").time()
            return max(0, int(delay * 1000))
        except (TypeError, ValueError, OverflowError):
            return None


def classify_model_error(error: Exception) -> ModelFailure:
    if isinstance(error, EmptyModelResponseError):
        return ModelFailure("EMPTY_RESPONSE", str(error), True)

    status = _status_code(error)
    message = str(error).strip() or type(error).__name__
    retry_after_ms = _retry_after_ms(error)
    if status in (401, 403):
        return ModelFailure("AUTH", message, False)
    if status == 429:
        return ModelFailure("RATE_LIMIT", message, True, retry_after_ms)
    if status is not None and 500 <= status < 600:
        return ModelFailure("SERVER", message, True, retry_after_ms)
    if status is not None and 400 <= status < 500:
        return ModelFailure("INVALID_REQUEST", message, False)

    class_name = type(error).__name__.lower()
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or "timeout" in class_name:
        return ModelFailure("TIMEOUT", message, True)
    if isinstance(error, ConnectionError) or any(
        marker in class_name for marker in ("connection", "connecterror", "transport")
    ):
        return ModelFailure("TRANSPORT", message, True)
    if "ratelimit" in class_name:
        return ModelFailure("RATE_LIMIT", message, True, retry_after_ms)
    if "internalserver" in class_name or "servererror" in class_name:
        return ModelFailure("SERVER", message, True, retry_after_ms)
    return ModelFailure("UNKNOWN", message, False)


def _has_meaningful_content(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return content is not None
    for block in content:
        if isinstance(block, str) and block.strip():
            return True
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return True
            if block.get("type") not in (None, "text"):
                return True
        elif block is not None:
            return True
    return False


def validate_model_response(response: AIMessage) -> AIMessage:
    if response.tool_calls or _has_meaningful_content(response.content):
        return response
    raise EmptyModelResponseError(
        "model returned a completed response with no content or tool calls"
    )


async def _record(event_type: str, payload: dict[str, Any]) -> None:
    try:
        from app.context.request_context import get_request_context
        from app.runtime import get_run_recorder

        run_id = get_request_context().run_id
        if run_id:
            await get_run_recorder().record_event(run_id, event_type, payload)
    except Exception as error:  # noqa: BLE001 - observability must not break recovery
        logger.warning("Failed to record %s: %s", event_type, error)


def _delay_ms(
    failure: ModelFailure,
    policy: ModelRetryPolicy,
    retry: int,
    random_sample: Callable[[], float],
) -> int:
    if failure.retry_after_ms is not None:
        return min(failure.retry_after_ms, policy.max_delay_ms)
    exponential = min(
        policy.initial_delay_ms * (2 ** max(0, retry - 1)),
        policy.max_delay_ms,
    )
    jitter = 1 - policy.jitter_ratio + 2 * policy.jitter_ratio * random_sample()
    return max(0, int(exponential * jitter))


async def invoke_with_model_retry(
    model: BaseChatModel,
    messages: Sequence[BaseMessage],
    *,
    policy: ModelRetryPolicy,
    request_identity: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_sample: Callable[[], float] = random.random,
) -> AIMessage:
    from app.context.request_context import get_request_context

    context = get_request_context()
    fingerprint_context = type(
        "FingerprintContext",
        (),
        {**context.__dict__, "request_identity": request_identity},
    )()
    durable = _DurableRetry.start(messages, policy, context=fingerprint_context)
    attempts_failed = durable.attempts_failed if durable is not None else 0
    for attempt in range(attempts_failed, policy.max_retries + 1):
        started_at = time.monotonic()
        try:
            response = await model.ainvoke(messages)
            if not isinstance(response, AIMessage):
                raise TypeError(f"chat model returned {type(response).__name__}, expected AIMessage")
            validated = validate_model_response(response)
            if durable is not None:
                durable.complete()
            return validated
        except Exception as error:
            failure = classify_model_error(error)
            await _record(
                "model_attempt_failed",
                {
                    "attempt": attempt + 1,
                    "code": failure.code,
                    "message": failure.message[:1000],
                    "retryable": failure.retryable,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                },
            )
            if not failure.retryable or attempt >= policy.max_retries:
                if durable is not None:
                    durable.fail(failure, attempt + 1)
                raise ModelRequestFailedError(failure, attempt + 1) from error

            retry = attempt + 1
            delay_ms = _delay_ms(failure, policy, retry, random_sample)
            await _record(
                "model_retry_scheduled",
                {
                    "retry": retry,
                    "max_retries": policy.max_retries,
                    "delay_ms": delay_ms,
                    "code": failure.code,
                },
            )
            if durable is not None:
                durable.schedule(failure, delay_ms, attempt + 1)
            await sleep(delay_ms / 1000)
            if durable is not None:
                durable.claim_next(policy)
            await _record(
                "model_retry_started",
                {"retry": retry, "code": failure.code},
            )

    raise AssertionError("model retry loop exhausted without returning or raising")