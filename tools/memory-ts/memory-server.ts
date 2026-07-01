#!/usr/bin/env node
/**
 * Memory Store MCP Server v2.0
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * Cross-session persistent memory system exposed to all AI clients via MCP.
 * Cross-session persistent memory system, exposed to all AI clients via the MCP protocol.
 *
 * Philosophy: "Store everything, retrieve on demand, let LLM decide"
 *                        "Store everything, retrieve on demand, let the LLM decide"
 * Forgetting model: Ebbinghaus forgetting curve R = e^(-t/S)
 *
 * Module structure:
 *   memory/types.ts           — Type definitions
 *   memory/persistence.ts     — Store / Health / WAL / Config persistence
 *   memory/forgetting-curve.ts — Ebbinghaus forgetting curve algorithm
 *   memory/search.ts          — TF-IDF search + retention weighting
 *   memory/health.ts          — Health diagnostics
 *   memory/importers.ts       — Copilot Sessions / Project Knowledge import
 *   memory-server.ts          — MCP Tool registration + entry point (this file)
 *
 * Safety:
 *   - NO automatic deletion — forgetting curve is diagnostic only
 *   - Pinned memories are immune to decay (retention = 1.0 always)
 *   - >70% forgotten triggers safe mode (no bulk delete suggestion) / safety valve against accidental deletion
 *   - WAL protects against server crash mid-write
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

import { textResult } from "./memory/types.js";
import {
  loadStore, saveStore, getStoreSizeKB, MEMORY_FILE,
  loadHealth, saveHealth, loadConfig,
  walAppend, walClear, recoverFromWAL,
} from "./memory/persistence.js";
import { relevanceScore } from "./memory/search.js";
import { analyzeRetention } from "./memory/forgetting-curve.js";
import { diagnoseHealth, retentionSummary } from "./memory/health.js";
import { importCopilotSessions, importProjectKnowledge } from "./memory/importers.js";
import { ToolCallTracker, truncateOutput } from "./memory/skill-extractor.js";

// ─── Tool Preset Gate ─────────────────────────────────────────────────────────
// TOOL_PRESET env var controls which tools are registered.
// "minimal" (5) | "standard" (7) | "full" (all, default)

const MINIMAL_TOOLS = new Set([
  "memory_search", "memory_set", "memory_get", "memory_list", "memory_context",
]);
const STANDARD_TOOLS = new Set([
  ...MINIMAL_TOOLS, "memory_delete", "memory_pin",
]);

function parseToolPreset(): Set<string> | null {
  const preset = (process.env.TOOL_PRESET ?? "full").toLowerCase();
  switch (preset) {
    case "minimal": return MINIMAL_TOOLS;
    case "standard": return STANDARD_TOOLS;
    case "full": return null; // null = register all
    default:
      console.error(`⚠️ Unknown TOOL_PRESET "${preset}", falling back to "full"`);
      return null;
  }
}

const allowedTools = parseToolPreset();

// ─── MCP Server ──────────────────────────────────────────────────────────────

const server = new McpServer({ name: "memory-store", version: "2.0.0" });
const skillTracker = new ToolCallTracker();

/**
 * Track a tool call and trigger background skill review if needed.
 * Call at the end of every tool handler.
 */
function track(toolName: string, input: Record<string, unknown>, output: string, startTime: number): void {
  skillTracker.record({
    tool: toolName,
    input,
    output: truncateOutput(output),
    timestamp: new Date().toISOString(),
    durationMs: Date.now() - startTime,
  });
  if (skillTracker.shouldReview()) {
    skillTracker.triggerReview().catch(() => {});
  }
}

/**
 * Universal tool registration wrapper — auto-tracks every tool call
 * AND enforces the TOOL_PRESET gate (skips registration for hidden tools).
 */
