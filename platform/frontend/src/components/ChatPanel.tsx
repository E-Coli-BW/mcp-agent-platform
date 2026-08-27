import { useState, useRef, useEffect } from 'react';
import { Check, CheckCircle2, ChevronDown, Code2, Headset, Loader2, MessageSquare, Pencil, Search, Send, ShieldAlert, Square, Terminal, ThumbsDown, ThumbsUp, X, XCircle } from 'lucide-react';
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
  invocationKey: string;
  tool: string;
  input: Record<string, unknown>;
  isWrite: boolean;
  status: 'running' | 'ok' | 'error';
  output?: string;
}

function toolInputText(input: Record<string, unknown>, key: string): string {
  const value = input[key];
  return typeof value === 'string' ? value : '';
}

function boundedDetail(value: string, limit = 4000): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}\n... (truncated)`;
}

function firstLine(value: string, limit = 180): string {
  const line = value.split(/\r?\n/, 1)[0];
  return line.length <= limit ? line : `${line.slice(0, limit)}...`;
}

function testCommandFromOutput(output?: string): string {
  return output?.match(/^\$ (.+)$/m)?.[1] || '';
}

function toolPresentation(chip: ToolChip): { summary: string; detail?: string } {
  const { tool, input, output } = chip;
  const path = toolInputText(input, 'path');
  const query = toolInputText(input, 'query');

  if (tool === 'file_read') {
    const start = input.start_line;
    const end = input.end_line;
    const range = typeof start === 'number' && typeof end === 'number'
      ? ` (lines ${start}-${end})`
      : '';
    return { summary: `${path || '<unspecified>'}${range}` };
  }
  if (tool === 'file_list') {
    const directory = toolInputText(input, 'directory') || '.';
    const depth = typeof input.depth === 'number' ? ` (depth ${input.depth})` : '';
    return { summary: `${directory}${depth}` };
  }
  if (tool === 'file_search') return { summary: query || '<empty query>' };
  if (tool === 'run_tests') {
    const command = testCommandFromOutput(output) || toolInputText(input, 'test_command');
    return { summary: command ? `$ ${command}` : 'auto-detecting test command' };
  }
  if (tool === 'code_shell') {
    const command = toolInputText(input, 'command');
    return {
      summary: `$ ${firstLine(command)}`,
      detail: command.includes('\n') || command.length > 180 ? boundedDetail(command) : undefined,
    };
  }
  if (tool === 'code_run') {
    const code = toolInputText(input, 'code');
    const language = toolInputText(input, 'language') || 'python';
    const prefix = language === 'shell' ? '$' : `${language}:`;
    return {
      summary: `${prefix} ${firstLine(code)}`,
      detail: code.includes('\n') || code.length > 180 ? boundedDetail(code) : undefined,
    };
  }
  if (tool === 'git_status') return { summary: '$ git status --short' };
  if (tool === 'git_diff') return { summary: '$ git diff' };
  if (tool === 'git_commit') {
    return { summary: `$ git commit -m ${JSON.stringify(toolInputText(input, 'message'))}` };
  }
  if (path) {
    const content = toolInputText(input, 'content');
    return { summary: path, detail: content ? boundedDetail(content) : undefined };
  }

  const serialized = JSON.stringify(input);
  return { summary: serialized === '{}' ? 'no arguments' : firstLine(serialized) };
}

function toolOutputSummary(output?: string): string {
  if (!output) return '';
  return output
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith('$ ') && line !== '```') || '';
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
  const streamAbortController = useRef<AbortController | null>(null);
  const stopRequested = useRef(false);
  const fileChangeTimeouts = useRef<ReturnType<typeof setTimeout>[]>([]);
  const agent = agents.find((item) => item.id === activeAgent) || agents[0];
  const AgentIcon = agent.icon;
  const messages = conversations[activeAgent].messages;
  const hasBlockingApproval = !!approval && ['pending', 'submitting', 'error'].includes(approval.status);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => () => {
    stopRequested.current = true;
    const runId = activeRunId.current;
    if (runId) {
      void authFetch(`/v1/runs/${runId}/cancel`, { method: 'POST' })
        .catch(() => { /* aborting the stream remains the fallback */ });
    }
    streamAbortController.current?.abort();
    for (const timeoutId of fileChangeTimeouts.current) clearTimeout(timeoutId);
    fileChangeTimeouts.current = [];
  }, []);

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

  const stopGeneration = () => {
    if (!sending) return;
    stopRequested.current = true;
    setStatus('Stopping...');
    const runId = activeRunId.current;
    if (runId) {
      void authFetch(`/v1/runs/${runId}/cancel`, { method: 'POST' })
        .catch(() => { /* disconnecting the stream remains the fallback */ });
    }
    streamAbortController.current?.abort();
  };

  const sendMessage = async () => {
    if (!input.trim() || hasBlockingApproval || sending) return;
    const text = input.trim();
    const abortController = new AbortController();
    streamAbortController.current = abortController;
    stopRequested.current = false;
    activeRunId.current = undefined;
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
        signal: abortController.signal,
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
      const responseRunId = resp.headers.get('X-Run-ID');
      if (responseRunId) activeRunId.current = responseRunId;
      const toolRunKey = responseRunId || `${conversations[activeAgent].sessionId}:${Date.now()}`;

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
              updateConversation((conversation) => {
                const messages = [...conversation.messages];
                const toolMessage = {
                  type: 'tool',
                  chip: {
                    invocationKey: `${toolRunKey}:${ev.seq}`,
                    tool: ev.tool,
                    input: ev.input || {},
                    isWrite,
                    status: 'running',
                  },
                } as const;
                const assistantIndex = messages.findLastIndex(
                  (message) => 'role' in message && message.role === 'assistant',
                );
                if (assistantIndex >= 0) messages.splice(assistantIndex, 0, toolMessage);
                else messages.push(toolMessage);
                return { ...conversation, messages };
              });
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
              updateConversation((conversation) => ({
                ...conversation,
                messages: conversation.messages.map((message) => (
                  'type' in message
                  && message.type === 'tool'
                  && message.chip.invocationKey === `${toolRunKey}:${ev.seq}`
                    ? {
                        ...message,
                        chip: {
                          ...message.chip,
                          input: ev.input || message.chip.input,
                          output: typeof ev.output === 'string' ? ev.output : '',
                          status: ev.status === 'error' ? 'error' : 'ok',
                        },
                      }
                    : message
                )),
              }));
              if (isWriteTool(ev.tool) && ev.input?.path) {
                const timeoutId = setTimeout(() => onFileChanged?.(ev.input.path), 300);
                fileChangeTimeouts.current.push(timeoutId);
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
      const wasStopped = stopRequested.current || (
        err instanceof DOMException && err.name === 'AbortError'
      );
      if (!wasStopped) console.error('[ChatPanel] stream error:', err);
      const errMsg = wasStopped
        ? 'Generation stopped.'
        : `❌ Error: ${err instanceof Error ? err.message : 'Unknown error'}`;
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
    const wasStopped = stopRequested.current;
    streamAbortController.current = null;
    activeRunId.current = undefined;
    stopRequested.current = false;
    setSending(false);
    setApproval((current) => current && ['pending', 'error'].includes(current.status) ? current : null);
    setStatus((prev) => (
      wasStopped
        ? 'Stopped'
        : prev.startsWith('✅ Done') || prev.startsWith('Approval required') || prev === 'Approval failed'
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
            const { tool, input, isWrite, status, output } = msg.chip;
            const presentation = toolPresentation(msg.chip);
            const resultSummary = status === 'running' ? '' : toolOutputSummary(output);
            const isExecution = ['run_tests', 'code_run', 'code_shell'].includes(tool);
            return (
              <div
                key={i}
                className={`w-full rounded border px-2 py-1.5 text-xs ${
                  isWrite
                    ? 'border-yellow-900 bg-yellow-950/60 text-orange-300'
                    : 'border-green-900 bg-green-950/50 text-green-300'
                }`}
              >
                <div className="flex min-w-0 items-start gap-1.5">
                  {isExecution
                    ? <Terminal className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    : isWrite
                    ? <Pencil className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    : <Search className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
                  <code className="shrink-0 font-semibold">{tool}</code>
                  <button
                    type="button"
                    onClick={() => input?.path && onFileOpen?.(input.path as string)}
                    disabled={!input?.path}
                    className="min-w-0 flex-1 break-all text-left font-mono text-[11px] text-gray-300 disabled:cursor-default"
                    title={presentation.summary}
                  >
                    {presentation.summary}
                  </button>
                  {status === 'running' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-label="Running" />}
                  {status === 'ok' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-label="Completed" />}
                  {status === 'error' && <XCircle className="h-3.5 w-3.5 shrink-0 text-red-400" aria-label="Failed" />}
                </div>
                {presentation.detail && (
                  <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap border-t border-white/10 pt-1 font-mono text-[11px] text-gray-400">
                    {presentation.detail}
                  </pre>
                )}
                {resultSummary && (
                  <div className={`mt-1 truncate border-t border-white/10 pt-1 text-[11px] ${status === 'error' ? 'text-red-400' : 'text-gray-500'}`} title={resultSummary}>
                    {resultSummary}
                  </div>
                )}
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
          disabled={hasBlockingApproval || sending}
          className="flex-1 rounded border border-ide-border bg-ide-bg px-3 py-1.5 text-sm text-gray-200 outline-none focus:border-brand-500 disabled:cursor-not-allowed disabled:opacity-50"
        />
        <button
          type="button"
          onClick={sending ? stopGeneration : sendMessage}
          disabled={!sending && (hasBlockingApproval || !input.trim())}
          className={`rounded px-3 py-1.5 text-white disabled:cursor-not-allowed disabled:bg-ide-panel disabled:text-gray-600 ${
            sending ? 'bg-red-700 hover:bg-red-600' : 'bg-brand-600 hover:bg-brand-500'
          }`}
          title={sending ? 'Stop generation' : 'Send message'}
        >
          {sending ? <Square className="h-4 w-4" fill="currentColor" /> : <Send className="h-4 w-4" />}
        </button>
      </div>
    </div>
  );
}
