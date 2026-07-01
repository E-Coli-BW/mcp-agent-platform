/**
 * Skill Extractor — Background worker that auto-extracts reusable skills
 * from tool call history using LLM review.
 *
 * Inspired by Hermes Agent's 4-layer skill extraction architecture.
 * Uses the same memory-store.json as regular memories.
 *
 * Trigger: every N tool calls → spawn async LLM review
 * Storage: namespace="skills", tags=["skill", "auto-extracted"]
 * Lifecycle: managed by the existing forgetting curve
 */

import { loadStore, saveStore, loadConfig } from "./persistence.js";

// ── Types ────────────────────────────────────────────────────

export interface ToolCallRecord {
  tool: string;
  input: Record<string, unknown>;
  output: string;      // truncated to 500 chars
  timestamp: string;
  durationMs: number;
}

interface SkillAction {
  action: "create" | "update" | "none";
  key?: string;
  content?: string;
  tags?: string[];
  reason?: string;
}

interface SkillConfig {
  enabled: boolean;
  nudgeInterval: number;
  llmEndpoint: string;
  llmModel: string;
  maxToolCallsInReview: number;
  autoPin: boolean;
}

// ── Default Config ───────────────────────────────────────────

const DEFAULT_CONFIG: SkillConfig = {
  enabled: true,
  nudgeInterval: 10,
  llmEndpoint: "http://localhost:11434/api/generate",
  llmModel: "qwen2.5:7b",
  maxToolCallsInReview: 20,
  autoPin: false,
};

// ── Tool Call Tracker ────────────────────────────────────────

export class ToolCallTracker {
  private calls: ToolCallRecord[] = [];
  private callsSinceLastReview = 0;
  private config: SkillConfig;
  private reviewing = false;  // prevent concurrent reviews

  constructor() {
    const userConfig = loadConfig();
    this.config = {
      ...DEFAULT_CONFIG,
      ...(userConfig as any)?.skillExtraction,
    };
  }

  /** Record a tool call. Call this after every tool handler. */
  record(call: ToolCallRecord): void {
    this.calls.push(call);
    // Keep only last 50 calls to prevent unbounded growth
    if (this.calls.length > 50) {
      this.calls = this.calls.slice(-50);
    }
    this.callsSinceLastReview++;
  }

  /** Check if we should trigger a background skill review. */
  shouldReview(): boolean {
    return (
      this.config.enabled &&
      !this.reviewing &&
      this.callsSinceLastReview >= this.config.nudgeInterval
    );
  }

  /** Get recent tool calls for the review prompt. */
  getRecentCalls(): ToolCallRecord[] {
    return this.calls.slice(-this.config.maxToolCallsInReview);
  }

  /** Run the background skill review. Non-blocking. */
  async triggerReview(): Promise<void> {
    if (!this.shouldReview()) return;

    this.reviewing = true;
    this.callsSinceLastReview = 0;

    try {
      const recentCalls = this.getRecentCalls();
      const existingSkills = getExistingSkillKeys();

      console.error(`🧠 Skill review: analyzing ${recentCalls.length} recent tool calls...`);

      const action = await reviewForSkills(
        recentCalls,
        existingSkills,
        this.config
      );

      if (action && action.action !== "none" && action.key && action.content) {
        saveSkill(action, this.config.autoPin);
        console.error(`🧠 Skill ${action.action}d: ${action.key} — ${action.reason}`);
      } else {
        console.error(`🧠 Skill review: nothing to extract`);
      }
    } catch (e: any) {
      console.error(`🧠 Skill review failed: ${e.message}`);
    } finally {
      this.reviewing = false;
    }
  }
}

// ── Skill Review Prompt ──────────────────────────────────────

function buildReviewPrompt(
  toolCalls: ToolCallRecord[],
  existingSkills: string[]
): string {
  return `You are a skill extraction agent. Review the following tool call history and determine if any reusable workflows, debugging techniques, or non-obvious patterns should be saved as a skill.

## Recent Tool Call History
${JSON.stringify(toolCalls, null, 2)}

## Existing Skills (avoid duplicates)
${existingSkills.length > 0 ? existingSkills.join(", ") : "(none)"}

## Instructions
1. Look for: complex multi-step workflows (5+ calls), tricky debugging patterns, non-obvious solutions, user corrections
2. If you find something worth saving, output a JSON skill object
3. If nothing is worth saving, output {"action": "none"}
4. Prefer UPDATING existing skills over creating new ones
5. Use task-class level names (e.g., "skill-maven-stale-jar-fix"), NOT session-specific names
6. Include: problem description, solution steps, why it works, pitfalls to avoid

## Output (JSON only, no markdown):
{"action": "create", "key": "skill-descriptive-name", "content": "## Problem\\n...\\n## Solution\\n...\\n## Steps\\n1. ...", "tags": ["skill", "auto-extracted", "category"], "reason": "One sentence why"}
or
{"action": "none"}`;
}

// ── LLM Integration ──────────────────────────────────────────

async function reviewForSkills(
  toolCalls: ToolCallRecord[],
  existingSkills: string[],
  config: SkillConfig
): Promise<SkillAction | null> {
  const prompt = buildReviewPrompt(toolCalls, existingSkills);

  try {
    const response = await fetch(config.llmEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: config.llmModel,
        prompt,
        stream: false,
        format: "json",
      }),
      signal: AbortSignal.timeout(30_000), // 30s timeout
    });

    if (!response.ok) {
      console.error(`🧠 LLM returned ${response.status}`);
      return null;
    }

    const data = await response.json() as { response: string };
    const parsed = JSON.parse(data.response) as SkillAction;
    return parsed;
  } catch (e: any) {
    // Graceful degradation — if Ollama isn't running, just skip
    if (e.message?.includes("fetch failed") || e.message?.includes("ECONNREFUSED")) {
      console.error(`🧠 Ollama not available — skipping skill review`);
    } else {
      console.error(`🧠 LLM call failed: ${e.message}`);
    }
    return null;
  }
}

// ── Skill Storage ────────────────────────────────────────────

function getExistingSkillKeys(): string[] {
  const store = loadStore();
  return Object.values(store.entries)
    .filter(e => e.namespace === "skills" || e.tags?.includes("skill"))
    .map(e => e.key);
}

function saveSkill(action: SkillAction, autoPin: boolean): void {
  if (!action.key || !action.content) return;

  const store = loadStore();
  const now = new Date().toISOString();
  const existing = store.entries[action.key];

  store.entries[action.key] = {
    key: action.key,
    content: action.content,
    tags: action.tags ?? ["skill", "auto-extracted"],
    namespace: "skills",
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
    accessCount: existing?.accessCount ?? 0,
    lastAccessedAt: now,
    pinned: autoPin || existing?.pinned || false,
  };

  saveStore(store);
}

// ── Truncation Helper ────────────────────────────────────────

export function truncateOutput(text: string, maxLen: number = 500): string {
  if (text.length <= maxLen) return text;
  return text.substring(0, maxLen) + `... (${text.length - maxLen} chars truncated)`;
}