const _originalTool = server.tool.bind(server) as Function;
(server as any).tool = function(name: string, ...rest: any[]) {
  // Preset gate: skip registration if tool is not in allowed set
  if (allowedTools && !allowedTools.has(name)) {
    return; // tool code is preserved, just not registered
  }

  // Find the handler (last argument that's a function)
  const handlerIdx = rest.findIndex((arg: any) => typeof arg === 'function');
  if (handlerIdx === -1) {
    return _originalTool(name, ...rest);
  }

  const originalHandler = rest[handlerIdx];
  rest[handlerIdx] = async function(args: any) {
    const startTime = Date.now();
    const result = await originalHandler(args);
    const outputText = result?.content?.[0]?.text ?? JSON.stringify(result).substring(0, 500);
    track(name, args ?? {}, outputText, startTime);
    return result;
  };

  return _originalTool(name, ...rest);
};

// ── memory_set ───────────────────────────────────────────────────────────────
server.tool(
  "memory_set",
  "Save/update a memory. Key must be unique per namespace.",
  {
    key: z.string().describe("Unique identifier"),
    content: z.string().describe("Content to remember"),
    tags: z.array(z.string()).optional().describe("Tags for search"),
    namespace: z.string().optional().describe("Namespace. Default: 'default'"),
    pinned: z.boolean().optional().describe("If true, immune to forgetting curve"),
  },
  async ({ key, content, tags, namespace, pinned }) => {
    const startTime = Date.now();
    const now = new Date().toISOString();
    walAppend({ key, content, tags: tags ?? [], namespace: namespace ?? "default", timestamp: now, operation: "set" });

    const store = loadStore();
    const existing = store.entries[key];

    store.entries[key] = {
      key, content,
      tags: tags ?? existing?.tags ?? [],
      namespace: namespace ?? existing?.namespace ?? "default",
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
      accessCount: existing?.accessCount ?? 0,
      lastAccessedAt: existing?.lastAccessedAt ?? null,
      pinned: pinned ?? existing?.pinned ?? false,
    };

    saveStore(store);
    walClear();

    const pin = store.entries[key].pinned ? " 📌" : "";
    const resultText = existing
      ? `✅ Memory updated: "${key}" (namespace: ${store.entries[key].namespace})${pin}`
      : `✅ Memory created: "${key}" (namespace: ${store.entries[key].namespace})${pin}`;
    return textResult(resultText);
  }
);

// ── memory_get ───────────────────────────────────────────────────────────────
server.tool(
  "memory_get",
  "Retrieve a memory by its exact key. Returns full content and metadata.",
  { key: z.string().describe("The key to retrieve") },
  async ({ key }) => {
    const store = loadStore();
    const entry = store.entries[key];
    if (!entry) return textResult(`❌ Memory not found: "${key}"`);

    entry.accessCount += 1;
    entry.lastAccessedAt = new Date().toISOString();
    saveStore(store);

    return textResult(JSON.stringify({
      key: entry.key, content: entry.content, tags: entry.tags,
      namespace: entry.namespace, createdAt: entry.createdAt,
      updatedAt: entry.updatedAt, accessCount: entry.accessCount,
      lastAccessedAt: entry.lastAccessedAt, pinned: entry.pinned ?? false,
    }, null, 2));
  }
);

// ── memory_search ────────────────────────────────────────────────────────────
server.tool(
  "memory_search",
  "Search memories by query+tags. Returns ranked results weighted by forgetting curve.",
  {
    query: z.string().describe("Search query"),
    tags: z.array(z.string()).optional().describe("Filter/boost by tags"),
    namespace: z.string().optional().describe("Filter by namespace"),
    limit: z.number().optional().describe("Max results. Default: 10"),
  },
  async ({ query, tags, namespace, limit }) => {
    const store = loadStore();
    const config = loadConfig();
    const maxResults = limit ?? 10;
    const queryTokens = query.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter((t) => t.length > 1);
    const queryTags = tags ?? [];

    let entries = Object.values(store.entries);
    if (namespace) entries = entries.filter((e) => e.namespace === namespace);

    const scored = entries
      .map((entry) => ({ entry, score: relevanceScore(entry, queryTokens, queryTags, config) }))
      .filter((s) => s.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, maxResults);

    if (scored.length === 0) {
      return textResult(`🔍 No memories found matching "${query}"${namespace ? ` in namespace "${namespace}"` : ""}`);
    }

    const results = scored.map((s, i) => ({
      rank: i + 1, key: s.entry.key,
      content: s.entry.content.length > 200 ? s.entry.content.slice(0, 200) + "..." : s.entry.content,
      tags: s.entry.tags, namespace: s.entry.namespace,
      score: Math.round(s.score * 100) / 100,
    }));

    return textResult(`🔍 Found ${scored.length} result(s) for "${query}":\n\n${JSON.stringify(results, null, 2)}`);
  }
);

