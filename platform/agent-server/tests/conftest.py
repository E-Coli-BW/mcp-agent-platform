"""Shared isolation fixtures for process-global test state."""

import pytest

from app.auth.middleware import active_auth_context, tenant_context
from app.context.request_context import (
    set_agent_contract,
    set_prompt_version,
    set_request_context,
)


@pytest.fixture(autouse=True)
def reset_request_contextvars():
    """Prevent one test's request context from affecting another test."""
    tenant_token = tenant_context.set("default")
    auth_token = active_auth_context.set(None)
    set_request_context("", "", "")
    set_prompt_version("")
    set_agent_contract("", "")
    try:
        yield
    finally:
        active_auth_context.reset(auth_token)
        tenant_context.reset(tenant_token)
        set_request_context("", "", "")
        set_prompt_version("")
        set_agent_contract("", "")