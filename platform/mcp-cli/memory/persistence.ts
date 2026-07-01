/**
 * Persistence layer: Store / Health / WAL / Config
 * Persistence layer: Store / Health / WAL / Config
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import type { MemoryStore, MemoryEntry, PendingWrite, HealthState } from "./types.js";
import { DEFAULT_FORGET_CONFIG, type ForgetConfig } from "./forgetting-curve.js";

// ─── Paths ──────────────────────────────────────────────────────────────────

export const DATA_DIR = process.env.MCP_DATA_DIR || path.join(os.homedir(), ".mcp-local");
export const MEMORY_FILE = path.join(DATA_DIR, "memory-store.json");
export const HEALTH_FILE = path.join(DATA_DIR, "memory-health.json");
export const WAL_FILE = path.join(DATA_DIR, "memory-wal.json");
export const CONFIG_FILE = path.join(DATA_DIR, "memory-config.json");

function ensureDataDir(): void {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  }
}

function writeJsonAtomic(filePath: string, value: unknown): void {
  const tmp = `${filePath}.tmp.${process.pid}.${Date.now()}`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), "utf-8");
  fs.renameSync(tmp, filePath);
}

// If we ever support multi-process, replace _mutateChain with a file lock.
let _mutateChain: Promise<void> = Promise.resolve();

export async function mutateStore(
  fn: (store: MemoryStore) => MemoryStore | void,
): Promise<void> {
  const next = _mutateChain.then(() => {
    const store = loadStore();
    const result = fn(store);
    saveStore(result ?? store);
  });
  _mutateChain = next.catch(() => {});
  return next;
}

// ─── Memory Store ───────────────────────────────────────────────────────────

export function loadStore(): MemoryStore {
  ensureDataDir();
  if (!fs.existsSync(MEMORY_FILE)) {
    return { version: 1, entries: {} };
  }
  try {
    const raw = fs.readFileSync(MEMORY_FILE, "utf-8");
    return JSON.parse(raw) as MemoryStore;
  } catch {
    return { version: 1, entries: {} };
  }
}

export function saveStore(store: MemoryStore): void {
  ensureDataDir();
  writeJsonAtomic(MEMORY_FILE, store);
}

export function getStoreSizeKB(): number {
  try {
    return Math.round((fs.statSync(MEMORY_FILE).size / 1024) * 10) / 10;
  } catch {
    return 0;
  }
}

// ─── Health State ───────────────────────────────────────────────────────────

export function loadHealth(): HealthState {
  ensureDataDir();
  if (!fs.existsSync(HEALTH_FILE)) {
    return { lastCompactAt: null, lastCompactEntryCount: 0, pendingWrites: [], compactHistory: [] };
  }
  try {
    return JSON.parse(fs.readFileSync(HEALTH_FILE, "utf-8")) as HealthState;
  } catch {
    return { lastCompactAt: null, lastCompactEntryCount: 0, pendingWrites: [], compactHistory: [] };
  }
}

export function saveHealth(health: HealthState): void {
  ensureDataDir();
  writeJsonAtomic(HEALTH_FILE, health);
}

// ─── Config (Forgetting Curve Parameters) ──────────────────────────────────

export function loadConfig(): ForgetConfig {
  if (!fs.existsSync(CONFIG_FILE)) return { ...DEFAULT_FORGET_CONFIG };
  try {
    const raw = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8"));
    return { ...DEFAULT_FORGET_CONFIG, ...raw };
  } catch {
    return { ...DEFAULT_FORGET_CONFIG };
  }
}

export function saveConfig(config: ForgetConfig): void {
  ensureDataDir();
  writeJsonAtomic(CONFIG_FILE, config);
}

// ─── Write-Ahead Log (WAL) ──────────────────────────────────────────────────
// Write to WAL before persisting to memory-store.json.
// On server restart, replay any incomplete writes.
// Write the WAL before actually writing memory-store.json, so the server auto-recovers on restart

export function walAppend(entry: PendingWrite): void {
  ensureDataDir();
  let wal: PendingWrite[] = [];
  if (fs.existsSync(WAL_FILE)) {
    try { wal = JSON.parse(fs.readFileSync(WAL_FILE, "utf-8")); } catch { wal = []; }
  }
  wal.push(entry);
  writeJsonAtomic(WAL_FILE, wal);
}

export function walClear(): void {
  if (fs.existsSync(WAL_FILE)) {
    writeJsonAtomic(WAL_FILE, []);
  }
}

export function walRecover(): { recovered: number; entries: PendingWrite[] } {
  if (!fs.existsSync(WAL_FILE)) return { recovered: 0, entries: [] };
  try {
    const wal: PendingWrite[] = JSON.parse(fs.readFileSync(WAL_FILE, "utf-8"));
    return wal.length > 0 ? { recovered: wal.length, entries: wal } : { recovered: 0, entries: [] };
  } catch {
    return { recovered: 0, entries: [] };
  }
}

/** Replay pending WAL writes on startup — re-apply un-flushed writes to the store */
export function recoverFromWAL(): string[] {
  const { recovered, entries } = walRecover();
  if (recovered === 0) return [];

  const store = loadStore();
  const recoveredKeys: string[] = [];

  for (const pending of entries) {
    if (pending.operation === "set") {
      const existing = store.entries[pending.key];
      store.entries[pending.key] = {
        key: pending.key,
        content: pending.content,
        tags: pending.tags,
        namespace: pending.namespace,
        createdAt: existing?.createdAt ?? pending.timestamp,
        updatedAt: pending.timestamp,
        accessCount: existing?.accessCount ?? 0,
        lastAccessedAt: existing?.lastAccessedAt ?? null,
        pinned: existing?.pinned ?? false,
      };
      recoveredKeys.push(`+${pending.key}`);
    } else if (pending.operation === "delete") {
      if (store.entries[pending.key]) {
        delete store.entries[pending.key];
        recoveredKeys.push(`-${pending.key}`);
      }
    }
  }

  if (recoveredKeys.length > 0) {
    saveStore(store);
    walClear();
    const health = loadHealth();
    health.pendingWrites = [];
    saveHealth(health);
    console.error(`🔄 WAL recovery: replayed ${recoveredKeys.length} pending writes: ${recoveredKeys.join(", ")}`);
  }

  return recoveredKeys;
}