// ── memory_delete ────────────────────────────────────────────────────────────
server.tool(
  "memory_delete",
  "Delete a memory by key.",
  { key: z.string().describe("Key to delete") },
  async ({ key }) => {
    const store = loadStore();
    if (!store.entries[key]) return textResult(`❌ Memory not found: "${key}"`);

    walAppend({ key, content: "", tags: [], namespace: store.entries[key].namespace, timestamp: new Date().toISOString(), operation: "delete" });
    delete store.entries[key];
    saveStore(store);
    walClear();

    return textResult(`🗑️ Memory deleted: "${key}"`);
  }
);

// ── memory_pin ───────────────────────────────────────────────────────────────
server.tool(
  "memory_pin",
  "Pin/unpin a memory. Pinned = immune to forgetting curve.",
  {
    key: z.string().describe("Key to pin/unpin"),
    pinned: z.boolean().optional().describe("true=pin, false=unpin. Default: true"),
  },
  async ({ key, pinned }) => {
    const store = loadStore();
    if (!store.entries[key]) return textResult(`❌ Memory not found: "${key}"`);

    store.entries[key].pinned = pinned ?? true;
    saveStore(store);

    return textResult(store.entries[key].pinned
      ? `📌 Memory pinned: "${key}" — will never be auto-forgotten`
      : `📌 Memory unpinned: "${key}" — subject to forgetting curve`);
  }
);

// ── memory_list ──────────────────────────────────────────────────────────────
server.tool(
  "memory_list",
  "List memory keys, optionally filtered by namespace or tags.",
  {
    namespace: z.string().optional().describe("Filter by namespace"),
    tags: z.array(z.string()).optional().describe("Filter by tags"),
  },
  async ({ namespace, tags }) => {
    const store = loadStore();
    let entries = Object.values(store.entries);

    if (namespace) entries = entries.filter((e) => e.namespace === namespace);
    if (tags && tags.length > 0) {
      const tagSet = new Set(tags.map((t) => t.toLowerCase()));
      entries = entries.filter((e) => e.tags.some((t) => tagSet.has(t.toLowerCase())));
    }

    if (entries.length === 0) return textResult("📭 No memories found with the given filters.");

    const summary = entries
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
      .map((e) => ({
        key: e.key, namespace: e.namespace, tags: e.tags,
        contentPreview: e.content.length > 80 ? e.content.slice(0, 80) + "..." : e.content,
        updatedAt: e.updatedAt, pinned: e.pinned ?? false,
      }));

    return textResult(`📋 ${entries.length} memory/memories found:\n\n${JSON.stringify(summary, null, 2)}`);
  }
);

// ── import_copilot_sessions ──────────────────────────────────────────────────
server.tool(
  "import_copilot_sessions",
  "Import Copilot session data from CLI and VS Code into memory.",
  { reimport: z.boolean().optional().describe("Overwrite existing. Default: false") },
  async ({ reimport }) => {
    const store = loadStore();
    const result = importCopilotSessions(store, reimport ?? false);
    saveStore(store);

    return textResult(
      `📥 Copilot session import complete:\n- Imported: ${result.imported}\n- Skipped: ${result.skipped}\n` +
      `- Stored in namespace: "copilot-sessions"\n\nUse memory_search(query="...", namespace="copilot-sessions") to search.`
    );
  }
);

