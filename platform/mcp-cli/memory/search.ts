/**
 * Search engine: TF-IDF keyword matching + forgetting curve weighting
 * 搜索引擎: TF-IDF 关键词匹配 + 遗忘曲线加权
 */

import type { MemoryEntry } from "./types.js";
import { computeRetention, type ForgetConfig } from "./forgetting-curve.js";

// ─── Tokenizer / 分词器 ──────────────────────────────────────────────────────

/** Split on non-word chars, lowercase, filter short tokens
 *  按非词字符分割，转小写，过滤短 token */
export function tokenize(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((t) => t.length > 1);
}

/** Compute term frequency map / 计算词频映射 */
export function termFrequency(tokens: string[]): Map<string, number> {
  const tf = new Map<string, number>();
  for (const t of tokens) {
    tf.set(t, (tf.get(t) ?? 0) + 1);
  }
  return tf;
}

// ─── Relevance Scoring / 相关度评分 ──────────────────────────────────────────

/**
 * Score = textRelevance × retentionBoost
 *
 * textRelevance: TF overlap + prefix matching + tag matching (same as before)
 * retentionBoost: 0.5 + 0.5 * retention
 *   Decayed memories are demoted but never completely hidden.
 *   衰减的记忆降权但不会完全消失。
 *
 * Examples / 示例:
 *   - retention=1.0 (just used / pinned) → boost=1.0 (no effect / 不影响)
 *   - retention=0.5 → boost=0.75
 *   - retention=0.1 → boost=0.55
 *   - retention=0.0 → boost=0.50 (at most halved / 最多降一半)
 */
export function relevanceScore(
  entry: MemoryEntry,
  queryTokens: string[],
  queryTags: string[],
  config?: ForgetConfig,
): number {
  const entryTokens = tokenize(entry.content + " " + entry.key);
  const entryTf = termFrequency(entryTokens);

  // Term overlap score
  let termScore = 0;
  for (const qt of queryTokens) {
    if (entryTf.has(qt)) {
      termScore += entryTf.get(qt)!;
    }
    // Partial/prefix matching bonus
    for (const [et, count] of entryTf) {
      if (et.startsWith(qt) || qt.startsWith(et)) {
        termScore += count * 0.5;
      }
    }
  }
  // Normalize by entry length to avoid bias toward long entries
  termScore = entryTokens.length > 0 ? termScore / Math.sqrt(entryTokens.length) : 0;

  // Tag match score
  let tagScore = 0;
  if (queryTags.length > 0) {
    const entryTagSet = new Set(entry.tags.map((t) => t.toLowerCase()));
    for (const qt of queryTags) {
      if (entryTagSet.has(qt.toLowerCase())) {
        tagScore += 2; // tags are strong signals
      }
    }
  }

  const textRelevance = termScore + tagScore;

  // Retention boost (if config provided)
  if (config) {
    const retention = computeRetention(entry, config);
    const boost = 0.5 + 0.5 * retention;
    return textRelevance * boost;
  }

  return textRelevance;
}
