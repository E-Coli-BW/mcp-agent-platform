"""Tool definitions — local filesystem tools + remote MCP backends."""

import os
import subprocess
from langchain_core.tools import tool
from app.tools.mcp_client import McpToolClient
from app.tools.agent_mode import get_workspace_root
from app.tools.workspace_resolver import WorkspacePathError, validate_path
from app.config import settings
from app.auth.middleware import tenant_context

# Lazy-init clients
_memory_client: McpToolClient | None = None
_filesearch_client: McpToolClient | None = None
_codeexec_client: McpToolClient | None = None
_auth_client = None  # AuthServiceClient singleton


def _get_auth_client():
    """Get or create the AuthServiceClient singleton."""
    global _auth_client
    if _auth_client is None:
        auth_url = getattr(settings, 'auth_service_url', None) or os.environ.get('AUTH_SERVICE_URL')
        if auth_url:
            from app.auth.auth_client import AuthServiceClient
            client_id = os.environ.get('AUTH_CLIENT_ID', 'agent-server')
            client_secret = os.environ.get('AUTH_CLIENT_SECRET', 'agent-secret')
            _auth_client = AuthServiceClient(auth_url, client_id, client_secret)
        else:
            _auth_client = False  # sentinel: no auth service configured
    return _auth_client if _auth_client is not False else None


def _make_client(base_url: str, audience: str = "mcp-platform") -> McpToolClient:
    """Create an McpToolClient with auth service + legacy fallback."""
    return McpToolClient(
        base_url,
        auth_client=_get_auth_client(),
        jwt_secret=settings.jwt_secret,
        audience=audience,
    )


def _get_memory() -> McpToolClient:
    global _memory_client
    if not _memory_client:
        _memory_client = _make_client(settings.memory_server_url, audience="memory-server")
    return _memory_client


def _get_filesearch() -> McpToolClient:
    global _filesearch_client
    if not _filesearch_client:
        _filesearch_client = _make_client(settings.filesearch_server_url, audience="filesearch-server")
    return _filesearch_client


def _get_codeexec() -> McpToolClient:
    global _codeexec_client
    if not _codeexec_client:
        _codeexec_client = _make_client(settings.codeexec_server_url, audience="codeexec-server")
    return _codeexec_client


# ── Memory Tools ──────────────────────────────────────────────

@tool
async def memory_search(query: str, namespace: str | None = None) -> str:
    """Search memories for relevant context. Use this to recall past conversations, decisions, or stored knowledge."""
    args = {"query": query}
    if namespace:
        args["namespace"] = namespace
    return await _get_memory().call_tool("memory_search", args, tenant_id=tenant_context.get())


@tool
async def memory_set(key: str, content: str, tags: list[str] | None = None) -> str:
    """Save important information to persistent memory. Use this to remember facts, decisions, or context for future sessions."""
    args = {"key": key, "content": content}
    if tags:
        args["tags"] = tags
    return await _get_memory().call_tool("memory_set", args, tenant_id=tenant_context.get())


@tool
async def memory_context() -> str:
    """Get an overview of what's stored in memory. Use at the start of a session."""
    return await _get_memory().call_tool("memory_context", {}, tenant_id=tenant_context.get())


@tool
async def skill_get(key: str) -> str:
    """Fetch the FULL body of a saved skill by key.

    Skills are reusable workflows extracted from past sessions. The system prompt
    only shows their keys + 1-line summaries (the catalog). When you need to
    apply a skill, call this to read its detailed steps.

    Example: skill_get(key="skill-maven-stale-jar-fix")
    """
    args = {"key": key}
    return await _get_memory().call_tool("memory_get", args, tenant_id=tenant_context.get())


# ── File Tools (local — no backend needed) ────────────────────


def _resolve_in_workspace(path: str | None, *, default_to_root: bool = True) -> str:
    """Resolve a tool-provided path against the current tenant's workspace.

    Returns the workspace root when ``path`` is None/empty and
    ``default_to_root`` is True. Raises :class:`WorkspacePathError` for
    traversal / symlink escape / absolute-path attempts. (Review fix C2.)
    """
    root = get_workspace_root()
    if path is None or path == "":
        if default_to_root:
            return root
        raise WorkspacePathError("Empty path is not allowed")
    return validate_path(path, root)


