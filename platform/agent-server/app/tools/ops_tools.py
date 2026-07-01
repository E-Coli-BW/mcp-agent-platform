"""Ops tools — ticket generation, PR creation, and git branching for daily operations.

Ticket backend is pluggable: local markdown files (default) or Jira REST API.
Switch via config: AGENT_TICKET_BACKEND=local|jira

Git/PR tools always operate locally.
"""

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool

from app.tools.agent_mode import get_workspace_root
from app.tools.ticket_backend import TicketBackend, create_ticket_backend

# ── Lazy-init ticket backend ─────────────────────────────────

_ticket_backend: TicketBackend | None = None


def _get_ticket_backend() -> TicketBackend:
    global _ticket_backend
    if _ticket_backend is None:
        from app.config import settings
        _ticket_backend = create_ticket_backend(
            backend_type=settings.ticket_backend,
            jira_url=settings.jira_url,
            jira_email=settings.jira_email,
            jira_token=settings.jira_token,
            jira_project=settings.jira_project,
            jira_issue_type=settings.jira_issue_type,
        )
    return _ticket_backend


def reset_ticket_backend(backend: TicketBackend | None = None) -> None:
    """Reset the ticket backend — for testing or runtime reconfiguration."""
    global _ticket_backend
    _ticket_backend = backend


# ── Git Branch Tools ──────────────────────────────────────────


@tool
def git_branch(branch_name: str, base_branch: str | None = None) -> str:
    """Create a new git branch and switch to it. Optionally specify a base branch."""
    try:
        root = get_workspace_root()
        if base_branch:
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name, base_branch],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
        else:
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
        if result.returncode == 0:
            return f"✅ Created and switched to branch: {branch_name}"
        if "already exists" in result.stderr:
            # Switch to existing branch
            switch = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=root, capture_output=True, text=True, timeout=10,
            )
            if switch.returncode == 0:
                return f"ℹ️ Branch '{branch_name}' already exists — switched to it."
            return f"❌ Branch exists and switch failed: {switch.stderr}"
        return f"❌ git branch failed: {result.stderr}"
    except Exception as e:
        return f"❌ git branch failed: {e}"


@tool
def git_log(max_count: int = 10) -> str:
    """Show recent git commits (one-line format)."""
    try:
        root = get_workspace_root()
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{max_count}"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return f"❌ git log failed: {result.stderr}"
        output = result.stdout.strip()
        return f"📋 Recent commits:\n```\n{output}\n```" if output else "No commits yet."
    except Exception as e:
        return f"❌ git log failed: {e}"


# ── PR Description Generator ─────────────────────────────────


@tool
def pr_create(
    title: str,
    description: str,
    branch_name: str | None = None,
    labels: str | None = None,
) -> str:
    """Generate a local PR description file. Creates .github/pull_requests/<branch>.md.
    Does NOT push to GitHub — just prepares the PR metadata locally."""
    try:
        root = get_workspace_root()

        # Get current branch if not specified
        if not branch_name:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=5,
            )
            branch_name = result.stdout.strip() if result.returncode == 0 else "unknown"

        # Get diff stats
        diff_result = subprocess.run(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        commits = diff_result.stdout.strip() if diff_result.returncode == 0 else "(unable to determine)"

        # Get changed files
        files_result = subprocess.run(
            ["git", "diff", "--name-only", "main...HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
        changed_files = files_result.stdout.strip() if files_result.returncode == 0 else ""

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        label_line = f"**Labels:** {labels}" if labels else ""

        pr_content = f"""# {title}

**Branch:** `{branch_name}`
**Created:** {now}
{label_line}

## Description

{description}

## Commits

```
{commits}
```

## Changed Files

```
{changed_files}
```

---
*Generated by MCP Agent — review before submitting.*
"""
        # Write to .github/pull_requests/
        pr_dir = os.path.join(root, ".github", "pull_requests")
        os.makedirs(pr_dir, exist_ok=True)
        safe_name = branch_name.replace("/", "-").replace(" ", "-")
        pr_path = os.path.join(pr_dir, f"{safe_name}.md")
        with open(pr_path, "w") as f:
            f.write(pr_content)

        return f"✅ PR description written to .github/pull_requests/{safe_name}.md"
    except Exception as e:
        return f"❌ PR creation failed: {e}"


# ── Ticket Tools (delegated to backend) ───────────────────────


@tool
def ticket_create(
    title: str,
    description: str,
    severity: str = "medium",
    category: str = "incident",
    assignee: str | None = None,
    related_files: str | None = None,
) -> str:
    """Create an incident/task ticket. Backend is configurable (local files or Jira)."""
    try:
        backend = _get_ticket_backend()
        ticket = backend.create(
            title=title,
            description=description,
            severity=severity,
            category=category,
            assignee=assignee,
            related_files=related_files,
        )
        return f"✅ Ticket created: {ticket.id} → {ticket.url}"
    except Exception as e:
        return f"❌ Ticket creation failed: {e}"


@tool
def ticket_list(status: str | None = None) -> str:
    """List tickets. Optionally filter by status (Open/Closed)."""
    try:
        backend = _get_ticket_backend()
        tickets = backend.list_tickets(status=status)
        if not tickets:
            msg = f"📋 No tickets with status '{status}'." if status else "📋 No tickets found."
            return msg
        lines = [f"- [{t.status}] {t.title} ({t.id})" for t in tickets]
        return "📋 Tickets:\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list tickets: {e}"


@tool
def ticket_update(ticket_id: str, status: str | None = None, resolution: str | None = None) -> str:
    """Update a ticket's status or resolution."""
    try:
        backend = _get_ticket_backend()
        ticket = backend.update(ticket_id, status=status, resolution=resolution)
        if ticket is None:
            return f"❌ Ticket not found: {ticket_id}"
        updates = []
        if status:
            updates.append(f"status → {status}")
        if resolution:
            updates.append("resolution updated")
        return f"✅ Updated {ticket.id}: {', '.join(updates)}"
    except Exception as e:
        return f"❌ Failed to update ticket: {e}"
