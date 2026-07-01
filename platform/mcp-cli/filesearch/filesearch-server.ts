#!/usr/bin/env node
/**
 * Local File Search MCP Server
 *
 * Local file-search tool, supporting:
 * - Search by filename glob pattern
 * - Search by file-content keyword (grep style)
 * - Filter by file type / directory
 * - Read file content (with line-number range)
 * - Retrieve the directory structure tree
 *
 * Runs purely locally, with no external service dependencies
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Default directories to skip */
const IGNORE_DIRS = new Set([
  "node_modules", ".git", ".svn", ".hg", "__pycache__",
  ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
  ".next", ".nuxt", ".output", "coverage", ".cache",
  "vendor", "Pods", ".gradle", "target",
]);

/** Default binary/large file extensions to skip */
const BINARY_EXTS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
  ".mp3", ".mp4", ".avi", ".mov", ".wav",
  ".zip", ".tar", ".gz", ".rar", ".7z",
  ".pdf", ".doc", ".docx", ".xls", ".xlsx",
  ".exe", ".dll", ".so", ".dylib", ".o",
  ".woff", ".woff2", ".ttf", ".eot",
  ".pyc", ".class", ".jar",
]);

function shouldIgnoreDir(name: string): boolean {
  return IGNORE_DIRS.has(name) || name.startsWith(".");
}

function isBinaryFile(filePath: string): boolean {
  return BINARY_EXTS.has(path.extname(filePath).toLowerCase());
}

/** Resolve ~ in paths */
function resolvePath(p: string): string {
  if (p.startsWith("~")) {
    return path.join(os.homedir(), p.slice(1));
  }
  return path.resolve(p);
}

/** Simple glob matching (supports * and **) */
function globMatch(pattern: string, filePath: string): boolean {
  const regex = pattern
    .replace(/\./g, "\\.")
    .replace(/\*\*/g, "{{GLOBSTAR}}")
    .replace(/\*/g, "[^/]*")
    .replace(/{{GLOBSTAR}}/g, ".*");
  return new RegExp(`^${regex}$`).test(filePath);
}

/** Walk directory recursively, respecting ignore rules */
function* walkDir(
  dir: string,
  opts: { maxDepth?: number; currentDepth?: number } = {}
): Generator<{ filePath: string; isDir: boolean; depth: number }> {
  const maxDepth = opts.maxDepth ?? 20;
  const currentDepth = opts.currentDepth ?? 0;

  if (currentDepth > maxDepth) return;

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return; // skip inaccessible dirs
  }

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const isDir = entry.isDirectory();

    if (isDir && shouldIgnoreDir(entry.name)) continue;

    yield { filePath: fullPath, isDir, depth: currentDepth };

    if (isDir) {
      yield* walkDir(fullPath, { maxDepth, currentDepth: currentDepth + 1 });
    }
  }
}

// ─── MCP Server ──────────────────────────────────────────────────────────────

const server = new McpServer({
  name: "local-file-search",
  version: "1.0.0",
});

// ── Tool: file_search ────────────────────────────────────────────────────────
server.tool(
  "file_search",
  "Search for files by name pattern in a directory. Supports glob patterns like '*.ts', '**/*.json', 'README*'. Useful for finding files when you know (part of) the filename.",
  {
    directory: z.string().describe("Root directory to search in (supports ~). E.g. '~/projects' or '/Users/me/code'"),
    pattern: z.string().describe("Glob pattern to match filenames. E.g. '*.ts', '**/*.test.js', 'Dockerfile*'"),
    maxResults: z.number().optional().describe("Max number of results. Default: 50"),
    maxDepth: z.number().optional().describe("Max directory depth to recurse. Default: 10"),
  },
  async ({ directory, pattern, maxResults, maxDepth }) => {
    const dir = resolvePath(directory);
    const limit = maxResults ?? 50;
    const results: string[] = [];

    if (!fs.existsSync(dir)) {
      return {
        content: [{ type: "text" as const, text: `❌ Directory not found: ${dir}` }],
      };
    }

    for (const { filePath, isDir } of walkDir(dir, { maxDepth: maxDepth ?? 10 })) {
      if (isDir) continue;
      const relativePath = path.relative(dir, filePath);
      const fileName = path.basename(filePath);

      if (globMatch(pattern, fileName) || globMatch(pattern, relativePath)) {
        results.push(relativePath);
        if (results.length >= limit) break;
      }
    }

    if (results.length === 0) {
      return {
        content: [{ type: "text" as const, text: `🔍 No files matching "${pattern}" found in ${dir}` }],
      };
    }

    return {
      content: [
        {
          type: "text" as const,
          text: `📂 Found ${results.length} file(s) matching "${pattern}" in ${dir}:\n\n${results.join("\n")}`,
        },
      ],
    };
  }
);

