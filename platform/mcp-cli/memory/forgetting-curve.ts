/**
 * Forgetting Curve / 遗忘曲线
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * Simulates the Ebbinghaus forgetting curve: R = e^(-t/S)
 * 模拟 Ebbinghaus 遗忘曲线: R = e^(-t/S)
 *
 *   R = retention (memory retention / 记忆保留度, 0~1)
 *   t = time since last interaction in days / 距上次交互的时间 (天)
 *   S = stability (higher = slower decay / 越高衰减越慢)
 *
 * Stability is determined by / Stability 由以下因素决定:
 *   - Access count (each access reinforces / 每次访问 +强化)
 *   - Updates (content refresh = re-learning / 内容刷新 = 重学)
 *   - Tag weight (bug/pinned are inherently important / bug/pinned 类天然重要)
 *   - Pinned flag → retention always = 1.0 / pinned 标记 → retention 永远 = 1.0
 *
 * ── Compared to hardcoded thresholds / 与 hardcoded threshold 的区别 ──
 *
 *   Old: STALE_DAYS=30, accessCount==0 → stale     (binary / 二元判断)
 *   New: retention < 0.3 → suggest compact           (continuous decay / 连续衰减)
 *        retention < 0.1 → suggest delete             (natural forgetting / 自然遗忘)
 *        pinned → retention = 1.0 always              (never forgotten / 永不遗忘)
 *
 * ── Safety: NO automatic deletion / 安全: 绝不自动删除 ──
 *
 *   The forgetting curve is ONLY used for search ranking and diagnostics.
 *   No code ever deletes memories automatically. Deletion only happens when
 *   memory_compact_execute is explicitly called with specific keys.
 *   遗忘曲线仅用于搜索排序和诊断建议，没有任何代码会自动删除记忆。
 *
 *   If >70% of memories are forgotten (e.g., months of inactivity), the system
 *   enters "safe mode" and suggests reviewing + pinning instead of bulk deletion.
 *   如果超过 70% 的记忆都处于 forgotten 状态，系统进入安全模式，
 *   建议先审查和 pin，不会建议批量删除。
 */

import type { MemoryEntry } from "./types.js";

// ─── Forgetting Curve Parameters ─────────────────────────────────────────────

/** Default parameters, overridable via ~/.mcp-local/memory-config.json
 *  默认参数，可通过 ~/.mcp-local/memory-config.json 覆盖 */
export interface ForgetConfig {
  /** Master switch: when false, all memories retain 1.0 (no decay).
   *  总开关: 设为 false 时所有记忆 retention 恒为 1.0（不衰减） */
  enabled: boolean;
  /** Base stability in days. An unaccessed memory drops to ~37% retention after this period.
   *  基础稳定性 (天)。一条从未被访问的记忆, 约经过此天数后 retention 降到 ~37% */
  baseStability: number;
  /** Stability bonus per access in days / 每次访问增加的稳定性 (天) */
  accessBonus: number;
  /** Stability bonus per update in days / 每次更新增加的稳定性 (天) */
  updateBonus: number;
  /** Retention below this → "fading" status (suggest compact) / 低于此值标记为 "fading"（建议整理） */
  fadingThreshold: number;
  /** Retention below this → "forgotten" status (suggest delete) / 低于此值标记为 "forgotten"（建议删除） */
  forgottenThreshold: number;
  /** Tags that grant extra stability bonus / 带有这些 tag 的记忆获得额外稳定性加成 */
  importantTags: Record<string, number>;
  /** Namespaces that grant extra stability bonus / 带有这些 namespace 的记忆获得额外稳定性加成 */
  importantNamespaces: Record<string, number>;
}

export const DEFAULT_FORGET_CONFIG: ForgetConfig = {
  enabled: true,              // 总开关，false = 关闭遗忘曲线
  baseStability: 14,          // 14天后降到 37%
  accessBonus: 7,             // 每次访问 +7天稳定性
  updateBonus: 14,            // 每次更新等于重新学习
  fadingThreshold: 0.3,       // <30% 建议整理
  forgottenThreshold: 0.1,    // <10% 建议删除
  importantTags: {
    "bug": 30,                // bug 记录天然重要
    "troubleshooting": 30,
    "architecture": 20,
    "decision": 20,
    "preference": 60,         // 用户偏好几乎不衰减
    "profile": 60,
    "workspace": 40,          // workspace 索引重要
    "index": 40,
  },
  importantNamespaces: {
    "preferences": 60,
    "personal": 40,
  },
};

// ─── Core Calculation / 核心计算 ─────────────────────────────────────────────

/** Compute memory stability S (in days) / 计算记忆的稳定性 S（天） */
export function computeStability(entry: MemoryEntry, config: ForgetConfig): number {
  let S = config.baseStability;

  // Access count bonus / 访问次数加成
  S += entry.accessCount * config.accessBonus;

  // Updated = content refreshed (createdAt !== updatedAt means updated)
  // 被更新过 = 内容被刷新（createdAt !== updatedAt 说明有更新）
  if (entry.createdAt !== entry.updatedAt) {
    S += config.updateBonus;
  }

  // Important tag bonus / 重要 tag 加成
  for (const tag of entry.tags) {
    const bonus = config.importantTags[tag.toLowerCase()];
    if (bonus) S += bonus;
  }

  // Important namespace bonus / 重要 namespace 加成
  const nsBonus = config.importantNamespaces[entry.namespace];
  if (nsBonus) S += nsBonus;

  return S;
}

/** Compute retention R (0~1). Pinned memories always return 1.0.
 *  When config.enabled=false, ALL memories return 1.0 (forgetting disabled).
 *  计算记忆保留度 R (0~1)，pinned 的记忆永远返回 1.0
 *  当 config.enabled=false 时，所有记忆返回 1.0（关闭遗忘） */
export function computeRetention(entry: MemoryEntry, config: ForgetConfig, now?: number): number {
  // Global disable: no forgetting / 全局禁用遗忘曲线
  if (!config.enabled) return 1.0;

  // Pinned memories never forgotten / pinned 记忆永不遗忘
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

// ─── Batch Analysis / 批量分析 ───────────────────────────────────────────────

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
 *  对所有记忆计算 retention，返回排序后的列表（最弱的排前面） */
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
  }).sort((a, b) => a.retention - b.retention); // 最弱的排前面
}