// ── import_project_knowledge ─────────────────────────────────────────────────
server.tool(
  "import_project_knowledge",
  "Scan directory for AI context files (AGENTS.md, copilot-instructions.md, etc) and import.",
  {
    directory: z.string().describe("Root directory to scan (supports ~)"),
    maxDepth: z.number().optional().describe("Max depth. Default: 2"),
    reimport: z.boolean().optional().describe("Overwrite existing. Default: false"),
  },
  async ({ directory, maxDepth, reimport }) => {
    const dir = directory.startsWith("~") ? path.join(os.homedir(), directory.slice(1)) : path.resolve(directory);
    if (!fs.existsSync(dir)) return textResult(`❌ Directory not found: ${dir}`);

    const store = loadStore();
    const result = importProjectKnowledge(store, dir, maxDepth ?? 2, reimport ?? false);
    saveStore(store);

    const projectEntries = Object.values(store.entries).filter((e) => e.namespace === "project-knowledge");
    const projectNames = [...new Set(projectEntries.map((e) => {
      try { return JSON.parse(e.content).projectName; } catch { return "unknown"; }
    }))];

    return textResult(
      `📥 Project knowledge import complete:\n- Imported: ${result.imported}\n- Skipped: ${result.skipped}\n` +
      `- Projects: ${projectNames.join(", ")}\n- Stored in namespace: "project-knowledge"`
    );
  }
);

// ── memory_compact_plan ──────────────────────────────────────────────────────
server.tool(
  "memory_compact_plan",
  "Analyze memory store and generate compaction plan (merge/delete candidates). Call before compact_execute.",
  { dryRun: z.boolean().optional().describe("If true (default), only show plan.") },
  async ({ dryRun }) => {
    const store = loadStore();
    const config = loadConfig();
    const health = loadHealth();
    const entries = Object.values(store.entries);
    const isDryRun = dryRun ?? true;

    const retentionData = analyzeRetention(entries, config);
    const forgotten = retentionData.filter((r) => r.status === "forgotten");
    const fading = retentionData.filter((r) => r.status === "fading");

    // Namespace breakdown
    const nsBreakdown: Record<string, { count: number; totalChars: number; keys: string[] }> = {};
    for (const e of entries) {
      if (!nsBreakdown[e.namespace]) nsBreakdown[e.namespace] = { count: 0, totalChars: 0, keys: [] };
      nsBreakdown[e.namespace].count++;
      nsBreakdown[e.namespace].totalChars += e.content.length;
      nsBreakdown[e.namespace].keys.push(e.key);
    }

    // Merge candidates
    const mergeGroups: Array<{ namespace: string; prefix: string; keys: string[]; totalChars: number }> = [];
    for (const [ns, info] of Object.entries(nsBreakdown)) {
      const prefixMap = new Map<string, string[]>();
      for (const key of info.keys) {
        const parts = key.split("-");
        const prefix = parts.slice(0, Math.min(2, parts.length)).join("-");
        if (!prefixMap.has(prefix)) prefixMap.set(prefix, []);
        prefixMap.get(prefix)!.push(key);
      }
      for (const [prefix, keys] of prefixMap) {
        if (keys.length >= 3) {
          const totalChars = keys.reduce((sum, k) => sum + (store.entries[k]?.content.length ?? 0), 0);
          mergeGroups.push({ namespace: ns, prefix, keys, totalChars });
        }
      }
    }

    const plan = {
      timestamp: new Date().toISOString(),
      currentState: {
        totalEntries: entries.length,
        totalChars: entries.reduce((s, e) => s + e.content.length, 0),
        storageSizeKB: getStoreSizeKB(),
        retention: retentionSummary(retentionData),
        namespaces: Object.fromEntries(
          Object.entries(nsBreakdown).map(([ns, info]) => [ns, { count: info.count, chars: info.totalChars }])
        ),
      },
      forgettingCurve: {
        forgotten: forgotten.map((r) => ({ key: r.key, retention: r.retention, days: r.daysSinceInteraction, ns: r.namespace })),
        fading: fading.map((r) => ({ key: r.key, retention: r.retention, days: r.daysSinceInteraction, ns: r.namespace })),
      },
      suggestions: {
        mergeGroups: mergeGroups.map((g) => ({
          action: "MERGE", description: `"${g.namespace}" prefix "${g.prefix}": ${g.keys.length} entries (${g.totalChars} chars)`,
          keys: g.keys,
        })),
        // Safety valve: if >70% of memories are forgotten, they're most likely long-unused — bulk deletion is not advised
        deleteCandidates: (forgotten.length / Math.max(entries.length, 1)) > 0.7
          ? { safeMode: true, message: `${forgotten.length}/${entries.length} are already forgotten (likely long-unused); pin important memories with memory_pin() before cleaning up`, keys: [] }
          : { safeMode: false, keys: forgotten.filter((r) => !r.pinned).map((r) => r.key) },
      },
      diagnostics: diagnoseHealth(store, config),
      compactHistory: health.compactHistory.slice(-3),
      isDryRun,
      nextStep: isDryRun
        ? "Please review the plan. Once you agree, call memory_compact_execute(deleteKeys=[...], mergeOperations=[...]). You can protect important memories first with memory_pin(key)."
        : "Plan generated, awaiting confirmation.",
    };

    return textResult(`📋 Memory Compact Plan:\n\n${JSON.stringify(plan, null, 2)}`);
  }
);

