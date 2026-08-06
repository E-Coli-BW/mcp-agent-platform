/**
 * Health diagnostics: smart analysis based on forgetting curve (replaces hardcoded thresholds)
 * 健康诊断: 基于遗忘曲线的智能分析（替代 hardcoded thresholds）
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
  // 遗忘曲线关闭时，跳过所有 retention 相关诊断
  if (!config.enabled) {
    diagnostics.push({
      level: "info",
      message: `ℹ️ 遗忘曲线已关闭 (enabled=false)，所有 ${entries.length} 条记忆保持同等搜索权重`,
    });

    // Still show storage size info
    const sizeKB = getStoreSizeKB();
    if (sizeKB > 500) {
      diagnostics.push({
        level: "warning",
        message: `💾 存储大小 ${sizeKB}KB`,
        suggestion: `如记忆数超过预期，可手动清理不再需要的条目`,
      });
    }

    return diagnostics;
  }

  // 遗忘曲线分析
  const retentionData = analyzeRetention(entries, config, now);
  const forgotten = retentionData.filter((r) => r.status === "forgotten");
  const fading = retentionData.filter((r) => r.status === "fading");

  // Safety valve: if >70% of memories are forgotten, it's likely due to long inactivity
  // rather than truly stale data. Don't suggest bulk deletion — prompt review instead.
  // 安全阀: 如果超过 70% 的记忆都 forgotten，说明可能是长时间未使用而非真正该删
  // 此时不建议批量删除，而是提示用户先审查
  const forgottenRatio = entries.length > 0 ? forgotten.length / entries.length : 0;

  // 1. Forgotten 记忆（retention < forgottenThreshold）
  if (forgottenRatio > 0.7 && forgotten.length > 5) {
    diagnostics.push({
      level: "info",
      message: `⚠️ ${forgotten.length}/${entries.length} 条记忆 (${Math.round(forgottenRatio * 100)}%) 处于 forgotten 状态 — 可能是长时间未使用导致，不建议批量删除`,
      suggestion: `建议先用 memory_list() 浏览，对重要记忆执行 memory_pin(key) 或 memory_get(key) 来强化，再考虑清理`,
    });
  } else if (forgotten.length > 0) {
    diagnostics.push({
      level: "action-needed",
      message: `🧊 ${forgotten.length} 条记忆已接近遗忘 (retention < ${config.forgottenThreshold})`,
      suggestion: `建议调用 memory_compact_plan() 查看详情，可删除或合并: ${forgotten.slice(0, 3).map((r) => `${r.key}(R=${r.retention})`).join(", ")}${forgotten.length > 3 ? "..." : ""}`,
    });
  }

  // 2. Fading 记忆（retention < fadingThreshold）
  if (fading.length > 3) {
    diagnostics.push({
      level: "warning",
      message: `⏳ ${fading.length} 条记忆正在衰减 (retention ${config.forgottenThreshold}~${config.fadingThreshold})`,
      suggestion: `访问或更新它们可以重新强化记忆`,
    });
  }

  // 3. 存储大小
  const sizeKB = getStoreSizeKB();
  if (sizeKB > 300) {
    diagnostics.push({
      level: "action-needed",
      message: `💾 存储大小 ${sizeKB}KB，建议整理`,
      suggestion: `优先删除 forgotten 状态的记忆`,
    });
  } else if (sizeKB > 200) {
    diagnostics.push({
      level: "warning",
      message: `💾 存储大小 ${sizeKB}KB，接近推荐上限`,
    });
  }

  // 4. Namespace 集中度（超过 40% 的条目集中在一个 namespace）
  const nsCounts: Record<string, number> = {};
  for (const e of entries) {
    nsCounts[e.namespace] = (nsCounts[e.namespace] ?? 0) + 1;
  }
  for (const [ns, count] of Object.entries(nsCounts)) {
    if (count > entries.length * 0.4 && count > 15) {
      diagnostics.push({
        level: "warning",
        message: `📂 Namespace "${ns}" 占比 ${Math.round((count / entries.length) * 100)}% (${count}/${entries.length})`,
        suggestion: `考虑合并 "${ns}" 中相似的记忆`,
      });
    }
  }

  // 5. Time since last compact (dynamic: more memories → more frequent compaction needed)
  // 距上次整理时间（用遗忘曲线思维：随记忆数增长，整理频率应更高）
  if (health.lastCompactAt) {
    const daysSinceCompact = (now - new Date(health.lastCompactAt).getTime()) / (1000 * 60 * 60 * 24);
    // Dynamic threshold: more memories → need more frequent compaction
    // 动态阈值：记忆越多，需要越频繁整理
    const compactInterval = Math.max(7, 30 - entries.length * 0.2);
    if (daysSinceCompact > compactInterval) {
      diagnostics.push({
        level: "warning",
        message: `⏰ 距上次整理已 ${Math.round(daysSinceCompact)} 天 (建议间隔: ${Math.round(compactInterval)} 天)`,
        suggestion: `调用 memory_compact_plan() 查看整理方案`,
      });
    }
  } else if (entries.length > 20) {
    diagnostics.push({
      level: "info",
      message: `ℹ️ 从未执行过整理，当前 ${entries.length} 条记忆`,
    });
  }

  // 6. No pinned memories hint / 无 pinned 记忆提示
  const pinnedCount = entries.filter((e) => e.pinned).length;
  if (pinnedCount === 0 && entries.length > 10) {
    diagnostics.push({
      level: "info",
      message: `📌 当前无 pinned 记忆。重要的记忆（如 workspace 索引、个人档案）建议设为 pinned 以防被遗忘`,
      suggestion: `调用 memory_set(key, content, { pinned: true }) 或 memory_pin(key)`,
    });
  }

  // 7. WAL check / WAL 检查
  const walResult = walRecover();
  if (walResult.recovered > 0) {
    diagnostics.push({
      level: "action-needed",
      message: `🔄 发现 ${walResult.recovered} 条未落盘的 WAL 记录`,
      suggestion: `这些记录已在 server 启动时自动恢复`,
    });
  }

  return diagnostics;
}

/** Generate forgetting curve distribution summary (for memory_context)
 *  生成遗忘曲线分布摘要（用于 memory_context） */
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
