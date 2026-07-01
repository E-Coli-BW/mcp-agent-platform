/**
 * Health diagnostics: smart analysis based on forgetting curve (replaces hardcoded thresholds)
 * Health diagnostics: forgetting-curve-based intelligent analysis (replaces hardcoded thresholds)
 */

import type { MemoryStore, HealthDiagnostic } from "./types.js";
import { analyzeRetention, type ForgetConfig, type RetentionInfo } from "./forgetting-curve.js";
import { loadHealth, getStoreSizeKB, MEMORY_FILE, walRecover } from "./persistence.js";

// ─── Health Diagnostics ──────────────────────────────────────────────────────

export function diagnoseHealth(store: MemoryStore, config: ForgetConfig): HealthDiagnostic[] {
  const diagnostics: HealthDiagnostic[] = [];
  const entries = Object.values(store.entries);
  const health = loadHealth();
  const now = Date.now();

  // When forgetting curve is disabled, skip all retention-based diagnostics
  // When the forgetting curve is disabled, skip all retention-related diagnostics
  if (!config.enabled) {
    diagnostics.push({
      level: "info",
      message: `ℹ️ Forgetting curve is disabled (enabled=false); all ${entries.length} memories keep equal search weight`,
    });

    // Still show storage size info
    const sizeKB = getStoreSizeKB();
    if (sizeKB > 500) {
      diagnostics.push({
        level: "warning",
        message: `💾 Storage size ${sizeKB}KB`,
        suggestion: `If the memory count exceeds expectations, manually clean up entries you no longer need`,
      });
    }

    return diagnostics;
  }

  // Forgetting-curve analysis
  const retentionData = analyzeRetention(entries, config, now);
  const forgotten = retentionData.filter((r) => r.status === "forgotten");
  const fading = retentionData.filter((r) => r.status === "fading");

  // Safety valve: if >70% of memories are forgotten, it's likely due to long inactivity
  // rather than truly stale data. Don't suggest bulk deletion — prompt review instead.
  // Safety valve: if more than 70% of memories are forgotten, it likely means long disuse rather than that they truly should be deleted
  // In that case, don't suggest bulk deletion; prompt the user to review first
  const forgottenRatio = entries.length > 0 ? forgotten.length / entries.length : 0;

  // 1. Forgotten memories (retention < forgottenThreshold)
  if (forgottenRatio > 0.7 && forgotten.length > 5) {
    diagnostics.push({
      level: "info",
      message: `⚠️ ${forgotten.length}/${entries.length} memories (${Math.round(forgottenRatio * 100)}%) are in the forgotten state — likely due to long disuse; bulk deletion is not advised`,
      suggestion: `First browse with memory_list(), reinforce important memories with memory_pin(key) or memory_get(key), then consider cleaning up`,
    });
  } else if (forgotten.length > 0) {
    diagnostics.push({
      level: "action-needed",
      message: `🧊 ${forgotten.length} memories are nearly forgotten (retention < ${config.forgottenThreshold})`,
      suggestion: `Call memory_compact_plan() to see details; you can delete or merge: ${forgotten.slice(0, 3).map((r) => `${r.key}(R=${r.retention})`).join(", ")}${forgotten.length > 3 ? "..." : ""}`,
    });
  }

  // 2. Fading memories (retention < fadingThreshold)
  if (fading.length > 3) {
    diagnostics.push({
      level: "warning",
      message: `⏳ ${fading.length} memories are fading (retention ${config.forgottenThreshold}~${config.fadingThreshold})`,
      suggestion: `Accessing or updating them re-reinforces the memory`,
    });
  }

  // 3. Storage size
  const sizeKB = getStoreSizeKB();
  if (sizeKB > 300) {
    diagnostics.push({
      level: "action-needed",
      message: `💾 Storage size ${sizeKB}KB, compaction suggested`,
      suggestion: `Delete memories in the forgotten state first`,
    });
  } else if (sizeKB > 200) {
    diagnostics.push({
      level: "warning",
      message: `💾 Storage size ${sizeKB}KB, approaching the recommended limit`,
    });
  }

  // 4. Namespace concentration (more than 40% of entries in a single namespace)
  const nsCounts: Record<string, number> = {};
  for (const e of entries) {
    nsCounts[e.namespace] = (nsCounts[e.namespace] ?? 0) + 1;
  }
  for (const [ns, count] of Object.entries(nsCounts)) {
    if (count > entries.length * 0.4 && count > 15) {
      diagnostics.push({
        level: "warning",
        message: `📂 Namespace "${ns}" accounts for ${Math.round((count / entries.length) * 100)}% (${count}/${entries.length})`,
        suggestion: `Consider merging similar memories in "${ns}"`,
      });
    }
  }

  // 5. Time since last compact (dynamic: more memories → more frequent compaction needed)
  // Time since last compaction (forgetting-curve thinking: as memory count grows, compaction should be more frequent)
  if (health.lastCompactAt) {
    const daysSinceCompact = (now - new Date(health.lastCompactAt).getTime()) / (1000 * 60 * 60 * 24);
    // Dynamic threshold: more memories → need more frequent compaction
    // Dynamic threshold: the more memories, the more frequently compaction is needed
    const compactInterval = Math.max(7, 30 - entries.length * 0.2);
    if (daysSinceCompact > compactInterval) {
      diagnostics.push({
        level: "warning",
        message: `⏰ ${Math.round(daysSinceCompact)} days since last compaction (suggested interval: ${Math.round(compactInterval)} days)`,
        suggestion: `Call memory_compact_plan() to see the compaction plan`,
      });
    }
  } else if (entries.length > 20) {
    diagnostics.push({
      level: "info",
      message: `ℹ️ Compaction has never run; currently ${entries.length} memories`,
    });
  }

  // 6. No pinned memories hint
  const pinnedCount = entries.filter((e) => e.pinned).length;
  if (pinnedCount === 0 && entries.length > 10) {
    diagnostics.push({
      level: "info",
      message: `📌 No pinned memories currently. Important memories (e.g. the workspace index or personal profile) should be pinned to avoid being forgotten`,
      suggestion: `Call memory_set(key, content, { pinned: true }) or memory_pin(key)`,
    });
  }

  // 7. WAL check
  const walResult = walRecover();
  if (walResult.recovered > 0) {
    diagnostics.push({
      level: "action-needed",
      message: `🔄 Found ${walResult.recovered} un-flushed WAL records`,
      suggestion: `These records were auto-recovered at server startup`,
    });
  }

  return diagnostics;
}

/** Generate forgetting curve distribution summary (for memory_context)
 *  Generate a forgetting-curve distribution summary (used by memory_context) */
export function retentionSummary(entries: RetentionInfo[]): {
  strong: number;
  fading: number;
  forgotten: number;
  pinned: number;
  avgRetention: number;
} {
  const pinned = entries.filter((r) => r.pinned).length;
  const strong = entries.filter((r) => r.status === "strong").length;
  const fading = entries.filter((r) => r.status === "fading").length;
  const forgotten = entries.filter((r) => r.status === "forgotten").length;
  const avgRetention = entries.length > 0
    ? Math.round((entries.reduce((s, r) => s + r.retention, 0) / entries.length) * 1000) / 1000
    : 0;

  return { strong, fading, forgotten, pinned, avgRetention };
}
