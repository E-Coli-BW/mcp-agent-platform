/**
 * Core type definitions / 核心类型定义
 */

// ─── Memory Entry ────────────────────────────────────────────────────────────

export interface MemoryEntry {
  key: string;
  content: string;
  tags: string[];
  namespace: string;
  createdAt: string;
  updatedAt: string;
  accessCount: number;
  /** Last time this entry was accessed via memory_get / memory_search hit
   *  上次被 memory_get / memory_search 命中的时间 */
  lastAccessedAt: string | null;
  /** Pinned memories are immune to the forgetting curve and never auto-suggested for deletion
   *  标记为 pinned 的记忆永不被遗忘曲线衰减或建议删除 */
  pinned?: boolean;
}

export interface MemoryStore {
  version: number;
  entries: Record<string, MemoryEntry>;
}

// ─── WAL (Write-Ahead Log / 预写日志) ────────────────────────────────────────

export interface PendingWrite {
  key: string;
  content: string;
  tags: string[];
  namespace: string;
  timestamp: string;
  operation: "set" | "delete";
}

// ─── Health & Compaction / 健康状态与整理 ────────────────────────────────────

export interface HealthState {
  lastCompactAt: string | null;
  lastCompactEntryCount: number;
  pendingWrites: PendingWrite[];
  compactHistory: CompactRecord[];
}

export interface CompactRecord {
  timestamp: string;
  beforeCount: number;
  afterCount: number;
  mergedKeys: string[];
  deletedKeys: string[];
}

export interface HealthDiagnostic {
  level: "info" | "warning" | "action-needed";
  message: string;
  suggestion?: string;
}

// ─── Tool Response Helper / 工具响应辅助函数 ─────────────────────────────────

export function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}