@tool
def file_search(query: str, directory: str | None = None) -> str:
    """Search for text in files using grep. Returns matching lines with file paths and line numbers."""
    try:
        search_dir = _resolve_in_workspace(directory)
    except WorkspacePathError as e:
        return f"❌ {e}"
    if not os.path.isdir(search_dir):
        return f"❌ Directory not found: {directory or '.'}"
    try:
        result = subprocess.run(
            ['grep', '-rn', '--include=*.py', '--include=*.java', '--include=*.js', '--include=*.ts',
             '--include=*.json', '--include=*.yaml', '--include=*.yml', '--include=*.md',
             '--include=*.html', '--include=*.css', '--include=*.xml', '--include=*.sql',
             '--include=*.sh', '--include=*.toml', '--include=*.txt', '--include=*.cfg',
             '-I', '--', query, '.'],
            cwd=search_dir, capture_output=True, text=True, timeout=15
        )
        output = result.stdout.strip()
        if not output:
            return f"No matches found for '{query}' in {directory or 'workspace'}"
        lines = output.split('\n')
        if len(lines) > 50:
            output = '\n'.join(lines[:50]) + f'\n... ({len(lines) - 50} more matches)'
        return output
    except subprocess.TimeoutExpired:
        return "Search timed out — try a more specific query"
    except Exception as e:
        return f"❌ Search failed: {e}"


