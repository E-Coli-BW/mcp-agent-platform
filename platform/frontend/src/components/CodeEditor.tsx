import { authFetch } from "@/lib/auth";
import { useCallback, useState } from 'react';
import Editor from '@monaco-editor/react';
import { X } from 'lucide-react';

export interface EditorTab {
  path: string;
  content: string;
  language: string;
  modified: boolean;
}

const FILE_ICONS: Record<string, string> = {
  py: '🐍', js: '📜', ts: '📘', java: '☕', json: '{}', md: '📝',
  html: '🌐', css: '🎨', yaml: '⚙', yml: '⚙', sh: '🔧',
};

function getIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  return FILE_ICONS[ext] || '📄';
}

function detectLanguage(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || '';
  const map: Record<string, string> = {
    py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
    java: 'java', json: 'json', md: 'markdown', html: 'html', css: 'css',
    yaml: 'yaml', yml: 'yaml', sh: 'shell', sql: 'sql', xml: 'xml',
    rs: 'rust', go: 'go', rb: 'ruby', php: 'php', c: 'c', cpp: 'cpp',
  };
  return map[ext] || 'plaintext';
}

interface CodeEditorProps {
  tabs: EditorTab[];
  activeTab: string | null;
  onTabSelect: (path: string) => void;
  onTabClose: (path: string) => void;
}

export function CodeEditor({ tabs, activeTab, onTabSelect, onTabClose }: CodeEditorProps) {
  const active = tabs.find((t) => t.path === activeTab);

  if (!active) {
    return (
      <div className="flex flex-1 items-center justify-center text-gray-600">
        <div className="text-center">
          <h2 className="text-lg font-light">Coding Agent IDE</h2>
          <p className="mt-1 text-sm text-gray-500">Open a workspace or ask the agent to create files</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      {/* Tabs */}
      <div className="flex h-9 shrink-0 items-stretch overflow-x-auto border-b border-ide-border bg-ide-sidebar">
        {tabs.map((tab) => {
          const name = tab.path.split('/').pop() || tab.path;
          const isActive = tab.path === activeTab;
          return (
            <div
              key={tab.path}
              onClick={() => onTabSelect(tab.path)}
              className={`flex cursor-pointer items-center gap-1.5 border-r border-ide-border px-3 text-xs ${
                isActive
                  ? 'border-b-2 border-b-brand-500 bg-ide-bg text-gray-200'
                  : 'text-gray-500 hover:bg-ide-panel'
              }`}
            >
              <span>{getIcon(name)}</span>
              <span>{name}</span>
              {tab.modified && <span className="text-orange-400">●</span>}
              <button
                onClick={(e) => { e.stopPropagation(); onTabClose(tab.path); }}
                className="ml-1 opacity-0 hover:opacity-100 group-hover:opacity-50"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          );
        })}
      </div>

      {/* Editor */}
      <Editor
        height="100%"
        theme="vs-dark"
        path={active.path}
        defaultValue={active.content}
        language={active.language}
        options={{
          fontSize: 13,
          fontFamily: "'Cascadia Code','Fira Code','Consolas',monospace",
          minimap: { enabled: true },
          scrollBeyondLastLine: false,
          automaticLayout: true,
          padding: { top: 8 },
        }}
      />
    </div>
  );
}

/** Hook to manage editor tabs */
export function useEditorTabs() {
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);

  const openFile = useCallback(async (path: string) => {
    // Already open?
    if (tabs.find((t) => t.path === path)) {
      setActiveTab(path);
      return;
    }

    try {
      const resp = await authFetch(`/api/workspace/file?path=${encodeURIComponent(path)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      const tab: EditorTab = {
        path,
        content: data.content,
        language: data.language || detectLanguage(path),
        modified: false,
      };
      setTabs((prev) => [...prev, tab]);
      setActiveTab(path);
    } catch { /* ignore */ }
  }, [tabs]);

  const closeTab = useCallback((path: string) => {
    setTabs((prev) => {
      const idx = prev.findIndex((t) => t.path === path);
      const next = prev.filter((t) => t.path !== path);
      if (activeTab === path) {
        setActiveTab(next.length ? next[Math.min(idx, next.length - 1)].path : null);
      }
      return next;
    });
  }, [activeTab]);

  const reloadFile = useCallback(async (path: string) => {
    try {
      const resp = await authFetch(`/api/workspace/file?path=${encodeURIComponent(path)}`);
      if (!resp.ok) return;
      const data = await resp.json();
      setTabs((prev) =>
        prev.map((t) =>
          t.path === path ? { ...t, content: data.content, modified: true } : t
        )
      );
      // Clear modified indicator after 3s
      setTimeout(() => {
        setTabs((prev) =>
          prev.map((t) => (t.path === path ? { ...t, modified: false } : t))
        );
      }, 3000);
    } catch { /* ignore */ }

    // If not open, open it
    if (!tabs.find((t) => t.path === path)) {
      openFile(path);
    }
  }, [tabs, openFile]);

  return { tabs, activeTab, openFile, closeTab, reloadFile, setActiveTab };
}