// ── Tool: content_search (grep) ──────────────────────────────────────────────
server.tool(
  "content_search",
  "Search for text content inside files (like grep). Finds lines matching a query string or regex. Returns matching lines with file paths and line numbers.",
  {
    directory: z.string().describe("Root directory to search in (supports ~)"),
    query: z.string().describe("Text or regex pattern to search for inside files"),
    isRegex: z.boolean().optional().describe("Whether the query is a regex pattern. Default: false (plain text)"),
    filePattern: z.string().optional().describe("Optional glob pattern to filter which files to search. E.g. '*.py' to only search Python files"),
    caseSensitive: z.boolean().optional().describe("Case-sensitive search. Default: false"),
    maxResults: z.number().optional().describe("Max number of matching lines to return. Default: 100"),
    contextLines: z.number().optional().describe("Number of context lines before/after each match. Default: 0"),
  },
  async ({ directory, query, isRegex, filePattern, caseSensitive, maxResults, contextLines }) => {
    const dir = resolvePath(directory);
    const limit = maxResults ?? 100;
    const ctxLines = contextLines ?? 0;
    const flags = caseSensitive ? "g" : "gi";

    if (!fs.existsSync(dir)) {
      return {
        content: [{ type: "text" as const, text: `❌ Directory not found: ${dir}` }],
      };
    }

    let regex: RegExp;
    try {
      regex = isRegex ? new RegExp(query, flags) : new RegExp(escapeRegex(query), flags);
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: `❌ Invalid regex: ${query}` }],
      };
    }

    interface Match {
      file: string;
      line: number;
      text: string;
      context?: string[];
    }

    const matches: Match[] = [];

    for (const { filePath, isDir } of walkDir(dir, { maxDepth: 10 })) {
      if (isDir || isBinaryFile(filePath)) continue;

      if (filePattern) {
        const fileName = path.basename(filePath);
        const relPath = path.relative(dir, filePath);
        if (!globMatch(filePattern, fileName) && !globMatch(filePattern, relPath)) {
          continue;
        }
      }

      let content: string;
      try {
        // Skip large files (> 1MB)
        const stat = fs.statSync(filePath);
        if (stat.size > 1_000_000) continue;
        content = fs.readFileSync(filePath, "utf-8");
      } catch {
        continue;
      }

      const lines = content.split("\n");
      for (let i = 0; i < lines.length; i++) {
        if (regex.test(lines[i])) {
          const match: Match = {
            file: path.relative(dir, filePath),
            line: i + 1,
            text: lines[i].trim(),
          };

          if (ctxLines > 0) {
            const start = Math.max(0, i - ctxLines);
            const end = Math.min(lines.length - 1, i + ctxLines);
            match.context = lines.slice(start, end + 1);
          }

          matches.push(match);
          // Reset regex lastIndex for global flag
          regex.lastIndex = 0;

          if (matches.length >= limit) break;
        }
        regex.lastIndex = 0;
      }

      if (matches.length >= limit) break;
    }

    if (matches.length === 0) {
      return {
        content: [
          {
            type: "text" as const,
            text: `🔍 No matches for "${query}" in ${dir}${filePattern ? ` (filtered by ${filePattern})` : ""}`,
          },
        ],
      };
    }

    // Group by file for readability
    const grouped = new Map<string, Match[]>();
    for (const m of matches) {
      if (!grouped.has(m.file)) grouped.set(m.file, []);
      grouped.get(m.file)!.push(m);
    }

    let output = `🔍 Found ${matches.length} match(es) in ${grouped.size} file(s):\n\n`;
    for (const [file, fileMatches] of grouped) {
      output += `📄 ${file}\n`;
      for (const m of fileMatches) {
        output += `  L${m.line}: ${m.text}\n`;
      }
      output += "\n";
    }

    return {
      content: [{ type: "text" as const, text: output }],
    };
  }
);

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// ── Tool: read_file_content ──────────────────────────────────────────────────
server.tool(
  "read_file_content",
  "Read the content of a local file with optional line range. Returns the full text content with line numbers.",
  {
    filePath: z.string().describe("Absolute path to the file to read (supports ~)"),
    startLine: z.number().optional().describe("Start line number (1-based). Default: 1"),
    endLine: z.number().optional().describe("End line number (1-based, inclusive). Default: end of file"),
  },
  async ({ filePath: rawPath, startLine, endLine }) => {
    const filePath = resolvePath(rawPath);

    if (!fs.existsSync(filePath)) {
      return {
        content: [{ type: "text" as const, text: `❌ File not found: ${filePath}` }],
      };
    }

    const stat = fs.statSync(filePath);
    if (stat.size > 5_000_000) {
      return {
        content: [{ type: "text" as const, text: `❌ File too large (${(stat.size / 1_000_000).toFixed(1)}MB). Max 5MB.` }],
      };
    }

    const content = fs.readFileSync(filePath, "utf-8");
    const lines = content.split("\n");
    const start = Math.max(1, startLine ?? 1);
    const end = Math.min(lines.length, endLine ?? lines.length);

    const selectedLines = lines.slice(start - 1, end);
    const numbered = selectedLines.map((line: string, i: number) => `${String(start + i).padStart(5)} | ${line}`);

    return {
      content: [
        {
          type: "text" as const,
          text: `📄 ${filePath} (lines ${start}-${end} of ${lines.length}):\n\n${numbered.join("\n")}`,
        },
      ],
    };
  }
);

