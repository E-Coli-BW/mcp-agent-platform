/**
 * Forgetting Curve
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * Simulates the Ebbinghaus forgetting curve: R = e^(-t/S)
 * Models the Ebbinghaus forgetting curve: R = e^(-t/S)
 *
 *   R = retention (memory retention, 0~1)
 *   t = time since last interaction in days
 *   S = stability (higher = slower decay)
 *
 * Stability is determined by:
 *   - Access count (each access reinforces)
 *   - Updates (content refresh = re-learning)
 *   - Tag weight (bug/pinned are inherently important)
 *   - Pinned flag → retention always = 1.0
 *
 * ── Compared to hardcoded thresholds ──
 *
 *   Old: STALE_DAYS=30, accessCount==0 → stale     (binary decision)
 *   New: retention < 0.3 → suggest compact           (continuous decay)
 *        retention < 0.1 → suggest delete             (natural forgetting)
 *        pinned → retention = 1.0 always              (never forgotten)
 *
 * ── Safety: NO automatic deletion ──
 *
 *   The forgetting curve is ONLY used for search ranking and diagnostics.
 *   No code ever deletes memories automatically. Deletion only happens when
 *   memory_compact_execute is explicitly called with specific keys.
 *   The forgetting curve is used only for search ranking and diagnostic suggestions; no code ever deletes memories automatically.
 *
 *   If >70% of memories are forgotten (e.g., months of inactivity), the system
 *   enters "safe mode" and suggests reviewing + pinning instead of bulk deletion.
 *   If more than 70% of memories are in the forgotten state, the system enters safe mode,
 *   suggesting review and pinning first, and never bulk deletion.
 */

import type { MemoryEntry } from "./types.js";

// ─── Forgetting Curve Parameters ─────────────────────────────────────────────

/** Default parameters, overridable via ~/.mcp-local/memory-config.json
 *  Default parameters, overridable via ~/.mcp-local/memory-config.json */
export interface ForgetConfig {
  /** Master switch: when false, all memories retain 1.0 (no decay).
   *  Master switch: when false, every memory keeps retention = 1.0 (no decay) */
  enabled: boolean;
  /** Base stability in days. An unaccessed memory drops to ~37% retention after this period.
   *  Base stability (days). A never-accessed memory drops to ~37% retention after roughly this many days */
  baseStability: number;
  /** Stability bonus per access, in days */
  accessBonus: number;
  /** Stability bonus per update, in days */
  updateBonus: number;
  /** Retention below this → "fading" status (suggest compact) */
  fadingThreshold: number;
  /** Retention below this → "forgotten" status (suggest delete) */
  forgottenThreshold: number;
  /** Tags that grant an extra stability bonus */
  importantTags: Record<string, number>;
  /** Namespaces that grant an extra stability bonus */
  importantNamespaces: Record<string, number>;
}

export const DEFAULT_FORGET_CONFIG: ForgetConfig = {
  enabled: true,              // master switch; false = disable the forgetting curve
  baseStability: 14,          // drops to 37% after 14 days
  accessBonus: 7,             // +7 days of stability per access
  updateBonus: 14,            // each update equals re-learning
  fadingThreshold: 0.3,       // <30% suggests compaction
  forgottenThreshold: 0.1,    // <10% suggests deletion
  importantTags: {
    "bug": 30,                // bug records are inherently important
    "troubleshooting": 30,
    "architecture": 20,
    "decision": 20,
    "preference": 60,         // user preferences barely decay
    "profile": 60,
    "workspace": 40,          // workspace index is important
    "index": 40,
  },
  importantNamespaces: {
    "preferences": 60,
    "personal": 40,
  },
};

// ─── Core Calculation ───────────────────────────────────────────────────────

/** Compute memory stability S (in days) */
export function computeStability(entry: MemoryEntry, config: ForgetConfig): number {
  let S = config.baseStability;

  // Access count bonus
  S += entry.accessCount * config.accessBonus;

  // Updated = content refreshed (createdAt !== updatedAt means updated)
  // Updated = content was refreshed (createdAt !== updatedAt indicates an update)
  if (entry.createdAt !== entry.updatedAt) {
    S += config.updateBonus;
  }

  // Important tag bonus
  for (const tag of entry.tags) {
    const bonus = config.importantTags[tag.toLowerCase()];
    if (bonus) S += bonus;
  }

  // Important namespace bonus
  const nsBonus = config.importantNamespaces[entry.namespace];
  if (nsBonus) S += nsBonus;

  return S;
}

/** Compute retention R (0~1). Pinned memories always return 1.0.
 *  When config.enabled=false, ALL memories return 1.0 (forgetting disabled).
 *  Compute memory retention R (0~1); pinned memories always return 1.0
 *  When config.enabled=false, all memories return 1.0 (forgetting disabled) */
export function computeRetention(entry: MemoryEntry, config: ForgetConfig, now?: number): number {
  // Global disable: no forgetting
  if (!config.enabled) return 1.0;

  // Pinned memories never forgotten
  if (entry.pinned) return 1.0;

  const currentTime = now ?? Date.now();
  const lastInteraction = entry.lastAccessedAt
    ? Math.max(new Date(entry.updatedAt).getTime(), new Date(entry.lastAccessedAt).getTime())
    : new Date(entry.updatedAt).getTime();

  const daysSinceInteraction = (currentTime - lastInteraction) / (1000 * 60 * 60 * 24);
  const S = computeStability(entry, config);

  // Ebbinghaus: R = e^(-t/S)
  return Math.exp(-daysSinceInteraction / S);
}

// ─── Batch Analysis ─────────────────────────────────────────────────────────

export interface RetentionInfo {
  key: string;
  namespace: string;
  retention: number;
  stability: number;
  daysSinceInteraction: number;
  status: "strong" | "fading" | "forgotten";
  pinned: boolean;
  tags: string[];
}

/** Compute retention for all memories, return sorted list (weakest first)
 *  Compute retention for all memories and return a sorted list (weakest first) */
export function analyzeRetention(
  entries: MemoryEntry[],
  config: ForgetConfig,
  now?: number
): RetentionInfo[] {
  const currentTime = now ?? Date.now();

  return entries.map((entry) => {
    const retention = computeRetention(entry, config, currentTime);
    const stability = computeStability(entry, config);
    const lastInteraction = entry.lastAccessedAt
      ? Math.max(new Date(entry.updatedAt).getTime(), new Date(entry.lastAccessedAt).getTime())
      : new Date(entry.updatedAt).getTime();
    const daysSinceInteraction = (currentTime - lastInteraction) / (1000 * 60 * 60 * 24);

    let status: "strong" | "fading" | "forgotten";
    if (entry.pinned || retention >= config.fadingThreshold) {
      status = "strong";
    } else if (retention >= config.forgottenThreshold) {
      status = "fading";
    } else {
      status = "forgotten";
    }

    return {
      key: entry.key,
      namespace: entry.namespace,
      retention: Math.round(retention * 1000) / 1000,
      stability: Math.round(stability * 10) / 10,
      daysSinceInteraction: Math.round(daysSinceInteraction * 10) / 10,
      status,
      pinned: entry.pinned ?? false,
      tags: entry.tags,
    };
  }).sort((a, b) => a.retention - b.retention); // weakest first
}