@tool
def file_read(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read the contents of a file with line numbers. Specify start_line/end_line to read a range. Without range, reads first 100 lines. For large files, read in chunks."""
    try:
        resolved = _resolve_in_workspace(path, default_to_root=False)
    except WorkspacePathError as e:
        return f"❌ {e}"
    if not os.path.isfile(resolved):
        return f"❌ File not found: {path}"
    try:
        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()
        total = len(all_lines)
        # Default: first 100 lines (saves context window)
        s = (start_line or 1) - 1
        e = end_line or min(s + 100, total)
        s = max(0, min(s, total))
        e = max(s, min(e, total))
        selected = all_lines[s:e]
        numbered = ''.join(f'{i+s+1:4d} | {line}' for i, line in enumerate(selected))
        header = f"File: {path} ({total} lines total, showing {s+1}-{e})\n"
        if e < total and end_line is None:
            header += f"⚠️ Showing first 100 lines. Use file_read('{path}', start_line={e+1}, end_line={min(e+100, total)}) to read more.\n"
        return header + numbered
    except Exception as e:
        return f"❌ Failed to read {path}: {e}"


@tool
def file_list(directory: str | None = None, depth: int = 3) -> str:
    """List contents of a directory in the workspace recursively. Shows files and subdirectories up to the given depth (default 3). Always call this BEFORE trying to read files to discover the actual file paths."""
    root = get_workspace_root()
    try:
        target = _resolve_in_workspace(directory)
    except WorkspacePathError as e:
        return f"❌ {e}"
    if not os.path.isdir(target):
        return f"❌ Directory not found: {directory or 'workspace root'}"
    IGNORE = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.DS_Store',
              'tmp-m2-repo', '.mypy_cache', '.pytest_cache', '.gradle', 'build', 'target',
              '.sonic', '.venv-debug', '.tox', 'dist', 'egg-info'}
    
    def _tree(dir_path, prefix, current_depth, max_depth):
        if current_depth > max_depth:
            return []
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            return []
        lines = []
        # Filter
        entries = [e for e in entries if e not in IGNORE and not e.startswith('.')]
        dirs = [e for e in entries if os.path.isdir(os.path.join(dir_path, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(dir_path, e))]
        all_items = dirs + files
        for i, name in enumerate(all_items):
            is_last = (i == len(all_items) - 1)
            connector = '└── ' if is_last else '├── '
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                lines.append(f"{prefix}{connector}📁 {name}/")
                extension = '    ' if is_last else '│   '
                lines.extend(_tree(full, prefix + extension, current_depth + 1, max_depth))
            else:
                size = os.path.getsize(full)
                lines.append(f"{prefix}{connector}📄 {name}  ({size}B)")
        return lines

    try:
        rel = os.path.relpath(target, root)
        header = f"📁 {rel}/" if rel != '.' else f"📁 {os.path.basename(target)}/"
        tree_lines = _tree(target, '', 1, depth)
        if not tree_lines:
            return f"Directory '{directory or '.'}' is empty"
        return header + '\n' + '\n'.join(tree_lines)
    except Exception as e:
        return f"❌ Failed to list: {e}"


# ── Code Execution Tools ─────────────────────────────────────

@tool
async def code_run(code: str, language: str = "python", timeout: int = 30) -> str:
    """Execute a code snippet in a sandboxed environment. Returns stdout, stderr, and exit code."""
    return await _get_codeexec().call_tool("code_run", {
        "code": code, "language": language, "timeout": timeout,
    }, tenant_id=tenant_context.get())


@tool
async def code_shell(command: str) -> str:
    """Execute a shell command. Shorthand for code_run with language=shell."""
    return await _get_codeexec().call_tool("code_shell", {"command": command}, tenant_id=tenant_context.get())


from app.tools.rag_tool import rag_search
from app.tools.agent_mode import file_write, file_edit, git_status, git_diff, git_commit, run_tests
from app.tools.ops_tools import (
    git_branch, git_log, pr_create,
    ticket_create, ticket_list, ticket_update,
)
from app.tools.subagent_tool import spawn_subagent

# ── Tool Registry ─────────────────────────────────────────────
# Tools are split into "always available" (local) and "backend-dependent" (remote).
# Remote tools are only registered if their backend is reachable at startup.
# This prevents the LLM from wasting tokens considering tools that will fail.

import logging
_tool_logger = logging.getLogger(__name__)

# Always available — run locally, no external dependency
_LOCAL_TOOLS = [
    rag_search,
    file_search, file_read, file_list,
    file_write, file_edit,
    git_status, git_diff, git_commit,
    git_branch, git_log,
    pr_create,
    ticket_create, ticket_list, ticket_update,
    run_tests,
    # spawn_subagent is a meta-tool: it doesn't talk to any backend
    # service, it invokes another instance of the agent in-process.
    # Always available; per-request guardrails enforced in
    # app.agent.subagent_context.SubagentContext (depth, fanout, budget).
    spawn_subagent,
]

# Require Java MCP backends — only register if reachable
_REMOTE_TOOLS = {
    "memory": [memory_search, memory_set, memory_context, skill_get],
    "codeexec": [code_run, code_shell],
}


def _check_backend(url: str) -> bool:
    """Check if a Java MCP backend is reachable (non-blocking, 1s timeout)."""
    import httpx
    try:
        resp = httpx.get(f"{url}/actuator/health", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


def _build_tool_list() -> list:
    """Build ALL_TOOLS dynamically based on available backends."""
    tools = list(_LOCAL_TOOLS)

    # Check memory server
    if _check_backend(settings.memory_server_url):
        tools.extend(_REMOTE_TOOLS["memory"])
        _tool_logger.info("✅ Memory server reachable — memory tools registered")
    else:
        _tool_logger.info("⚠️  Memory server not reachable — memory tools disabled")

    # Check code execution server
    if _check_backend(settings.codeexec_server_url):
        tools.extend(_REMOTE_TOOLS["codeexec"])
        _tool_logger.info("✅ Code exec server reachable — code tools registered")
    else:
        _tool_logger.info("⚠️  Code exec server not reachable — code tools disabled")

    from app.plugins.loader import get_plugin_tools

    plugin_tools = get_plugin_tools()
    if plugin_tools:
        tools.extend(plugin_tools)
        _tool_logger.info("✅ Loaded %d plugin tools", len(plugin_tools))

    _tool_logger.info("Registered %d tools total", len(tools))
    return tools


# Cache with TTL — re-probe backends periodically instead of freezing at import
_cached_tools: list | None = None
_cache_timestamp: float = 0
_CACHE_TTL_SECONDS: float = 60  # re-check backend availability every 60s


def get_tools() -> list:
    """Get available tools, re-probing backends every 60s.
    
    Fixes: previously ALL_TOOLS was computed once at import time.
    If a backend started after the agent, its tools were permanently disabled.
    """
    global _cached_tools, _cache_timestamp
    import time
    now = time.monotonic()
    if _cached_tools is None or (now - _cache_timestamp) > _CACHE_TTL_SECONDS:
        _cached_tools = _build_tool_list()
        _cache_timestamp = now
    return _cached_tools

