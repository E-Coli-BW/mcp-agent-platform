"""Tests for ops tools — ticket, PR, and git branch operations."""

import os
import subprocess

import pytest

from app.tools.agent_mode import set_workspace_root, get_workspace_root
from app.tools.ops_tools import (
    git_branch,
    git_log,
    pr_create,
    ticket_create,
    ticket_list,
    ticket_update,
    reset_ticket_backend,
)
from app.tools.ticket_backend import LocalTicketBackend, Ticket


@pytest.fixture()
def workspace(tmp_path):
    """Create a temp workspace with git initialized."""
    old_root = get_workspace_root()
    set_workspace_root(str(tmp_path))
    # Init git repo with initial commit
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)
    # Create initial commit on main
    (tmp_path / "README.md").write_text("# Test")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(tmp_path), capture_output=True)
    yield tmp_path
    reset_ticket_backend()
    set_workspace_root(old_root)


# ── git_branch ────────────────────────────────────────────────


def test_git_branch_creates_branch(workspace):
    result = git_branch.invoke({"branch_name": "fix/test-123"})
    assert "✅" in result
    assert "fix/test-123" in result


def test_git_branch_existing_branch(workspace):
    git_branch.invoke({"branch_name": "feat/a"})
    # Switch back to main
    subprocess.run(["git", "checkout", "main"], cwd=str(workspace), capture_output=True)
    result = git_branch.invoke({"branch_name": "feat/a"})
    assert "already exists" in result or "switched" in result.lower()


# ── git_log ───────────────────────────────────────────────────


def test_git_log_shows_commits(workspace):
    result = git_log.invoke({"max_count": 5})
    assert "init" in result


# ── ticket_create ─────────────────────────────────────────────


def test_ticket_create_writes_file(workspace):
    result = ticket_create.invoke({
        "title": "Server OOM",
        "description": "Memory usage spiked to 95%",
        "severity": "high",
        "category": "incident",
    })
    assert "✅" in result
    assert "INC-" in result

    # Verify file exists
    ticket_dir = workspace / ".ops" / "tickets"
    tickets = list(ticket_dir.glob("INC-*.md"))
    assert len(tickets) == 1

    content = tickets[0].read_text()
    assert "Server OOM" in content
    assert "high" in content
    assert "Memory usage spiked" in content


def test_ticket_create_sequential_ids(workspace):
    ticket_create.invoke({"title": "First", "description": "d1"})
    ticket_create.invoke({"title": "Second", "description": "d2"})

    ticket_dir = workspace / ".ops" / "tickets"
    tickets = sorted(ticket_dir.glob("TASK-*.md"))
    # Both are "task" by default (category defaults to "incident", but let's test incident)
    tickets_inc = sorted(ticket_dir.glob("INC-*.md"))
    # Default category is incident
    assert len(tickets_inc) == 2


def test_ticket_create_task_category(workspace):
    result = ticket_create.invoke({
        "title": "Add monitoring",
        "description": "Set up dashboards",
        "category": "task",
    })
    assert "TASK-" in result


# ── ticket_list ───────────────────────────────────────────────


def test_ticket_list_empty(workspace):
    result = ticket_list.invoke({})
    assert "No tickets" in result


def test_ticket_list_shows_tickets(workspace):
    ticket_create.invoke({"title": "Bug A", "description": "desc"})
    ticket_create.invoke({"title": "Bug B", "description": "desc"})
    result = ticket_list.invoke({})
    assert "Bug A" in result
    assert "Bug B" in result


def test_ticket_list_filter_status(workspace):
    ticket_create.invoke({"title": "Open bug", "description": "d"})
    result = ticket_list.invoke({"status": "Closed"})
    assert "No tickets" in result


# ── ticket_update ─────────────────────────────────────────────


def test_ticket_update_status(workspace):
    create_result = ticket_create.invoke({"title": "Fix me", "description": "broken"})
    # Extract ticket ID
    ticket_id = create_result.split(":")[1].strip().split(" ")[0]

    result = ticket_update.invoke({"ticket_id": ticket_id, "status": "Closed"})
    assert "✅" in result

    # Verify content changed
    ticket_path = workspace / ".ops" / "tickets" / f"{ticket_id}.md"
    content = ticket_path.read_text()
    assert "Closed" in content


def test_ticket_update_resolution(workspace):
    create_result = ticket_create.invoke({"title": "Investigate", "description": "issue"})
    ticket_id = create_result.split(":")[1].strip().split(" ")[0]

    result = ticket_update.invoke({
        "ticket_id": ticket_id,
        "resolution": "Root cause: connection pool exhaustion. Fixed by increasing pool size.",
    })
    assert "✅" in result
    content = (workspace / ".ops" / "tickets" / f"{ticket_id}.md").read_text()
    assert "connection pool exhaustion" in content


def test_ticket_update_not_found(workspace):
    result = ticket_update.invoke({"ticket_id": "INC-99999999-999"})
    assert "❌" in result


# ── pr_create ─────────────────────────────────────────────────


def test_pr_create_writes_file(workspace):
    # Create a branch with changes
    subprocess.run(["git", "checkout", "-b", "fix/oom-123"], cwd=str(workspace), capture_output=True)
    (workspace / "fix.py").write_text("# fix")
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix: oom"], cwd=str(workspace), capture_output=True)

    result = pr_create.invoke({
        "title": "Fix OOM in worker",
        "description": "Increased heap size and added GC tuning",
        "labels": "bug,urgent",
    })
    assert "✅" in result

    pr_dir = workspace / ".github" / "pull_requests"
    prs = list(pr_dir.glob("*.md"))
    assert len(prs) == 1

    content = prs[0].read_text()
    assert "Fix OOM in worker" in content
    assert "fix/oom-123" in content or "fix-oom-123" in content
    assert "bug,urgent" in content


def test_pr_create_custom_branch(workspace):
    result = pr_create.invoke({
        "title": "Feature X",
        "description": "New feature",
        "branch_name": "feat/x",
    })
    assert "✅" in result
    assert (workspace / ".github" / "pull_requests" / "feat-x.md").exists()
