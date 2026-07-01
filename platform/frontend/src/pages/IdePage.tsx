import { FileTree } from '@/components/FileTree';
import { CodeEditor, useEditorTabs } from '@/components/CodeEditor';
import { ChatPanel } from '@/components/ChatPanel';

/**
 * IDE Page — Coding Agent workspace.
 * Composes FileTree + CodeEditor + ChatPanel into a VS Code-like layout.
 */
export function IdePage() {
  const { tabs, activeTab, openFile, closeTab, reloadFile, setActiveTab } = useEditorTabs();

  return (
    <div className="flex h-full">
      {/* Sidebar: File Tree */}
      <FileTree onFileSelect={openFile} />

      {/* Main area: Editor + Chat */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Code Editor with tabs */}
        <CodeEditor
          tabs={tabs}
          activeTab={activeTab}
          onTabSelect={setActiveTab}
          onTabClose={closeTab}
        />

        {/* Resizable divider (future: make draggable) */}
        <div className="h-1 shrink-0 cursor-ns-resize bg-transparent hover:bg-brand-500" />

        {/* Chat Panel */}
        <ChatPanel
          onFileChanged={reloadFile}
          onFileOpen={openFile}
          activeFile={activeTab ? { path: activeTab } : undefined}
        />
      </div>
    </div>
  );
}
