/**
 * Core type definitions
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
   *  Timestamp of the last memory_get / memory_search hit */
  lastAccessedAt: string | null;
  /** Pinned memories are immune to the forgetting curve and never auto-suggested for deletion
   *  Memories marked pinned are never decayed or suggested for deletion by the forgetting curve */
  pinned?: boolean;
}

export interface MemoryStore {
  version: number;
  entries: Record<string, MemoryEntry>;
}

// ─── WAL (Write-Ahead Log) ──────────────────────────────────────────────────

export interface PendingWrite {
  key: string;
  content: string;
  tags: string[];
  namespace: string;
  timestamp: string;
  operation: "set" | "delete";
}

// ─── Health & Compaction ────────────────────────────────────────────────────

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

// ─── Tool Response Helper ───────────────────────────────────────────────────

export function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}
