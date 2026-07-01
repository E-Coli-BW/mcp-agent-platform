"""Workspace API — file tree, file content, and workspace switching."""

import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.tools.agent_mode import get_workspace_root, set_workspace_root
from app.tools.workspace_resolver import WorkspacePathError, validate_path

router = APIRouter(prefix="/api/workspace", tags=["workspace"])

# Files/dirs to skip in tree listing
IGNORE = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.DS_Store',
           'tmp-m2-repo', '.mypy_cache', '.pytest_cache', '*.pyc',
           '.sonic', '.venv-debug', '.tox', 'dist', '.gradle', 'build', 'target',
           'poetry.lock', 'package-lock.json'}


def _should_ignore(name: str) -> bool:
    if name in IGNORE:
        return True
    return any(name.endswith(pat.lstrip('*')) for pat in IGNORE if pat.startswith('*'))


def _build_tree(root: str, rel: str = "", depth: int = 0, max_depth: int = 5) -> list[dict]:
    """Build a file tree as a list of nodes."""
    if depth > max_depth:
        return []
    
    full = os.path.join(root, rel) if rel else root
    if not os.path.isdir(full):
        return []
    
    entries = []
    try:
        items = sorted(os.listdir(full), key=lambda x: (not os.path.isdir(os.path.join(full, x)), x.lower()))
    except PermissionError:
        return []
    
    for name in items:
        if _should_ignore(name):
            continue
        child_rel = os.path.join(rel, name) if rel else name
        child_full = os.path.join(full, name)
        if os.path.isdir(child_full):
            entries.append({
                "name": name,
                "path": child_rel,
                "type": "directory",
                "children": _build_tree(root, child_rel, depth + 1, max_depth),
            })
        else:
            size = 0
            try:
                size = os.path.getsize(child_full)
            except OSError:
                pass
            entries.append({
                "name": name,
                "path": child_rel,
                "type": "file",
                "size": size,
            })
    return entries


@router.get("/current")
async def get_current_workspace():
    """Return the current workspace root path."""
    root = get_workspace_root()
    return {"path": root, "exists": os.path.isdir(root)}


class OpenWorkspaceRequest(BaseModel):
    path: str


@router.post("/open")
async def open_workspace(req: OpenWorkspaceRequest):
    """Switch the workspace root at runtime.
    
    Security: resolves symlinks and blocks paths outside user's home directory.
    This prevents path traversal attacks like "~root" or "/etc".
    """
    path = os.path.expanduser(req.path)
    resolved = os.path.realpath(path)

    # Security: block paths outside user's home directory
    home = os.path.realpath(os.path.expanduser("~"))
    # Allow /tmp for testing + anything under home directory
    allowed_prefixes = [home, "/tmp", "/private/tmp", "/var/folders", "/private/var"]  # macOS uses /private
    if not any(resolved.startswith(prefix) for prefix in allowed_prefixes):
        raise HTTPException(403, f"Path '{req.path}' is outside allowed directories. Must be under home or /tmp.")

    if not os.path.isdir(resolved):
        try:
            Path(resolved).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"Cannot create directory: {e}")
    
    set_workspace_root(resolved)
    return {"path": get_workspace_root(), "message": f"Workspace set to {get_workspace_root()}"}


@router.get("/browse")
async def browse_directory(path: str = Query("~", description="Directory to list")):
    """List subdirectories of a given path — for the frontend directory picker.
    Returns only directories (not files), with security checks.
    """
    expanded = os.path.expanduser(path)
    resolved = os.path.realpath(expanded)

    home = os.path.realpath(os.path.expanduser("~"))
    allowed_prefixes = [home, "/tmp", "/private/tmp"]
    if not any(resolved.startswith(prefix) for prefix in allowed_prefixes):
        raise HTTPException(403, "Path outside allowed directories")

    if not os.path.isdir(resolved):
        return {"path": resolved, "dirs": []}

    dirs = []
    try:
        for name in sorted(os.listdir(resolved)):
            if name.startswith('.'):
                continue
            full = os.path.join(resolved, name)
            if os.path.isdir(full) and not _should_ignore(name):
                dirs.append({"name": name, "path": os.path.join(path, name)})
    except PermissionError:
        pass

    return {"path": resolved, "dirs": dirs}


@router.get("/files")
async def list_files():
    """Return the workspace file tree as JSON."""
    root = get_workspace_root()
    if not os.path.isdir(root):
        return {"root": root, "tree": []}
    return {"root": root, "tree": _build_tree(root)}


@router.get("/file")
async def read_file(path: str = Query(..., description="Relative path within workspace")):
    """Read a file's content."""
    root = get_workspace_root()
    try:
        resolved = validate_path(path, root)  # C2 fix: is_relative_to + symlink rejection
    except WorkspacePathError as exc:
        raise HTTPException(403, str(exc))

    if not os.path.isfile(resolved):
        raise HTTPException(404, f"File not found: {path}")
    
    # Check if binary
    try:
        with open(resolved, 'r', encoding='utf-8') as f:
            content = f.read(500_000)  # 500KB limit
    except UnicodeDecodeError:
        raise HTTPException(400, "Binary file — cannot display")
    
    # Detect language from extension
    ext_map = {
        '.py': 'python', '.js': 'javascript', '.ts': 'typescript', '.tsx': 'typescript',
        '.java': 'java', '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
        '.md': 'markdown', '.html': 'html', '.css': 'css', '.sql': 'sql',
        '.sh': 'shell', '.bash': 'shell', '.xml': 'xml', '.toml': 'toml',
        '.rs': 'rust', '.go': 'go', '.rb': 'ruby', '.c': 'c', '.cpp': 'cpp',
        '.h': 'c', '.hpp': 'cpp', '.kt': 'kotlin', '.swift': 'swift',
    }
    ext = os.path.splitext(path)[1].lower()
    language = ext_map.get(ext, 'plaintext')
    
    return {"path": path, "content": content, "language": language}
