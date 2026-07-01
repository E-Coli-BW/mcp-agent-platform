import { useState } from 'react';
import { FolderOpen, RefreshCw, HardDrive } from 'lucide-react';
import { authFetch } from '@/lib/auth';

interface FileNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: FileNode[];
}

interface FileTreeProps {
  onFileSelect: (path: string) => void;
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

export function FileTree({ onFileSelect }: FileTreeProps) {
  const [workspacePath, setWorkspacePath] = useState('');
  const [tree, setTree] = useState<FileNode[]>([]);
  const [loading, setLoading] = useState(false);
  const supportsFilePicker = typeof window !== 'undefined' && 'showDirectoryPicker' in window;

  async function doRefreshTree() {
    setLoading(true);
    try {
      const resp = await authFetch('/api/workspace/files');
      const data = await resp.json();
      console.log('[FileTree] got tree:', data.tree?.length, 'nodes');
      setTree(data.tree || []);
    } catch (e) {
      console.error('[FileTree] refresh error:', e);
      setTree([]);
    }
    setLoading(false);
  }

  async function doOpenWorkspace(path?: string) {
    const p = path || workspacePath;
    console.log('[FileTree] doOpenWorkspace:', p);
    if (!p || !p.trim()) {
      alert('Please type a workspace path first');
      return;
    }
    setLoading(true);
    try {
      const resp = await authFetch('/api/workspace/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p }),
      });
      console.log('[FileTree] open status:', resp.status);
      if (resp.ok) {
        const data = await resp.json();
        console.log('[FileTree] opened:', data.path);
        setWorkspacePath(data.path);
        await doRefreshTree();
      } else {
        const err = await resp.text();
        console.error('[FileTree] open failed:', err);
      }
    } catch (e) {
      console.error('[FileTree] open error:', e);
    }
    setLoading(false);
  }

  async function doPickDirectory() {
    if (!supportsFilePicker) {
      alert('Your browser does not support the File System Access API.\nPlease type the path manually.');
      return;
    }
    try {
      // Opens the native OS file picker (Finder on macOS, Explorer on Windows)
      // @ts-expect-error — showDirectoryPicker is not in all TS libs yet
      const handle: FileSystemDirectoryHandle = await window.showDirectoryPicker({ mode: 'read' });
      const name = handle.name;
      console.log('[FileTree] native picker selected:', name);

      // Browser security prevents reading the full path.
      // Try common locations in order.
      const candidates = [
        `~/${name}`,
        `~/projects/${name}`,
        `~/work/${name}`,
        `~/Documents/${name}`,
        `~/Desktop/${name}`,
        `~/repos/${name}`,
        `~/dev/${name}`,
      ];

      for (const candidate of candidates) {
        try {
          // Use plain fetch (not authFetch) to avoid logout on 404
          const resp = await fetch(`/api/workspace/open`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: candidate }),
          });
          if (resp.ok) {
            const data = await resp.json();
            // Verify it actually has files (not just auto-created empty dir)
            const filesResp = await fetch('/api/workspace/files');
            if (filesResp.ok) {
              const filesData = await filesResp.json();
              if (filesData.tree && filesData.tree.length > 0) {
                console.log('[FileTree] found at:', data.path, 'with', filesData.tree.length, 'nodes');
                setWorkspacePath(data.path);
                setTree(filesData.tree);
                return;
              }
            }
          }
        } catch { /* try next */ }
      }

      // None matched — fill the input so user can fix the path
      setWorkspacePath(`~/${name}`);
      console.log('[FileTree] could not auto-locate, user needs to edit path');
    } catch (e: unknown) {
      // User cancelled the native picker — that's fine
      if (e instanceof Error && e.name !== 'AbortError') {
        console.error('Directory picker error:', e);
      }
    }
  }

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-ide-border bg-ide-sidebar">
      <div className="flex items-center justify-between border-b border-ide-border px-3 py-2">
        <div className="flex items-center gap-2">
          <FolderOpen className="h-3.5 w-3.5 text-gray-400" />
          <span className="text-xs uppercase tracking-wide text-gray-500">Explorer</span>
        </div>
        <button onClick={doRefreshTree} className="text-gray-500 hover:text-gray-300" title="Refresh">
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
          className="w-full rounded border border-ide-border bg-ide-panel px-2 py-1 font-mono text-xs text-gray-300 outline-none focus:border-brand-500"
        />
        <div className="flex gap-1">
          <button
            onClick={() => doOpenWorkspace()}
            className="flex-1 rounded bg-brand-600 px-2 py-1 text-xs text-white hover:bg-brand-500"
          >
            Go
          </button>
          {supportsFilePicker && (
            <button
              onClick={doPickDirectory}
              className="flex items-center gap-1 rounded border border-ide-border bg-ide-panel px-2 py-1 text-xs text-gray-400 hover:bg-ide-hover hover:text-gray-200"
              title="Browse folders"
            >
              <HardDrive className="h-3 w-3" /> Browse
            </button>
          )}
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
    </aside>
  );
}