// ── memory_compact_execute ───────────────────────────────────────────────────
server.tool(
  "memory_compact_execute",
  "Execute compaction: merge entries and/or delete stale ones. Call compact_plan first.",
  {
    deleteKeys: z.array(z.string()).optional().describe("Keys to delete"),
    mergeOperations: z.array(z.object({
      sourceKeys: z.array(z.string()).describe("Keys to merge (will be deleted)"),
      targetKey: z.string().describe("New key for merged entry"),
      mergedContent: z.string().describe("LLM-generated merged content"),
      tags: z.array(z.string()).describe("Tags for merged entry"),
      namespace: z.string().describe("Namespace"),
    })).optional().describe("Merge operations"),
  },
  async ({ deleteKeys, mergeOperations }) => {
    const store = loadStore();
    const health = loadHealth();
    const now = new Date().toISOString();
    const beforeCount = Object.keys(store.entries).length;
    const mergedKeys: string[] = [];
    const deletedKeys: string[] = [];

    if (mergeOperations) {
      for (const op of mergeOperations) {
        store.entries[op.targetKey] = {
          key: op.targetKey, content: op.mergedContent, tags: op.tags, namespace: op.namespace,
          createdAt: now, updatedAt: now, accessCount: 0, lastAccessedAt: null, pinned: false,
        };
        for (const sourceKey of op.sourceKeys) {
          if (sourceKey !== op.targetKey && store.entries[sourceKey]) {
            delete store.entries[sourceKey];
            mergedKeys.push(sourceKey);
          }
        }
      }
    }

    if (deleteKeys) {
      for (const key of deleteKeys) {
        if (store.entries[key]) { delete store.entries[key]; deletedKeys.push(key); }
      }
    }

    const afterCount = Object.keys(store.entries).length;
    saveStore(store);

    health.lastCompactAt = now;
    health.lastCompactEntryCount = afterCount;
    health.compactHistory.push({ timestamp: now, beforeCount, afterCount, mergedKeys, deletedKeys });
    if (health.compactHistory.length > 10) health.compactHistory = health.compactHistory.slice(-10);
    saveHealth(health);

    return textResult(
      `🧹 Compact complete:\n` +
      `- Merged: ${mergeOperations?.length ?? 0} groups (removed ${mergedKeys.length} sources)\n` +
      `- Deleted: ${deletedKeys.length}\n` +
      `- Entries: ${beforeCount} → ${afterCount}\n` +
      `- Storage: ${getStoreSizeKB()}KB`
    );
  }
);

