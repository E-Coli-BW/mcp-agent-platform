"""Tests for ticket backend abstraction."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.ticket_backend import (
    LocalTicketBackend,
    JiraTicketBackend,
    JiraConfig,
    Ticket,
    create_ticket_backend,
)


# ── LocalTicketBackend ────────────────────────────────────────


@pytest.fixture()
def local_backend(tmp_path):
    return LocalTicketBackend(str(tmp_path))


def test_local_create(local_backend):
    ticket = local_backend.create("Server down", "OOM killed", severity="critical")
    assert ticket.id.startswith("INC-")
    assert ticket.title == "Server down"
    assert ticket.severity == "critical"
    assert ticket.url.endswith(".md")


def test_local_create_task(local_backend):
    ticket = local_backend.create("Add monitoring", "dashboards", category="task")
    assert ticket.id.startswith("TASK-")


def test_local_sequential_ids(local_backend):
    t1 = local_backend.create("First", "d1")
    t2 = local_backend.create("Second", "d2")
    # Both INC, same day, sequential
    assert t1.id != t2.id
    seq1 = int(t1.id.split("-")[-1])
    seq2 = int(t2.id.split("-")[-1])
    assert seq2 == seq1 + 1


def test_local_list(local_backend):
    local_backend.create("A", "desc")
    local_backend.create("B", "desc")
    tickets = local_backend.list_tickets()
    assert len(tickets) == 2


def test_local_list_filter_status(local_backend):
    local_backend.create("A", "desc")
    assert len(local_backend.list_tickets(status="Closed")) == 0
    assert len(local_backend.list_tickets(status="Open")) == 1


def test_local_update_status(local_backend):
    ticket = local_backend.create("Fix me", "broken")
    updated = local_backend.update(ticket.id, status="Closed")
    assert updated is not None
    assert updated.status == "Closed"


def test_local_update_resolution(local_backend):
    ticket = local_backend.create("Investigate", "issue")
    local_backend.update(ticket.id, resolution="Fixed by restart")
    # Re-read the file
    import os
    path = os.path.join(local_backend._ticket_dir, f"{ticket.id}.md")
    content = open(path).read()
    assert "Fixed by restart" in content
    assert "_Pending_" not in content


def test_local_update_not_found(local_backend):
    result = local_backend.update("NONEXISTENT-999")
    assert result is None


def test_local_get(local_backend):
    ticket = local_backend.create("Test", "desc")
    fetched = local_backend.get(ticket.id)
    assert fetched is not None
    assert fetched.id == ticket.id


def test_local_get_not_found(local_backend):
    assert local_backend.get("NOPE-999") is None


# ── JiraTicketBackend (mocked) ────────────────────────────────


@pytest.fixture()
def jira_config():
    return JiraConfig(
        base_url="https://test.atlassian.net",
        email="bot@test.com",
        api_token="fake-token",
        project_key="OPS",
    )


@pytest.fixture()
def jira_backend(jira_config):
    return JiraTicketBackend(jira_config)


def test_jira_create(jira_backend):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"key": "OPS-42"}
    mock_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.post.return_value = mock_resp
    jira_backend._session = mock_session

    ticket = jira_backend.create("Server OOM", "Memory at 95%", severity="high")

    assert ticket.id == "OPS-42"
    assert ticket.url == "https://test.atlassian.net/browse/OPS-42"
    assert ticket.severity == "high"


def test_jira_list(jira_backend):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "issues": [
            {
                "key": "OPS-1",
                "fields": {
                    "summary": "Bug A",
                    "status": {"name": "Open"},
                    "priority": {"name": "High"},
                    "labels": ["incident"],
                },
            },
            {
                "key": "OPS-2",
                "fields": {
                    "summary": "Bug B",
                    "status": {"name": "Closed"},
                    "priority": {"name": "Low"},
                    "labels": [],
                },
            },
        ]
    }
    mock_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    jira_backend._session = mock_session

    tickets = jira_backend.list_tickets()

    assert len(tickets) == 2
    assert tickets[0].id == "OPS-1"
    assert tickets[0].url == "https://test.atlassian.net/browse/OPS-1"


def test_jira_get_not_found(jira_backend):
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    jira_backend._session = mock_session

    result = jira_backend.get("OPS-999")

    assert result is None


# ── Factory ───────────────────────────────────────────────────


def test_factory_local(tmp_path):
    backend = create_ticket_backend("local", workspace_root=str(tmp_path))
    assert isinstance(backend, LocalTicketBackend)


def test_factory_jira(monkeypatch):
    monkeypatch.setenv("AGENT_JIRA_URL", "https://test.atlassian.net")
    monkeypatch.setenv("AGENT_JIRA_EMAIL", "bot@test.com")
    monkeypatch.setenv("AGENT_JIRA_TOKEN", "token")
    backend = create_ticket_backend("jira")
    assert isinstance(backend, JiraTicketBackend)


def test_factory_unknown():
    with pytest.raises(ValueError, match="Unknown ticket backend"):
        create_ticket_backend("notion")
