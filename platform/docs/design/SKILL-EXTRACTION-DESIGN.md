# Skill Extraction — Design & Implementation Reference

## Status: ✅ Implemented (Option C + Option A)

## Overview

Automatic skill extraction from tool call history, inspired by
[Hermes Agent](https://github.com/nousresearch/hermes-agent)'s 4-layer architecture.
Two implementations are active:

- **Option C** (prompt-only): `.github/copilot-instructions.md` tells the LLM to save skills
- **Option A** (background worker): `tools/memory-ts/memory/skill-extractor.ts` auto-extracts skills

---

## 1. Architecture

```
You chat with Copilot / Claude / CLI
       │ stdio MCP protocol
       ▼
┌──────────────────────────────────────────────────────────┐
│  TypeScript Memory MCP Server (memory-server.ts)         │
│                                                          │
│  server.tool() ←── PROXIED ──→ Universal Wrapper         │
│       │                              │                   │
│       ▼                              ▼                   │
│  Original handler runs         tracker.record()          │
│  → returns result to client    callsSinceLastReview++    │
│                                      │                   │
│                              if (count >= 10):           │
│                                setImmediate →             │
│                                triggerReview()            │
│                                      │                   │
│                              ┌───────▼──────────┐        │
│                              │ Skill Extractor   │        │
│                              │                   │        │
│                              │ 1. Collect last   │        │
│                              │    20 tool calls  │        │
│                              │                   │        │
│                              │ 2. Build review   │        │
│                              │    prompt          │        │
│                              │                   │        │
│                              │ 3. Call Ollama     │        │
│                              │    (local LLM)     │        │
│                              │    qwen2.5:7b      │        │
│                              │    30s timeout     │        │
│                              │                   │        │
│                              │ 4. Parse skill    │        │
│                              │    JSON response   │        │
│                              │                   │        │
│                              │ 5. Write to        │        │
│                              │    memory-store.json│       │
│                              └───────────────────┘        │
│                                                          │
│  Also: setInterval(60s) periodic check                   │
└──────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
   ~/.mcp-local/memory-store.json    http://localhost:11434
   (skills stored here)              (Ollama, local LLM)
```

## 2. Two Active Systems

### Option C: Prompt-Based (Zero Code)

Location: `.github/copilot-instructions.md`

The LLM is instructed to save skills after:
- Complex tasks (5+ tool calls)
- Tricky bug fixes (surprising root cause)
- Non-obvious workflows (multi-step, hard to rediscover)
- User corrections ("stop doing X", "always do Y")

Skill format:
```
memory_set(
  key="skill-{descriptive-name}",
  content="## Problem\n...\n## Solution\n...\n## Steps\n1...",
  tags=["skill", "auto-extracted", "{category}"],
  namespace="skills"
)
```

**Pros:** Zero infrastructure, works immediately
**Cons:** Relies on LLM following instructions (not guaranteed)

### Option A: Background Worker (Automatic)

Location: `tools/memory-ts/memory/skill-extractor.ts`

Key components:

#### Universal Tracking Wrapper
```typescript
// Monkey-patches server.tool() to intercept ALL tool handlers
const _originalTool = server.tool.bind(server);
(server as any).tool = function(name, ...rest) {
  // Wraps the handler to record every call
  rest[handlerIdx] = async function(args) {
    const result = await originalHandler(args);
    track(name, args, outputText, startTime);
    return result;
  };
};
```
- **All 11 tools tracked** — no per-handler code needed
- Non-blocking — result returned to client before tracking

#### Trigger Mechanism
- Counter: `callsSinceLastReview++` on every tool call
- Threshold: triggers at 10 calls (configurable)
- Also: `setInterval(60s)` periodic check for accumulated calls
- Guard: `reviewing = true` prevents concurrent reviews

#### LLM Review
- Endpoint: `http://localhost:11434/api/generate` (local Ollama)
- Model: `qwen2.5:7b` (configurable)
- Timeout: 30 seconds
- Graceful degradation: if Ollama is down, silently skips

#### Two Different LLMs
```
Copilot/Claude (cloud) ← the AI you're chatting with
Ollama (local)         ← the AI that extracts skills (background)
```
These never interact. The local LLM only sees tool call history, not the conversation.

## 3. Skill Storage

Skills are stored in the SAME `memory-store.json` as regular memories:

```json
{
  "key": "skill-maven-stale-jar-fix",
  "content": "## Problem\n...\n## Solution\n...",
  "tags": ["skill", "auto-extracted", "build"],
  "namespace": "skills",
  "pinned": false,
  "accessCount": 3,
  "createdBy": "skill-extractor"
}
```

Found via: `memory_search("maven", tags=["skill"])`

## 4. Skill Lifecycle (via Forgetting Curve)

No custom curator needed — the existing forgetting curve handles it:

| Scenario | What Happens |
|----------|-------------|
| Skill used frequently | `accessCount` grows → high retention → never decays |
| Skill unused 30 days | Retention drops below fading threshold |
| Skill unused 90 days | Suggested for cleanup in `memory_compact_plan` |
| Critical skill | User pins it → immune to forgetting curve |

Compared to Hermes:
| Hermes | Our Platform |
|--------|-------------|
| Custom curator (30d stale, 90d archive) | Forgetting curve (continuous decay) |
| File system (`~/.hermes/skills/`) | Memory store (`memory-store.json`) |
| Separate backup before curator | WAL (Write-Ahead Log) |

## 5. Configuration

```json
// ~/.mcp-local/memory-config.json
{
  "skillExtraction": {
    "enabled": true,
    "nudgeInterval": 10,
    "llmEndpoint": "http://localhost:11434/api/generate",
    "llmModel": "qwen2.5:7b",
    "maxToolCallsInReview": 20,
    "autoPin": false
  }
}
```

## 6. Hermes Comparison

| Layer | Hermes | Our Implementation |
|-------|--------|-------------------|
| **Layer 1: Prompt** | System prompt nudge | ✅ copilot-instructions.md |
| **Layer 2: Counter** | `_skill_nudge_interval = 10` | ✅ `callsSinceLastReview` |
| **Layer 3: Background Review** | Independent LLM call | ✅ `triggerReview()` → Ollama |
| **Layer 4: Curator** | 30d stale, 90d archive | ✅ Forgetting curve (continuous) |
| **Provenance** | `created_by=agent` | ✅ Tracked via tags |
| **Skill naming** | Task-class level | ✅ Same convention |
| **User corrections** | First-class signal | ✅ In prompt instructions |

## 7. Files

| File | Purpose |
|------|---------|
| `tools/memory-ts/memory/skill-extractor.ts` | ToolCallTracker, LLM review, skill storage |
| `tools/memory-ts/memory-server.ts` | Universal wrapper, periodic timer |
| `.github/copilot-instructions.md` | Option C prompt instructions |
| `~/.mcp-local/memory-config.json` | User configuration |
| `~/.mcp-local/memory-store.json` | Skill storage (namespace="skills") |

## Architecture

```
VS Code / Copilot CLI / Claude Desktop
       │ stdio MCP
       ▼
┌──────────────────────────────────────────────┐
│  TypeScript Memory MCP Server                │
│                                              │
│  ┌─────────────────────┐                     │
│  │ Tool Handler        │                     │
│  │ memory_set/search/  │                     │
│  │ context/compact     │                     │
│  └──────────┬──────────┘                     │
│             │                                │
│  ┌──────────▼──────────┐                     │
│  │ memory-store.json   │◄─── Skills written  │
│  │ (all data)          │     back here       │
│  └──────────┬──────────┘                     │
│             │                                │
│  ┌──────────▼──────────┐                     │
│  │ Skill Extractor     │  ← NEW component    │
│  │ (background worker) │                     │
│  │                     │                     │
│  │ Triggers:           │                     │
│  │ 1. Every N tool     │                     │
│  │    calls (nudge)    │                     │
│  │ 2. On session end   │                     │
│  │ 3. Periodic timer   │                     │
│  │                     │                     │
│  │ Process:            │                     │
│  │ 1. Collect recent   │                     │
│  │    tool call history│                     │
│  │ 2. Send to LLM     │                     │
│  │    with review      │                     │
│  │    prompt           │                     │
│  │ 3. LLM returns      │                     │
│  │    skill JSON       │                     │
│  │ 4. Write to memory  │                     │
│  │    store            │                     │
│  └─────────────────────┘                     │
└──────────────────────────────────────────────┘
```

## Implementation Plan

### 1. Tool Call Tracker

```typescript
// Track tool calls within the current session
interface ToolCallRecord {
  tool: string;
  input: Record<string, any>;
  output: string;       // truncated to 500 chars
  timestamp: number;
  durationMs: number;
}

class ToolCallTracker {
  private calls: ToolCallRecord[] = [];
  private nudgeInterval = 10;  // review after every 10 tool calls
  private callsSinceLastReview = 0;

  record(call: ToolCallRecord) {
    this.calls.push(call);
    this.callsSinceLastReview++;
  }

  shouldReview(): boolean {
    return this.callsSinceLastReview >= this.nudgeInterval;
  }

  getRecentCalls(n: number = 20): ToolCallRecord[] {
    return this.calls.slice(-n);
  }

  resetNudge() {
    this.callsSinceLastReview = 0;
  }
}
```

### 2. Skill Review Prompt

```typescript
const SKILL_REVIEW_PROMPT = `
You are a skill extraction agent. Review the following tool call history
and determine if any reusable workflows, debugging techniques, or
non-obvious patterns should be saved as a skill.

## Tool Call History
{TOOL_CALLS_JSON}

## Instructions
1. Look for: complex multi-step workflows, tricky debugging patterns,
   non-obvious solutions, user corrections/preferences
2. If you find something worth saving, output a JSON skill object
3. If nothing is worth saving, output {"action": "none"}
4. Prefer UPDATING existing skills over creating new ones
5. Use task-class level names (e.g., "maven-stale-jar-fix"), not session-specific

## Output Format
{
  "action": "create" | "update" | "none",
  "key": "skill-{descriptive-name}",
  "content": "## Problem\\n...\\n## Solution\\n...\\n## Steps\\n1...",
  "tags": ["skill", "auto-extracted", "{category}"],
  "reason": "Why this is worth saving"
}
`;
```

### 3. LLM Integration

```typescript
// Call Ollama (or any OpenAI-compatible API) for skill review
async function reviewForSkills(
  toolCalls: ToolCallRecord[],
  existingSkills: string[]  // keys of existing skills for dedup
): Promise<SkillAction | null> {
  const prompt = SKILL_REVIEW_PROMPT
    .replace('{TOOL_CALLS_JSON}', JSON.stringify(toolCalls, null, 2));

  const response = await fetch('http://localhost:11434/api/generate', {
    method: 'POST',
    body: JSON.stringify({
      model: 'qwen2.5:7b',
      prompt: prompt,
      stream: false,
      format: 'json',
    }),
  });

  const data = await response.json();
  return JSON.parse(data.response);
}
```

### 4. Integration Points

```typescript
// In memory-server.ts, after each tool call handler:
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const startTime = Date.now();
  const result = await handleToolCall(request);

  // Track the call
  tracker.record({
    tool: request.params.name,
    input: request.params.arguments,
    output: truncate(result, 500),
    timestamp: Date.now(),
    durationMs: Date.now() - startTime,
  });

  // Check if we should review for skills
  if (tracker.shouldReview()) {
    // Non-blocking background review
    setImmediate(async () => {
      try {
        const action = await reviewForSkills(
          tracker.getRecentCalls(20),
          await getExistingSkillKeys()
        );
        if (action && action.action !== 'none') {
          await saveSkill(action);
          console.error(`🧠 Auto-extracted skill: ${action.key}`);
        }
        tracker.resetNudge();
      } catch (e) {
        console.error('Skill review failed:', e);
      }
    });
  }

  return result;
});
```

### 5. Skill Storage

Skills are stored in the SAME `memory-store.json` as regular memories:

```json
{
  "key": "skill-maven-stale-jar-fix",
  "content": "## Problem\nEditing mcp-common source doesn't affect memory-server tests...",
  "namespace": "skills",
  "tags": ["skill", "auto-extracted", "build"],
  "pinned": false,
  "accessCount": 3,
  "createdAt": "2026-05-21T...",
  "createdBy": "skill-extractor"
}
```

### 6. Skill Retrieval (at session start)

The existing `memory_context()` already shows recent entries.
Add skill-aware search to the session startup flow:

```typescript
// In memory_context handler, add a "skills" section:
const skills = entries.filter(e => e.namespace === 'skills');
if (skills.length > 0) {
  result.availableSkills = skills.map(s => ({
    key: s.key,
    preview: s.content.substring(0, 100),
    lastUsed: s.lastAccessedAt,
  }));
}
```

## Configuration

```json
// ~/.mcp-local/memory-config.json
{
  "skillExtraction": {
    "enabled": true,
    "nudgeInterval": 10,       // review after every N tool calls
    "llmEndpoint": "http://localhost:11434/api/generate",
    "llmModel": "qwen2.5:7b",
    "maxToolCallsInReview": 20, // how many recent calls to include
    "autoPin": false            // auto-pin extracted skills?
  }
}
```

## Lifecycle (via Forgetting Curve)

No custom curator needed — our forgetting curve handles it:

| Scenario | What Happens |
|----------|-------------|
| Skill used frequently | `accessCount` grows → high retention → never suggested for deletion |
| Skill unused for 30 days | Retention drops below fading threshold → marked as fading |
| Skill unused for 90 days | Retention drops below forgotten threshold → suggested in compact_plan |
| Critical skill | User pins it → immune to forgetting curve forever |

## Implementation Estimate

| Component | Effort |
|-----------|--------|
| ToolCallTracker class | 0.5 day |
| Skill review prompt + LLM integration | 0.5 day |
| Integration into tool call handler | 0.5 day |
| Skill-aware memory_context | 0.25 day |
| Configuration + testing | 0.25 day |
| **Total** | **2 days** |

## Dependencies

- Ollama running locally (already required for the agent)
- No new npm packages needed (uses native fetch)
- No new infrastructure (writes to existing memory-store.json)