// ── Tool: directory_tree ─────────────────────────────────────────────────────
server.tool(
  "directory_tree",
  "Get a tree view of a directory structure. Shows files and folders hierarchically, like the 'tree' command.",
  {
    directory: z.string().describe("Root directory to list (supports ~)"),
    maxDepth: z.number().optional().describe("Max depth to display. Default: 3"),
    showHidden: z.boolean().optional().describe("Show hidden files/dirs (starting with .). Default: false"),
  },
  async ({ directory, maxDepth, showHidden }) => {
    const dir = resolvePath(directory);
    const depth = maxDepth ?? 3;

    if (!fs.existsSync(dir)) {
      return {
        content: [{ type: "text" as const, text: `❌ Directory not found: ${dir}` }],
      };
    }

    function buildTree(currentDir: string, currentDepth: number, prefix: string): string {
      if (currentDepth > depth) return "";

      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(currentDir, { withFileTypes: true });
      } catch {
        return "";
      }

      // Sort: dirs first, then files, alphabetically
      entries.sort((a, b) => {
        if (a.isDirectory() && !b.isDirectory()) return -1;
        if (!a.isDirectory() && b.isDirectory()) return 1;
        return a.name.localeCompare(b.name);
      });

      // Filter hidden
      if (!showHidden) {
        entries = entries.filter((e) => !e.name.startsWith("."));
      }

      // Filter ignored dirs
      entries = entries.filter((e) => !(e.isDirectory() && IGNORE_DIRS.has(e.name)));

      let result = "";
      for (let i = 0; i < entries.length; i++) {
        const entry = entries[i];
        const isLast = i === entries.length - 1;
        const connector = isLast ? "└── " : "├── ";
        const childPrefix = isLast ? "    " : "│   ";

        const icon = entry.isDirectory() ? "📁 " : "";
        result += `${prefix}${connector}${icon}${entry.name}\n`;

        if (entry.isDirectory() && currentDepth < depth) {
          result += buildTree(
            path.join(currentDir, entry.name),
            currentDepth + 1,
            prefix + childPrefix
          );
        }
      }

      return result;
    }

    const tree = `📂 ${dir}\n${buildTree(dir, 1, "")}`;

    return {
      content: [{ type: "text" as const, text: tree }],
    };
  }
);

// ── Tool: file_info ──────────────────────────────────────────────────────────
server.tool(
  "file_info",
  "Get detailed metadata about a file or directory: size, permissions, timestamps, type.",
  {
    filePath: z.string().describe("Path to the file or directory (supports ~)"),
  },
  async ({ filePath: rawPath }) => {
    const filePath = resolvePath(rawPath);

    if (!fs.existsSync(filePath)) {
      return {
        content: [{ type: "text" as const, text: `❌ Not found: ${filePath}` }],
      };
    }

    const stat = fs.statSync(filePath);
    const info = {
      path: filePath,
      type: stat.isDirectory() ? "directory" : stat.isFile() ? "file" : "other",
      size: stat.isFile() ? formatSize(stat.size) : undefined,
      sizeBytes: stat.isFile() ? stat.size : undefined,
      created: stat.birthtime.toISOString(),
      modified: stat.mtime.toISOString(),
      accessed: stat.atime.toISOString(),
      permissions: (stat.mode & 0o777).toString(8),
      extension: stat.isFile() ? path.extname(filePath) : undefined,
    };

    return {
      content: [{ type: "text" as const, text: JSON.stringify(info, null, 2) }],
    };
  }
);

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

// ── Start ────────────────────────────────────────────────────────────────────
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("🔍 Local File Search MCP server running on stdio");
}

main().catch((err) => {
  console.error("Fatal error:", err);
  process.exit(1);
});
