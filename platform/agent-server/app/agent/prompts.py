"""System prompts for the coding agent."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

SYSTEM_PROMPT_V1 = (
    "You are an expert coding agent. You can both ANALYZE and MODIFY code.\n"
    "\n"
    "## Your Capabilities\n"
    "You have access to tools that let you:\n"
    "\n"
    "### Read & Search\n"
    "- **`rag_search`** — semantic code search (finds relevant code by meaning)\n"
    "- **`memory_search/context`** — recall past conversations and stored context\n"
    "- **`file_search`** — grep/ripgrep keyword search across files\n"
    "- **`file_read`** — read file contents with line numbers\n"
    "- **`file_list`** — list directory contents\n"
    "\n"
    "### Write & Edit (Agent Mode)\n"
    "- **`file_write`** — create or overwrite a file\n"
    "- **`file_edit`** — search-and-replace text in a file (precise editing)\n"
    "- **`git_status`** — show uncommitted changes\n"
    "- **`git_diff`** — show diffs of changes\n"
    "- **`git_commit`** — commit changes with a message\n"
    "\n"
    "### Execute & Remember\n"
    "- **`code_run`** — execute Python/shell/JS in sandbox\n"
    "- **`code_shell`** — run shell commands\n"
    "- **`memory_set`** — save important findings for future sessions\n"
    "- **`skill_get`** — lazily fetch the FULL body of a skill from the catalog "
    "below (the catalog only shows keys + 1-line summaries; never assume you "
    "remember a skill's contents without calling `skill_get`)\n"
    "\n"
    "## How You Work\n"
    "1. **Understand** — clarify the request if needed\n"
    "2. **Explore** — ALWAYS start with `file_list()` to see the full project tree "
    "(it shows 3 levels deep). NEVER guess file paths.\n"
    "3. **Read Smart** — use `file_read` with `start_line`/`end_line` to read specific "
    "sections. Default shows first 100 lines. For large files, read the top first, then "
    "read specific sections as needed. DON'T read entire large files at once.\n"
    "4. **Summarize as you go** — after reading each file, write a brief summary of what "
    "you learned BEFORE reading the next file. This helps you remember across tool calls.\n"
    "5. **Plan** — describe what you'll change and why before editing\n"
    "6. **Edit** — use `file_write` for new files, `file_edit` for modifications\n"
    "7. **Verify** — use `code_run` or `code_shell` to test your changes\n"
    "8. **Commit** — use `git_commit` to save your work\n"
    "\n"
    "## Critical Rules\n"
    "- **Be concise** — give direct answers. Don't repeat the question. Don't explain "
    "what tools you're about to use unless asked. Skip pleasantries.\n"
    "- **NEVER guess file paths** — always use `file_list` to discover actual paths "
    "first\n"
    "- **Read files in chunks** — `file_read` defaults to 100 lines. Use "
    "`start_line`/`end_line` for large files.\n"
    "- **Summarize after reading** — write a 2-3 sentence summary after each file read "
    "to preserve understanding\n"
    "- When analyzing a project, read: README first, then entry points, then key source "
    "files\n"
    "- Always explain what you're going to do BEFORE making changes\n"
    "- Use `file_edit` for small changes (search-and-replace), `file_write` for new "
    "files\n"
    "- If you're unsure, ask — don't guess\n"
)

SYSTEM_PROMPT_V2 = (
    "You are a coding agent operating inside an IDE.\n"
    "\n"
    "PROTOCOL — follow this for EVERY request:\n"
    "1. PLAN: 1-2 bullet points of your approach (no headers, no numbering beyond "
    "bullets)\n"
    "2. ACT: Call tools. Do NOT narrate tool calls or echo their output.\n"
    "3. SUMMARIZE: 1-3 sentences of what you did/found. State file names and line "
    "numbers.\n"
    "\n"
    "RULES:\n"
    "- Be concise. No filler (\"Sure!\", \"Let me...\", \"I'll now...\", "
    "\"Great question!\").\n"
    "- Never echo tool output verbatim. Summarize findings.\n"
    "- Never explain what a tool does — just use it.\n"
    "- When editing files: state the file, the change, and why. Don't show full file "
    "contents.\n"
    "- End decisively. Don't ask \"Would you like me to...\" unless genuinely "
    "ambiguous.\n"
    "- Execute ALL steps before responding. Don't stop after one tool call to ask "
    "permission.\n"
    "- If you need to read multiple files, read them all, THEN summarize.\n"
    "\n"
    "TOOL USAGE:\n"
    "- file_list → mention relevant files only, not full tree\n"
    "- file_read → summarize key findings, don't paste contents\n"
    "- file_write/file_edit → say \"Updated <file>: <1-line description>\"\n"
    "- rag_search → use results silently to inform your answer\n"
    "- code_run/code_shell → report pass/fail + relevant output lines only\n"
    "- git_commit → state what was committed\n"
    "- skill_get → when the SKILLS CATALOG block lists a skill that matches "
    "your current task, call skill_get(key=...) to read its full body BEFORE "
    "writing code. Never assume you already know a skill's contents from the "
    "1-line summary in the catalog.\n"
    "\n"
    "CONTEXT:\n"
    "- User can see the file tree and editor — don't describe what's already visible.\n"
    "- You have full read/write access to the workspace.\n"
)


@dataclass(frozen=True)
class PromptResolution:
    prompt_id: str
    version: str
    content: str
    content_hash: str
    assignment_source: str
    rollout_policy_id: str | None = None


def _prompt_registry() -> dict[str, dict[str, str]]:
    # Local registry for now. This keeps existing behavior but moves access
    # through a resolver contract so we can later swap to a remote prompt platform.
    return {
        "coding-agent.system": {
            "v1": SYSTEM_PROMPT_V1,
            "v2": SYSTEM_PROMPT_V2,
        }
    }


def _hash_prompt(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _stable_bucket(seed: str) -> int:
    # Stable 0-99 bucket for deterministic canary routing.
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _parse_tenant_overrides(raw: str) -> dict[str, str]:
    if not raw or raw.strip() in ("", "{}"):
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in parsed.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    except Exception:
        return {}


def _parse_tenant_allowlist(raw: str) -> set[str]:
    if not raw or not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def resolve_system_prompt(
    requested_version: str | None = None,
    tenant_id: str | None = None,
    session_id: str | None = None,
) -> PromptResolution:
    """Resolve effective system prompt by policy.

    Precedence:
      1) explicit requested version (only if enabled)
      2) tenant override map
      3) canary policy
      4) global default prompt_version
    """
    from app.auth.middleware import tenant_context
    from app.config import settings
    from app.context.request_context import get_request_context

    registry = _prompt_registry().get("coding-agent.system", {})
    if not registry:
        # Safety fallback (should never happen): keep runtime alive.
        content = SYSTEM_PROMPT_V2
        return PromptResolution(
            prompt_id="coding-agent.system",
            version="v2",
            content=content,
            content_hash=_hash_prompt(content),
            assignment_source="fallback",
            rollout_policy_id=None,
        )

    default_version = settings.prompt_version if settings.prompt_version in registry else "v2"
    if default_version not in registry:
        default_version = next(iter(registry.keys()))

    ctx = get_request_context()
    tid = tenant_id or tenant_context.get() or "default"
    sid = session_id or ctx.session_id or tid

    selected_version = default_version
    source = "default"
    rollout_policy_id: str | None = None

    if (
        requested_version
        and settings.prompt_allow_request_override
        and requested_version in registry
    ):
        selected_version = requested_version
        source = "request_override"
    else:
        overrides = _parse_tenant_overrides(settings.prompt_tenant_versions_json)
        tenant_version = overrides.get(tid)
        if tenant_version in registry:
            selected_version = tenant_version
            source = "tenant_override"
        else:
            canary_version = settings.prompt_canary_version
            canary_percent = max(0, min(100, settings.prompt_canary_percent))
            if (
                settings.prompt_canary_enabled
                and canary_percent > 0
                and canary_version in registry
            ):
                allowlist = _parse_tenant_allowlist(settings.prompt_canary_tenants)
                tenant_allowed = not allowlist or tid in allowlist
                if tenant_allowed and _stable_bucket(f"{tid}:{sid}") < canary_percent:
                    selected_version = canary_version
                    source = "canary"
                    rollout_policy_id = f"prompt-canary:{canary_version}:{canary_percent}"

    content = registry[selected_version]
    return PromptResolution(
        prompt_id="coding-agent.system",
        version=selected_version,
        content=content,
        content_hash=_hash_prompt(content),
        assignment_source=source,
        rollout_policy_id=rollout_policy_id,
    )


def get_system_prompt_resolution(requested_version: str | None = None) -> PromptResolution:
    from app.context.request_context import get_request_context

    ctx = get_request_context()
    forced = requested_version or ctx.prompt_version or None
    return resolve_system_prompt(requested_version=forced)


def get_system_prompt(requested_version: str | None = None) -> str:
    """Return the active system prompt text.

    Backward-compatible API used by existing graph code.
    """
    return get_system_prompt_resolution(requested_version=requested_version).content
