import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUp, Check, Folder, FolderOpen, FolderSearch, Loader2, RefreshCw, X } from 'lucide-react';
import { authFetch } from '@/lib/auth';

interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: FileNode[];
}

interface FileTreeProps {
  onFileSelect: (path: string) => void;
  onWorkspaceOpen: (path: string) => void;
}

interface BrowseDirectory {
  name: string;
  path: string;
}

interface BrowseResponse {
  path: string;
  parent: string | null;
  dirs: BrowseDirectory[];
}

const FILE_ICONS: Record<string, string> = {
  py: '🐍', js: '📜', ts: '📘', java: '☕', json: '{}', md: '📝',
  html: '🌐', css: '🎨', yaml: '⚙', yml: '⚙', sh: '🔧',
};

function getFileIcon(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  return FILE_ICONS[ext] || '📄';
}

function TreeNode({ node, depth, onFileSelect }: {
  node: FileNode; depth: number; onFileSelect: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  if (node.type === 'directory') {
    return (
      <>
        <div
          className="flex cursor-pointer items-center py-0.5 pr-2 text-gray-400 hover:bg-ide-hover"
          style={{ paddingLeft: `${12 + depth * 14}px` }}
          onClick={() => setExpanded(!expanded)}
        >
          <span className="mr-1 w-4 text-center text-xs text-brand-400">
            {expanded ? '▼' : '▶'}
          </span>
          <span className="truncate text-sm">{node.name}</span>
        </div>
        {expanded && node.children?.map((child) => (
          <TreeNode key={child.path} node={child} depth={depth + 1} onFileSelect={onFileSelect} />
        ))}
      </>
    );
  }

  return (
    <div
      className="flex cursor-pointer items-center py-0.5 pr-2 text-gray-400 hover:bg-ide-hover hover:text-gray-200"
      style={{ paddingLeft: `${12 + depth * 14}px` }}
      onClick={() => onFileSelect(node.path)}
    >
      <span className="mr-1 w-4 text-center text-xs">{getFileIcon(node.name)}</span>
      <span className="truncate text-sm">{node.name}</span>
    </div>
  );
}

export function FileTree({ onFileSelect, onWorkspaceOpen }: FileTreeProps) {
  const [workspacePath, setWorkspacePath] = useState('');
  const [tree, setTree] = useState<FileNode[]>([]);
  const [loading, setLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState('');
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserPath, setBrowserPath] = useState('');
  const [browserParent, setBrowserParent] = useState<string | null>(null);
  const [browserDirs, setBrowserDirs] = useState<BrowseDirectory[]>([]);
  const [browserLoading, setBrowserLoading] = useState(false);
  const [browserError, setBrowserError] = useState('');
  const workspaceRequestVersion = useRef(0);

  async function responseError(response: Response): Promise<string> {
    const body = await response.json().catch(() => null);
    if (typeof body?.detail === 'string') return body.detail;
    return `Request failed with HTTP ${response.status}`;
  }

  const doRefreshTree = useCallback(async (expectedRoot?: string) => {
    setLoading(true);
    try {
      const resp = await authFetch('/api/workspace/files');
      const data = await resp.json();
      if (expectedRoot && data.root !== expectedRoot) return;
      console.log('[FileTree] got tree:', data.tree?.length, 'nodes');
      setTree(data.tree || []);
    } catch (e) {
      console.error('[FileTree] refresh error:', e);
      setTree([]);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const requestVersion = ++workspaceRequestVersion.current;
    void authFetch('/api/workspace/current')
      .then((response) => response.ok ? response.json() : null)
      .then((data: { path?: string; exists?: boolean } | null) => {
        if (
          cancelled
          || requestVersion !== workspaceRequestVersion.current
          || !data?.exists
          || !data.path
        ) return;
        setWorkspacePath(data.path);
        onWorkspaceOpen(data.path);
        void doRefreshTree(data.path);
      })
      .catch(() => { /* workspace can still be opened manually */ });
    return () => { cancelled = true; };
  }, [doRefreshTree, onWorkspaceOpen]);

  async function doOpenWorkspace(path?: string) {
    const p = path || workspacePath;
    console.log('[FileTree] doOpenWorkspace:', p);
    if (!p || !p.trim()) {
      alert('Please type a workspace path first');
      return;
    }
    const requestVersion = ++workspaceRequestVersion.current;
    setLoading(true);
    setWorkspaceError('');
    try {
      const resp = await authFetch('/api/workspace/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p }),
      });
      if (requestVersion !== workspaceRequestVersion.current) return;
      console.log('[FileTree] open status:', resp.status);
      if (resp.ok) {
        const data = await resp.json();
        console.log('[FileTree] opened:', data.path);
        setWorkspacePath(data.path);
        onWorkspaceOpen(data.path);
        await doRefreshTree(data.path);
      } else {
        const error = await responseError(resp);
        setWorkspaceError(error);
        console.error('[FileTree] open failed:', error);
      }
    } catch (e) {
      console.error('[FileTree] open error:', e);
      setWorkspaceError(e instanceof Error ? e.message : 'Unable to open workspace');
    }
    setLoading(false);
  }

  async function loadBrowseDirectory(path: string) {
    setBrowserLoading(true);
    setBrowserError('');
    try {
      const response = await authFetch(
        `/api/workspace/browse?path=${encodeURIComponent(path)}`,
      );
      if (!response.ok) {
        throw new Error(await responseError(response));
      }
      const data = await response.json() as BrowseResponse;
      setBrowserPath(data.path);
      setBrowserParent(data.parent);
      setBrowserDirs(data.dirs);
    } catch (error) {
      setBrowserError(error instanceof Error ? error.message : 'Unable to browse directories');
    } finally {
      setBrowserLoading(false);
    }
  }

  function openDirectoryBrowser() {
    setBrowserOpen(true);
    void loadBrowseDirectory(workspacePath.trim() || '~');
  }

  async function selectBrowseDirectory() {
    if (!browserPath) return;
    setBrowserOpen(false);
    await doOpenWorkspace(browserPath);
  }

  return (
    <aside className="hidden w-56 shrink-0 flex-col border-r border-ide-border bg-ide-sidebar sm:flex">
      <div className="flex items-center justify-between border-b border-ide-border px-3 py-2">
        <div className="flex items-center gap-2">
          <FolderOpen className="h-3.5 w-3.5 text-gray-400" />
          <span className="text-xs uppercase tracking-wide text-gray-500">Explorer</span>
        </div>
        <button onClick={() => void doRefreshTree()} className="text-gray-500 hover:text-gray-300" title="Refresh">
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="flex flex-col gap-1.5 border-b border-ide-border px-2 py-2">
        <span className="text-[10px] text-gray-600">Type a path, then press Enter or click Go</span>
        <input
          type="text"
          placeholder="~/projects/my-app"
          value={workspacePath}
          onChange={(e) => setWorkspacePath(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && doOpenWorkspace()}
          disabled={loading}
          className="w-full rounded border border-ide-border bg-ide-panel px-2 py-1 font-mono text-xs text-gray-300 outline-none focus:border-brand-500"
        />
        {workspaceError && <p className="break-words text-[10px] text-red-400">{workspaceError}</p>}
        <div className="flex gap-1">
          <button
            onClick={() => doOpenWorkspace()}
            disabled={loading}
            className="flex-1 rounded bg-brand-600 px-2 py-1 text-xs text-white hover:bg-brand-500 disabled:cursor-not-allowed disabled:bg-ide-panel disabled:text-gray-600"
          >
            Go
          </button>
          <button
            onClick={openDirectoryBrowser}
            disabled={loading}
            className="flex items-center gap-1 rounded border border-ide-border bg-ide-panel px-2 py-1 text-xs text-gray-400 hover:bg-ide-hover hover:text-gray-200 disabled:cursor-not-allowed disabled:opacity-50"
            title="Browse server folders"
          >
            <FolderSearch className="h-3 w-3" /> Browse
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto py-1">
        {tree.length === 0 ? (
          <div className="p-3 text-xs text-gray-600">Open a workspace to browse files</div>
        ) : (
          tree.map((node) => (
            <TreeNode key={node.path} node={node} depth={0} onFileSelect={onFileSelect} />
          ))
        )}
      </div>

      {browserOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onMouseDown={(event) => event.target === event.currentTarget && setBrowserOpen(false)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="directory-browser-title"
            className="flex max-h-[70vh] w-full max-w-lg flex-col overflow-hidden rounded-md border border-ide-border bg-ide-sidebar shadow-2xl"
          >
            <div className="flex items-center gap-2 border-b border-ide-border px-3 py-2">
              <FolderSearch className="h-4 w-4 shrink-0 text-brand-400" />
              <div className="min-w-0 flex-1">
                <h2 id="directory-browser-title" className="text-sm font-semibold text-gray-200">
                  Select workspace folder
                </h2>
                <p className="truncate font-mono text-[10px] text-gray-500" title={browserPath}>
                  {browserPath || 'Loading...'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setBrowserOpen(false)}
                className="rounded p-1 text-gray-500 hover:bg-ide-hover hover:text-gray-200"
                title="Close directory browser"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="flex items-center gap-2 border-b border-ide-border px-3 py-2">
              <button
                type="button"
                onClick={() => browserParent && loadBrowseDirectory(browserParent)}
                disabled={!browserParent || browserLoading}
                className="inline-flex h-7 items-center gap-1 rounded border border-ide-border px-2 text-xs text-gray-300 hover:bg-ide-hover disabled:cursor-not-allowed disabled:opacity-40"
                title="Go to parent folder"
              >
                <ArrowUp className="h-3.5 w-3.5" /> Up
              </button>
              <button
                type="button"
                onClick={() => loadBrowseDirectory('~')}
                disabled={browserLoading}
                className="h-7 rounded border border-ide-border px-2 text-xs text-gray-300 hover:bg-ide-hover disabled:opacity-40"
              >
                Home
              </button>
              {browserLoading && (
                <span className="ml-auto inline-flex items-center gap-1 text-xs text-gray-500">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading
                </span>
              )}
            </div>

            <div className="min-h-32 flex-1 overflow-y-auto py-1">
              {browserError ? (
                <div className="p-3 text-xs text-red-400">{browserError}</div>
              ) : !browserLoading && browserDirs.length === 0 ? (
                <div className="p-3 text-xs text-gray-500">No subfolders</div>
              ) : (
                browserDirs.map((directory) => (
                  <button
                    type="button"
                    key={directory.path}
                    onClick={() => loadBrowseDirectory(directory.path)}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm text-gray-300 hover:bg-ide-hover hover:text-white"
                    title={directory.path}
                  >
                    <Folder className="h-4 w-4 shrink-0 text-brand-400" />
                    <span className="truncate">{directory.name}</span>
                  </button>
                ))
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-ide-border px-3 py-2">
              <button
                type="button"
                onClick={() => setBrowserOpen(false)}
                className="h-8 rounded border border-ide-border px-3 text-xs text-gray-300 hover:bg-ide-hover"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void selectBrowseDirectory()}
                disabled={!browserPath || browserLoading}
                className="inline-flex h-8 items-center gap-1 rounded bg-brand-600 px-3 text-xs font-medium text-white hover:bg-brand-500 disabled:cursor-not-allowed disabled:bg-ide-panel disabled:text-gray-600"
              >
                <Check className="h-3.5 w-3.5" /> Select folder
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
