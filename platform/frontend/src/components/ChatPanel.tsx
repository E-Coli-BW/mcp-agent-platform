import { useState, useRef, useEffect } from 'react';
import { Check, ChevronDown, Code2, Headset, Loader2, MessageSquare, Send, ShieldAlert, ThumbsDown, ThumbsUp, X } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { authFetch } from '@/lib/auth';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  feedback?: 'positive' | 'negative';
}

interface ToolChip {
  tool: string;
  input: Record<string, unknown>;
  isWrite: boolean;
}

interface ApprovalRequest {
  approval_id: string;
  run_id: string;
  tool_name: string;
  reason: string;
  status: 'pending' | 'submitting' | 'approved' | 'rejected' | 'error';
  error?: string;
}

interface ChatPanelProps {
  onFileChanged?: (path: string) => void;
  onFileOpen?: (path: string) => void;
  activeFile?: { path: string; visibleStart?: number; visibleEnd?: number };
}

type AgentId = string;

interface AgentDefinition {
  id: AgentId;
  name: string;
  description: string;
  icon: typeof Code2;
  accent: string;
  placeholder: string;
}

const FALLBACK_AGENTS: AgentDefinition[] = [
  {
    id: 'coding-agent',
    name: 'Coding Agent',
    description: 'Read, edit, and run code in this workspace',
    icon: Code2,
    accent: 'text-blue-400',
    placeholder: 'Ask the coding agent... (Enter to send)',
  },
  {
    id: 'customer-qa-agent',
    name: 'Customer QA',
    description: 'Answer from the indexed support knowledge base',
    icon: Headset,
    accent: 'text-emerald-400',
    placeholder: 'Ask about shipping, returns, or support...',
  },
];

type Conversation = { messages: (Message | { type: 'tool'; chip: ToolChip })[]; sessionId: string };

function newConversation(agent: AgentId): Conversation {
  return { messages: [], sessionId: `${agent}-${Math.random().toString(36).slice(2, 10)}` };
}