// ── memory_context ───────────────────────────────────────────────────────────
server.tool(
  "memory_context",
  "Memory system overview: stats, namespaces, recent entries, health. Call at session start.",
  {},
  async () => {
    const store = loadStore();
    const config = loadConfig();
    const entries = Object.values(store.entries);

    if (entries.length === 0) {
      return textResult(JSON.stringify({
        status: "empty", totalMemories: 0,
        suggestion: "No memories yet. Consider running import_copilot_sessions() or import_project_knowledge(directory='~/work').",
      }, null, 2));
    }

    const namespaceCounts: Record<string, number> = {};
    for (const e of entries) namespaceCounts[e.namespace] = (namespaceCounts[e.namespace] ?? 0) + 1;

    const tagCounts: Record<string, number> = {};
    for (const e of entries) for (const t of e.tags) tagCounts[t] = (tagCounts[t] ?? 0) + 1;
    const topTags = Object.entries(tagCounts).sort((a, b) => b[1] - a[1]).slice(0, 20)
      .map(([tag, count]) => ({ tag, count }));

    const recent = entries.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, 10)
      .map((e) => ({
        key: e.key, namespace: e.namespace, tags: e.tags.slice(0, 5),
        updatedAt: e.updatedAt, preview: e.content.slice(0, 80), pinned: e.pinned ?? false,
      }));

    const mostAccessed = entries.filter((e) => e.accessCount > 0)
      .sort((a, b) => b.accessCount - a.accessCount).slice(0, 5)
      .map((e) => ({ key: e.key, accessCount: e.accessCount, namespace: e.namespace }));

    const retentionData = analyzeRetention(entries, config);

    return textResult(JSON.stringify({
      status: "ready",
      totalMemories: entries.length,
      namespaces: namespaceCounts,
      topTags,
      recentEntries: recent,
      mostAccessed,
      storageFile: MEMORY_FILE,
      storageSizeKB: getStoreSizeKB(),
      forgettingCurve: retentionSummary(retentionData),
      healthDiagnostics: diagnoseHealth(store, config),
    }, null, 2));
  }
);

// ── memory://stats resource ──────────────────────────────────────────────────
server.resource("memory-stats", "memory://stats", async () => {
  const store = loadStore();
  const entries = Object.values(store.entries);
  return {
    contents: [{
      uri: "memory://stats",
      text: JSON.stringify({
        totalMemories: entries.length,
        namespaces: [...new Set(entries.map((e) => e.namespace))],
        tags: [...new Set(entries.flatMap((e) => e.tags))],
        storageFile: MEMORY_FILE,
        lastUpdated: entries.length > 0 ? entries.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0].updatedAt : null,
      }, null, 2),
      mimeType: "application/json",
    }],
  };
});

// ── Start ────────────────────────────────────────────────────────────────────
async function main() {
  const recoveredKeys = recoverFromWAL();
  if (recoveredKeys.length > 0) {
    console.error(`🔄 WAL recovery on startup: ${recoveredKeys.length} writes replayed`);
  }
  const transport = new StdioServerTransport();
  await server.connect(transport);
  const preset = process.env.TOOL_PRESET ?? "full";
  const toolCount = allowedTools ? allowedTools.size : 11;
  console.error(`🧠 Memory Store MCP server v2.0 running on stdio (preset=${preset}, ${toolCount} tools registered)`);

  // Periodic skill review — checks if enough tool calls have accumulated
  // and spawns a background LLM review if so. Runs every 60 seconds.
  setInterval(() => {
    if (skillTracker.shouldReview()) {
      skillTracker.triggerReview().catch((e) => {
        console.error(`🧠 Periodic skill review failed: ${e.message}`);
      });
    }
  }, 60_000);
}

main().catch((err) => { console.error("Fatal error:", err); process.exit(1); });
