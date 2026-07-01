import { useState, useRef, useEffect } from 'react';
import { MessageSquare, Send } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { authFetch } from '@/lib/auth';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

interface ToolChip {
  tool: string;
  input: Record<string, unknown>;
  isWrite: boolean;
}

interface ChatPanelProps {
  onFileChanged?: (path: string) => void;
  onFileOpen?: (path: string) => void;
  activeFile?: { path: string; visibleStart?: number; visibleEnd?: number };
}

export function ChatPanel({ onFileChanged, onFileOpen, activeFile }: ChatPanelProps) {
  const [messages, setMessages] = useState<(Message | { type: 'tool'; chip: ToolChip })[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState('Ready');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const sessionId = useRef('ide-' + Math.random().toString(36).slice(2, 10));

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const isWriteTool = (name: string): boolean =>
    ['file_write', 'file_edit', 'git_commit'].includes(name);

  const sendMessage = async () => {
    if (!input.trim() || sending) return;
    const text = input.trim();
    setInput('');
    setSending(true);
    setStatus('Thinking...');
    setMessages((prev) => [...prev, { role: 'user', content: text }]);

    let assistantContent = '';
    setMessages((prev) => [...prev, { role: 'assistant', content: '...' }]);

    try {
      const resp = await authFetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'coding-agent',
          messages: [{ role: 'user', content: text }],
          stream: true,
          session_id: sessionId.current,
          active_file: activeFile
            ? {
                path: activeFile.path,
                visible_start: activeFile.visibleStart,
                visible_end: activeFile.visibleEnd,
              }
            : undefined,
        }),
      });

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      let currentEventType: string | null = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';

        for (const line of lines) {
          const t = line.trim();

          if (t.startsWith('event:')) {
            currentEventType = t.slice(6).trim();
            continue;
          }

          if (!t.startsWith('data:')) continue;
          const d = t.slice(5).trim();
          if (d === '[DONE]') continue;

          if (currentEventType === 'tool_start') {
            try {
              const ev = JSON.parse(d);
              const isWrite = isWriteTool(ev.tool);
              setMessages((prev) => [
                ...prev,
                { type: 'tool', chip: { tool: ev.tool, input: ev.input || {}, isWrite } },
              ]);
              setStatus('🔧 ' + ev.tool);
            } catch { /* skip */ }
            currentEventType = null;
            continue;
          }

          if (currentEventType === 'tool_end') {
            try {
              const ev = JSON.parse(d);
              if (isWriteTool(ev.tool) && ev.input?.path) {
                setTimeout(() => onFileChanged?.(ev.input.path), 300);
              }
            } catch { /* skip */ }
            currentEventType = null;
            continue;
          }

          if (currentEventType === 'status') {
            try {
              const ev = JSON.parse(d);
              if (ev.state === 'thinking') setStatus('Thinking...');
              else if (ev.state === 'complete') {
                const duration = ev.duration_ms ? `${(ev.duration_ms / 1000).toFixed(1)}s` : '';
                const tools = ev.tool_count ? `${ev.tool_count} tools` : '';
                const parts = [tools, duration].filter(Boolean).join(', ');
                setStatus(`✅ Done${parts ? ` (${parts})` : ''}`);
              } else if (ev.state === 'file_changed' && ev.path) {
                onFileChanged?.(ev.path);
              }
            } catch { /* skip */ }
            currentEventType = null;
            continue;
          }

          currentEventType = null;

          try {
            const j = JSON.parse(d);
            const c = j.choices?.[0]?.delta?.content || '';
            if (!c) continue;
            console.log('[ChatPanel] chunk:', c.substring(0, 50));

            // Handle tool events
            const toolMatch = c.match(/<!-- TOOL:({.*?}) -->/);
            if (toolMatch) {
              try {
                const ev = JSON.parse(toolMatch[1]);
                if (ev.action === 'start') {
                  const isWrite = isWriteTool(ev.tool);
                  setMessages((prev) => [
                    ...prev,
                    { type: 'tool', chip: { tool: ev.tool, input: ev.input, isWrite } },
                  ]);
                  setStatus('🔧 ' + ev.tool);
                } else if (ev.action === 'end' && ['file_write', 'file_edit'].includes(ev.tool)) {
                  const path = ev.input?.path;
                  if (path) {
                    setTimeout(() => {
                      onFileChanged?.(path);
                    }, 300);
                  }
                }
              } catch { /* skip */ }
              const clean = c.replace(/<!-- TOOL:.*? -->\n?/g, '').replace(/^🔧.*\n?/, '').trim();
              if (clean) assistantContent += clean;
            } else {
              assistantContent += c;
            }

            if (assistantContent) {
              setMessages((prev) => {
                const updated = [...prev];
                // Find last assistant message
                for (let i = updated.length - 1; i >= 0; i--) {
                  if ('role' in updated[i] && (updated[i] as Message).role === 'assistant') {
                    updated[i] = { role: 'assistant', content: assistantContent };
                    break;
                  }
                }
                return updated;
              });
            }
          } catch (parseErr) {
            console.warn('[ChatPanel] SSE parse error:', parseErr, 'raw:', t.substring(0, 100));
          }
        }
      }
    } catch (err) {
      console.error('[ChatPanel] stream error:', err);
      const errMsg = `❌ Error: ${err instanceof Error ? err.message : 'Unknown error'}`;
      setMessages((prev) => {
        const updated = [...prev];
        for (let i = updated.length - 1; i >= 0; i--) {
          if ('role' in updated[i] && (updated[i] as Message).role === 'assistant') {
            updated[i] = { role: 'assistant', content: errMsg };
            break;
          }
        }
        return updated;
      });
    }
    setSending(false);
    setStatus((prev) => (prev.startsWith('✅ Done') ? prev : 'Ready'));
  };

  return (
    <div className="flex h-72 shrink-0 flex-col bg-ide-sidebar">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-ide-border px-3 py-1.5">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-3.5 w-3.5 text-gray-400" />
          <span className="text-xs uppercase tracking-wide text-gray-500">Chat</span>
        </div>
        <span className="text-xs text-gray-600">{status}</span>
      </div>

      {/* Messages */}
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {messages.map((msg, i) => {
          if ('type' in msg && msg.type === 'tool') {
            const { tool, input, isWrite } = msg.chip;
            const detail = (input?.path || input?.query || input?.message || '') as string;
            return (
              <div
                key={i}
                onClick={() => input?.path && onFileOpen?.(input.path as string)}
                className={`inline-flex cursor-pointer items-center gap-1 rounded px-2 py-0.5 text-xs ${
                  isWrite
                    ? 'border border-yellow-900 bg-yellow-950 text-orange-400'
                    : 'border border-green-900 bg-green-950 text-green-400'
                }`}
              >
                {isWrite ? '📝' : '🔍'} {tool}{detail ? `: ${detail}` : ''}
              </div>
            );
          }

          const m = msg as Message;
          return (
            <div
              key={i}
              className={`rounded-md px-3 py-2 text-sm ${
                m.role === 'user'
                  ? 'ml-auto max-w-[70%] rounded-br-sm bg-blue-900/50 text-gray-200'
                  : 'bg-ide-panel text-gray-300'
              }`}
            >
              {m.role === 'assistant' ? (
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    code({ className, children, ...props }) {
                      const match = /language-(\w+)/.exec(className || '');
                      const code = String(children).replace(/\n$/, '');
                      return match ? (
                        <SyntaxHighlighter style={vscDarkPlus} language={match[1]} PreTag="div">
                          {code}
                        </SyntaxHighlighter>
                      ) : (
                        <code className="rounded bg-ide-bg px-1 py-0.5 text-xs" {...props}>
                          {children}
                        </code>
                      );
                    },
                  }}
                >
                  {m.content}
                </ReactMarkdown>
              ) : (
                <span>{m.content}</span>
              )}
            </div>
          );
        })}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="flex gap-2 border-t border-ide-border p-2">
        <input
          type="text"
          placeholder="Ask the agent... (Enter to send)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && sendMessage()}
          disabled={sending}
          className="flex-1 rounded border border-ide-border bg-ide-bg px-3 py-1.5 text-sm text-gray-200 outline-none focus:border-brand-500 disabled:opacity-50"
        />
        <button
          onClick={sendMessage}
          disabled={sending}
          className="rounded bg-brand-600 px-3 py-1.5 text-white hover:bg-brand-500 disabled:cursor-not-allowed disabled:bg-ide-panel"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
