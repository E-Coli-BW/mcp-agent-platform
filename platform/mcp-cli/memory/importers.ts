/**
 * Import tools: Copilot Sessions + Project Knowledge
 * Import tools: Copilot Sessions + Project Knowledge
 * (Extracted from the original monolithic memory-server.ts)
 * (extracted from the original monolithic memory-server.ts)
 */

import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import type { MemoryStore, MemoryEntry } from "./types.js";

// ─── Platform-aware VS Code storage path ───────────────────────────────────
function getVSCodeStorageDir(): string {
  switch (process.platform) {
    case "win32":
      return path.join(process.env.APPDATA ?? path.join(os.homedir(), "AppData", "Roaming"), "Code", "User", "workspaceStorage");
    case "linux":
      return path.join(process.env.XDG_CONFIG_HOME ?? path.join(os.homedir(), ".config"), "Code", "User", "workspaceStorage");
    case "darwin":
    default:
      return path.join(os.homedir(), "Library", "Application Support", "Code", "User", "workspaceStorage");
  }
}

// ─── Import Copilot Sessions ────────────────────────────────────────────────

export function importCopilotSessions(
  store: MemoryStore,
  reimport: boolean,
): { imported: number; skipped: number } {
  const now = new Date().toISOString();
  let imported = 0;
  let skipped = 0;

  const copilotDir = path.join(os.homedir(), ".copilot");
  const sessionDir = path.join(copilotDir, "session-state");
  const configFile = path.join(copilotDir, "config.json");

  // ── Global config ──
  if (fs.existsSync(configFile)) {
    try {
      const config = JSON.parse(fs.readFileSync(configFile, "utf-8"));
      const configKey = "copilot-cli-config";
      if (reimport || !store.entries[configKey]) {
        store.entries[configKey] = makeEntry(configKey, JSON.stringify({
          trustedFolders: config.trusted_folders ?? [],
          user: config.last_logged_in_user ?? null,
          firstLaunch: config.firstLaunchAt ?? null,
        }, null, 2), ["copilot", "config", "auto-imported"], "copilot-sessions", now, store.entries[configKey]);
        imported++;
      }
    } catch { /* ignore */ }
  }

  // ── Command history ──
  const histFile = path.join(copilotDir, "command-history-state.json");
  if (fs.existsSync(histFile)) {
    try {
      const hist = JSON.parse(fs.readFileSync(histFile, "utf-8"));
      const histKey = "copilot-cli-command-history";
      if (reimport || !store.entries[histKey]) {
        store.entries[histKey] = makeEntry(histKey, JSON.stringify(hist.commandHistory ?? [], null, 2),
          ["copilot", "history", "commands", "auto-imported"], "copilot-sessions", now, store.entries[histKey]);
        imported++;
      }
    } catch { /* ignore */ }
  }

  // ── CLI sessions ──
  if (fs.existsSync(sessionDir)) {
    let sessionDirs: fs.Dirent[];
    try { sessionDirs = fs.readdirSync(sessionDir, { withFileTypes: true }); } catch { sessionDirs = []; }

    for (const entry of sessionDirs) {
      if (!entry.isDirectory()) continue;
      const sessionId = entry.name;
      const sessionKey = `copilot-session-${sessionId}`;

      if (!reimport && store.entries[sessionKey]) { skipped++; continue; }

      const workspaceFile = path.join(sessionDir, sessionId, "workspace.yaml");
      const checkpointIndex = path.join(sessionDir, sessionId, "checkpoints", "index.md");

      const sessionContent: Record<string, unknown> = { sessionId, source: "copilot-cli" };

      if (fs.existsSync(workspaceFile)) {
        try {
          const yaml = fs.readFileSync(workspaceFile, "utf-8");
          for (const line of yaml.split("\n")) {
            const match = line.match(/^(\w+):\s*(.+)$/);
            if (match) sessionContent[match[1]] = match[2];
          }
        } catch { /* ignore */ }
      }

      if (fs.existsSync(checkpointIndex)) {
        try {
          const md = fs.readFileSync(checkpointIndex, "utf-8");
          const rows = md.split("\n").filter((l) => l.startsWith("|") && !l.startsWith("| #") && !l.startsWith("|--"));
          if (rows.length > 0) {
            sessionContent.checkpoints = rows.map((row) => {
              const cols = row.split("|").map((c) => c.trim()).filter(Boolean);
              return { number: cols[0], title: cols[1], file: cols[2] };
            });
            const checkpointDir = path.join(sessionDir, sessionId, "checkpoints");
            const cpFiles = fs.readdirSync(checkpointDir).filter((f) => f.endsWith(".md") && f !== "index.md");
            const summaries: string[] = [];
            for (const cpFile of cpFiles.slice(0, 10)) {
              try { summaries.push(fs.readFileSync(path.join(checkpointDir, cpFile), "utf-8").slice(0, 500)); } catch { /* ignore */ }
            }
            if (summaries.length > 0) sessionContent.summaries = summaries;
          }
        } catch { /* ignore */ }
      }

      store.entries[sessionKey] = makeEntry(sessionKey, JSON.stringify(sessionContent, null, 2),
        ["copilot", "copilot-cli", "session", "workspace", "auto-imported", ...(sessionContent.cwd ? [String(sessionContent.cwd).split(/[/\\]/).pop() ?? ""] : [])],
        "copilot-sessions", now, store.entries[sessionKey],
        (sessionContent.created_at as string) ?? undefined);
      imported++;
    }
  }

  // ── VS Code Chat sessions ──
  const vscodeStorageDir = getVSCodeStorageDir();
  if (fs.existsSync(vscodeStorageDir)) {
    let storageDirs: fs.Dirent[];
    try { storageDirs = fs.readdirSync(vscodeStorageDir, { withFileTypes: true }); } catch { storageDirs = []; }

    for (const storageEntry of storageDirs) {
      if (!storageEntry.isDirectory()) continue;
      const storagePath = path.join(vscodeStorageDir, storageEntry.name);
      const chatDir = path.join(storagePath, "chatSessions");
      const wsFile = path.join(storagePath, "workspace.json");

      if (!fs.existsSync(chatDir)) continue;

      let workspacePath = "";
      let workspaceName = "";
      if (fs.existsSync(wsFile)) {
        try {
          const ws = JSON.parse(fs.readFileSync(wsFile, "utf-8"));
          const raw = ws.folder || ws.workspace || ws.configuration || "";
          // file:///C:/... → C:/... on Windows; file:///home/... → /home/... on Unix
          const decoded = decodeURIComponent(raw.replace(/^file:\/\/\//, process.platform === "win32" ? "" : "/").replace(/^file:\/\//, ""));
          workspacePath = decoded;
          workspaceName = workspacePath.split(/[/\\]/).filter(Boolean).pop() ?? storageEntry.name;
        } catch { /* ignore */ }
      }

      let chatFiles: string[];
      try { chatFiles = fs.readdirSync(chatDir).filter((f) => f.endsWith(".json")); } catch { continue; }

      for (const chatFile of chatFiles) {
        const sessionId = chatFile.replace(".json", "");
        const memKey = `vscode-chat-${workspaceName}-${sessionId.slice(0, 8)}`;
        if (!reimport && store.entries[memKey]) { skipped++; continue; }

        try {
          const raw = fs.readFileSync(path.join(chatDir, chatFile), "utf-8");
          const session = JSON.parse(raw);
          const requests = session.requests ?? session.turns ?? [];
          const userMessages: string[] = [];
          for (const req of requests) {
            const msg = req.message ?? req.text ?? "";
            const text = typeof msg === "string" ? msg : (msg.text ?? "");
            if (text) userMessages.push(text.slice(0, 300));
          }
          if (userMessages.length === 0) continue;

          const chatContent = {
            source: "vscode-copilot-chat", sessionId, workspacePath, workspaceName,
            createdAt: session.creationDate ?? null, lastMessage: session.lastMessageDate ?? null,
            messageCount: userMessages.length, userMessages: userMessages.slice(0, 20),
          };

          const tags: string[] = ["copilot", "vscode-chat", "session", "auto-imported", workspaceName];
          const allText = userMessages.join(" ").toLowerCase();
          if (allText.includes("bug") || allText.includes("fix") || allText.includes("error")) tags.push("debugging");
          if (allText.includes("test")) tags.push("testing");
          if (allText.includes("refactor")) tags.push("refactoring");
          if (allText.includes("build") || allText.includes("deploy")) tags.push("build");

          store.entries[memKey] = makeEntry(memKey, JSON.stringify(chatContent, null, 2),
            [...new Set(tags)], "copilot-sessions", now, store.entries[memKey], session.creationDate ?? undefined);
          imported++;
        } catch { /* ignore */ }
      }
    }
  }

  return { imported, skipped };
}

// ─── Import Project Knowledge ───────────────────────────────────────────────

export function importProjectKnowledge(
  store: MemoryStore,
  dir: string,
  depth: number,
  reimport: boolean,
): { imported: number; skipped: number } {
  const now = new Date().toISOString();
  let imported = 0;
  let skipped = 0;

  const knowledgeFiles = [
    "AGENTS.md", ".github/copilot-instructions.md", "copilot-instructions.md",
    ".copilot-instructions.md", "CLAUDE.md", "CONVENTIONS.md",
  ];
  const skipDirs = new Set([
    "node_modules", ".git", ".svn", "dist", "build", "target",
    ".gradle", ".idea", ".vscode", "__pycache__", "vendor",
  ]);

  function scanDir(currentDir: string, currentDepth: number): void {
    if (currentDepth > depth) return;
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(currentDir, { withFileTypes: true }); } catch { return; }

    for (const knowledgeFile of knowledgeFiles) {
      const fullPath = path.join(currentDir, knowledgeFile);
      if (!fs.existsSync(fullPath)) continue;

      const projectName = path.basename(currentDir);
      const memKey = `project-knowledge-${projectName}-${knowledgeFile.replace(/[/.]/g, "-")}`;
      if (!reimport && store.entries[memKey]) { skipped++; continue; }

      try {
        const stat = fs.statSync(fullPath);
        if (stat.size > 500_000) continue;
        const content = fs.readFileSync(fullPath, "utf-8");

        const sections: Record<string, string> = {};
        let currentSection = "overview";
        for (const line of content.split("\n")) {
          const headingMatch = line.match(/^#{1,3}\s+(.+)/);
          if (headingMatch) currentSection = headingMatch[1].trim().toLowerCase().replace(/[^a-z0-9]+/g, "-");
          if (!sections[currentSection]) sections[currentSection] = "";
          sections[currentSection] += line + "\n";
        }

        const tags: string[] = ["project", "knowledge", "auto-imported", projectName];
        const lc = content.toLowerCase();
        if (lc.includes("build") || lc.includes("maven") || lc.includes("gradle") || lc.includes("npm")) tags.push("build");
        if (lc.includes("test")) tags.push("testing");
        if (lc.includes("architecture") || lc.includes("module")) tags.push("architecture");
        if (lc.includes("deploy") || lc.includes("infrastructure")) tags.push("deploy");
        if (lc.includes("api") || lc.includes("openapi")) tags.push("api");
        if (lc.includes("spring") || lc.includes("java")) tags.push("java");
        if (lc.includes("python") || lc.includes("poetry")) tags.push("python");
        if (lc.includes("typescript") || lc.includes("node")) tags.push("typescript");
        if (lc.includes("docker") || lc.includes("container")) tags.push("docker");
        if (lc.includes("aws") || lc.includes("lambda") || lc.includes("cloudformation")) tags.push("aws");

        store.entries[memKey] = makeEntry(memKey, JSON.stringify({
          projectName, projectPath: currentDir, sourceFile: knowledgeFile,
          fullContent: content.slice(0, 5000), sections: Object.keys(sections),
          fileModified: stat.mtime.toISOString(),
        }, null, 2), [...new Set(tags)], "project-knowledge", now, store.entries[memKey]);
        imported++;
      } catch { /* ignore */ }
    }

    if (currentDepth < depth) {
      for (const entry of entries) {
        if (!entry.isDirectory() || skipDirs.has(entry.name) || entry.name.startsWith(".")) continue;
        scanDir(path.join(currentDir, entry.name), currentDepth + 1);
      }
    }
  }

  scanDir(dir, 0);
  return { imported, skipped };
}

// ─── Helper ─────────────────────────────────────────────────────────────────

function makeEntry(
  key: string, content: string, tags: string[], namespace: string,
  now: string, existing?: MemoryEntry, createdAtOverride?: string,
): MemoryEntry {
  return {
    key, content, tags, namespace,
    createdAt: existing?.createdAt ?? createdAtOverride ?? now,
    updatedAt: now,
    accessCount: existing?.accessCount ?? 0,
    lastAccessedAt: existing?.lastAccessedAt ?? null,
    pinned: existing?.pinned ?? false,
  };
}