export function ChatPanel({ onFileChanged, onFileOpen, activeFile }: ChatPanelProps) {
  const [agents, setAgents] = useState<AgentDefinition[]>(FALLBACK_AGENTS);
  const [activeAgent, setActiveAgent] = useState<AgentId>('coding-agent');
  const [conversations, setConversations] = useState<Record<AgentId, Conversation>>(() => ({
    'coding-agent': newConversation('coding-agent'),
    'customer-qa-agent': newConversation('customer-qa-agent'),
  }));
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [status, setStatus] = useState('Ready');
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const activeRunId = useRef<string | undefined>(undefined);
  const activeTraceIds = useRef<string[]>([]);
  const agent = agents.find((item) => item.id === activeAgent) || agents[0];
  const AgentIcon = agent.icon;
  const messages = conversations[activeAgent].messages;
  const hasBlockingApproval = !!approval && ['pending', 'submitting', 'error'].includes(approval.status);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    let cancelled = false;
    authFetch('/api/agents')
      .then((response) => response.ok ? response.json() : Promise.reject(new Error('agent catalog unavailable')))
      .then((body: { data?: Array<{ id: string; name: string; description?: string }> }) => {
        if (cancelled || !body.data?.length) return;
        const next = body.data.map((item) => {
          const fallback = FALLBACK_AGENTS.find((candidate) => candidate.id === item.id);
          return {
            id: item.id,
            name: item.name,
            description: item.description || fallback?.description || 'Configured agent',
            icon: fallback?.icon || Headset,
            accent: fallback?.accent || 'text-violet-400',
            placeholder: fallback?.placeholder || `Ask ${item.name}...`,
          };
        });
        setAgents(next);
        setConversations((current) => {
          const updated = { ...current };
          for (const item of next) updated[item.id] ||= newConversation(item.id);
          return updated;
        });
      })
      .catch(() => { /* fallback catalog keeps the switcher usable */ });
    return () => { cancelled = true; };
  }, []);

  const updateConversation = (update: (conversation: Conversation) => Conversation) => {
    setConversations((prev) => ({
      ...prev,
      [activeAgent]: update(prev[activeAgent]),
    }));
  };

  const switchAgent = (nextAgent: AgentId) => {
    if (sending || hasBlockingApproval || nextAgent === activeAgent) return;
    setApproval(null);
    setActiveAgent(nextAgent);
    setStatus('Ready');
  };

  const decideApproval = async (decision: 'approved' | 'rejected') => {
    if (!approval || approval.status === 'submitting') return;
    const currentApprovalId = approval.approval_id;
    setApproval((current) => current?.approval_id === currentApprovalId
      ? { ...current, status: 'submitting', error: undefined }
      : current);
    setStatus(decision === 'approved' ? 'Approving tool...' : 'Rejecting tool...');

    try {
      const response = await authFetch(`/v1/approvals/${currentApprovalId}/decision`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: decision }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        const detail = typeof body?.detail === 'string' ? body.detail : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      setApproval((current) => current?.approval_id === currentApprovalId
        ? { ...current, status: decision }
        : current);
      setStatus(decision === 'approved' ? 'Approved — waiting for tool...' : 'Tool rejected');
    } catch (error) {
      setApproval((current) => current?.approval_id === currentApprovalId
        ? {
            ...current,
            status: 'error',
            error: error instanceof Error ? error.message : 'Unable to submit decision',
          }
        : current);
      setStatus('Approval failed');
    }
  };

  const sendFeedback = async (message: Message, rating: 'positive' | 'negative') => {
    if (message.feedback) return;
    message.feedback = rating;
    setConversations((prev) => ({ ...prev }));
    await authFetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rating,
        reason: rating === 'positive' ? 'helpful' : 'not_helpful',
        agent: activeAgent,
        session_id: conversations[activeAgent].sessionId,
        run_id: activeRunId.current,
        retrieval_trace_ids: activeTraceIds.current,
        model: activeAgent,
        answer: message.content,
      }),
    }).catch(() => { /* feedback is best effort and must not block chat */ });
  };

  const isWriteTool = (name: string): boolean =>
    ['file_write', 'file_edit', 'git_commit'].includes(name);

  const sendMessage = async () => {
    if (!input.trim() || hasBlockingApproval) return;
    const text = input.trim();
    setInput('');
    setSending(true);
    setApproval(null);
    setStatus('Thinking...');
    updateConversation((conversation) => ({
      ...conversation,
      messages: [...conversation.messages, { role: 'user', content: text }],
    }));

    let assistantContent = '';
    updateConversation((conversation) => ({
      ...conversation,
      messages: [...conversation.messages, { role: 'assistant', content: '...' }],
    }));

    try {
      const resp = await authFetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: activeAgent,
          messages: [{ role: 'user', content: text }],
          stream: true,
          session_id: conversations[activeAgent].sessionId,
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
      let currentEventId: string | null = null;
      const seenToolEventIds = new Set<string>();

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

          if (t.startsWith('id:')) {
            currentEventId = t.slice(3).trim();
            continue;
          }

          if (!t.startsWith('data:')) continue;
          const d = t.slice(5).trim();
          if (d === '[DONE]') continue;

          if (currentEventType === 'tool_start') {
            const eventId = currentEventId;
            currentEventType = null;
            currentEventId = null;
            if (eventId && seenToolEventIds.has(eventId)) continue;
            if (eventId) seenToolEventIds.add(eventId);
            try {
              const ev = JSON.parse(d);
              const isWrite = isWriteTool(ev.tool);
              updateConversation((conversation) => ({
                ...conversation,
                messages: [...conversation.messages, { type: 'tool', chip: { tool: ev.tool, input: ev.input || {}, isWrite } }],
              }));
              setStatus('🔧 ' + ev.tool);
            } catch { /* skip */ }
            continue;
          }

          if (currentEventType === 'approval_required') {
            try {
              const ev = JSON.parse(d) as Omit<ApprovalRequest, 'status'>;
              setApproval({ ...ev, status: 'pending' });
              setStatus(`Approval required: ${ev.tool_name}`);
            } catch { /* skip malformed approval event */ }
            currentEventType = null;
            currentEventId = null;
            continue;
          }

          if (currentEventType === 'tool_end') {
            const eventId = currentEventId;
            currentEventType = null;
            currentEventId = null;
            if (eventId && seenToolEventIds.has(eventId)) continue;
            if (eventId) seenToolEventIds.add(eventId);
            try {
              const ev = JSON.parse(d);
              if (isWriteTool(ev.tool) && ev.input?.path) {
                setTimeout(() => onFileChanged?.(ev.input.path), 300);
              }
            } catch { /* skip */ }
            continue;
          }

            if (currentEventType === 'status') {
              try {
                const ev = JSON.parse(d);
                if (ev.run_id) activeRunId.current = ev.run_id;
                if (Array.isArray(ev.retrieval_trace_ids)) activeTraceIds.current = ev.retrieval_trace_ids;
              if (ev.state === 'thinking') setStatus('Thinking...');
              else if (ev.state === 'queued') setStatus('Queued behind active turn...');
              else if (ev.state === 'complete') {
                const duration = ev.duration_ms ? `${(ev.duration_ms / 1000).toFixed(1)}s` : '';
                const tools = ev.tool_count ? `${ev.tool_count} tools` : '';
                const parts = [tools, duration].filter(Boolean).join(', ');
                setStatus(`✅ Done${parts ? ` (${parts})` : ''}`);
                setApproval(null);
              } else if (ev.state === 'file_changed' && ev.path) {
                onFileChanged?.(ev.path);
              }
            } catch { /* skip */ }
            currentEventType = null;
            currentEventId = null;
            continue;
          }

          currentEventType = null;
          currentEventId = null;

          try {
            const j = JSON.parse(d);
            const c = j.choices?.[0]?.delta?.content || '';
            if (!c) continue;
            console.log('[ChatPanel] chunk:', c.substring(0, 50));

            assistantContent += c
              .replace(/<!-- TOOL:.*? -->\n?/g, '')
              .replace(/^🔧 Using \w+\.\.\.\s*/gm, '');

            if (assistantContent) {
              updateConversation((conversation) => {
                const updated = [...conversation.messages];
                // Find last assistant message
                for (let i = updated.length - 1; i >= 0; i--) {
                  if ('role' in updated[i] && (updated[i] as Message).role === 'assistant') {
                    updated[i] = { role: 'assistant', content: assistantContent };
                    break;
                  }
                }
                return { ...conversation, messages: updated };
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
      updateConversation((conversation) => {
        const updated = [...conversation.messages];
        for (let i = updated.length - 1; i >= 0; i--) {
          if ('role' in updated[i] && (updated[i] as Message).role === 'assistant') {
            updated[i] = { role: 'assistant', content: errMsg };
            break;
          }
        }
        return { ...conversation, messages: updated };
      });
    }
    setSending(false);
    setApproval((current) => current && ['pending', 'error'].includes(current.status) ? current : null);
    setStatus((prev) => (
      prev.startsWith('✅ Done') || prev.startsWith('Approval required') || prev === 'Approval failed'
        ? prev
        : 'Ready'
    ));
  };

  return (
    <div className="flex h-80 min-w-0 shrink-0 flex-col bg-ide-sidebar">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-ide-border px-3 py-1.5">
        <div className="relative flex items-center gap-2">
          <MessageSquare className="h-3.5 w-3.5 text-gray-400" />
          <label className="sr-only" htmlFor="agent-switcher">Choose agent</label>
          <select
            id="agent-switcher"
            value={activeAgent}
            onChange={(event) => switchAgent(event.target.value as AgentId)}
            className="appearance-none bg-transparent pr-5 text-xs font-semibold text-gray-200 outline-none hover:text-white"
            title="Switch agent"
          >
            {agents.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
          <ChevronDown className="pointer-events-none absolute right-0 h-3 w-3 text-gray-500" />
          <span className="hidden text-[11px] text-gray-600 xl:inline">{agent.description}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-600">
          <span className={`flex items-center gap-1 ${agent.accent}`}>
            <AgentIcon className="h-3 w-3" />
            <span className="hidden sm:inline">{messages.length ? 'Session active' : 'New session'}</span>
          </span>
          <span>{status}</span>
        </div>
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
                <>
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
                  {m.content !== '...' && (
                    <div className="mt-2 flex items-center gap-1 border-t border-white/5 pt-1">
                      <span className="mr-1 text-[10px] text-gray-600">Helpful?</span>
                      <button onClick={() => sendFeedback(m, 'positive')} disabled={!!m.feedback} className="rounded p-1 text-gray-500 hover:bg-ide-hover hover:text-green-400 disabled:opacity-40" title="Helpful">
                        <ThumbsUp className="h-3 w-3" />
                      </button>
                      <button onClick={() => sendFeedback(m, 'negative')} disabled={!!m.feedback} className="rounded p-1 text-gray-500 hover:bg-ide-hover hover:text-red-400 disabled:opacity-40" title="Not helpful">
                        <ThumbsDown className="h-3 w-3" />
                      </button>
                      {m.feedback && <span className="text-[10px] text-gray-600">Thanks for the feedback</span>}
                    </div>
                  )}
                </>
              ) : (
                <span>{m.content}</span>
              )}
            </div>
          );
        })}
        <div ref={messagesEndRef} />
      </div>

      {approval && (
        <div
          className="border-t border-amber-900/70 bg-amber-950/40 px-3 py-2"
          role="alert"
          aria-live="assertive"
        >
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start">
            <div className="flex min-w-0 flex-1 items-start gap-2">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-xs font-semibold text-amber-200">Tool approval required</span>
                  <code className="rounded bg-black/25 px-1.5 py-0.5 text-[11px] text-amber-300">
                    {approval.tool_name}
                  </code>
                </div>
                <p className="mt-1 break-words text-xs text-amber-100/75">{approval.reason}</p>
                {approval.error && (
                  <p className="mt-1 text-xs text-red-400">{approval.error}</p>
                )}
              </div>
            </div>
            <div className="flex shrink-0 items-center justify-end gap-1.5">
              {approval.status === 'pending' || approval.status === 'error' ? (
                <>
                  <button
                    type="button"
                    onClick={() => decideApproval('rejected')}
                    className="inline-flex h-7 items-center gap-1 rounded border border-ide-border px-2 text-xs text-gray-300 hover:bg-ide-hover hover:text-white"
                    title="Reject tool execution"
                  >
                    <X className="h-3.5 w-3.5" />
                    Reject
                  </button>
                  <button
                    type="button"
                    onClick={() => decideApproval('approved')}
                    className="inline-flex h-7 items-center gap-1 rounded bg-amber-500 px-2 text-xs font-medium text-black hover:bg-amber-400"
                    title="Approve tool execution"
                  >
                    <Check className="h-3.5 w-3.5" />
                    Approve
                  </button>
                </>
              ) : approval.status === 'submitting' ? (
                <span className="inline-flex h-7 items-center gap-1.5 px-2 text-xs text-amber-200">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  Submitting
                </span>
              ) : (
                <span className={`inline-flex h-7 items-center gap-1 px-2 text-xs ${
                  approval.status === 'approved' ? 'text-green-400' : 'text-gray-400'
                }`}>
                  {approval.status === 'approved'
                    ? <Check className="h-3.5 w-3.5" />
                    : <X className="h-3.5 w-3.5" />}
                  {approval.status === 'approved' ? 'Approved' : 'Rejected'}
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Input */}
      <div className="flex gap-2 border-t border-ide-border p-2">
        <input
          type="text"
          placeholder={agent.placeholder}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && sendMessage()}
          disabled={hasBlockingApproval}
          className="flex-1 rounded border border-ide-border bg-ide-bg px-3 py-1.5 text-sm text-gray-200 outline-none focus:border-brand-500 disabled:cursor-not-allowed disabled:opacity-50"
        />
        <button
          onClick={sendMessage}
          disabled={hasBlockingApproval || !input.trim()}
          className="rounded bg-brand-600 px-3 py-1.5 text-white hover:bg-brand-500 disabled:cursor-not-allowed disabled:bg-ide-panel disabled:text-gray-600"
        >
          <Send className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
