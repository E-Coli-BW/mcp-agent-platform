import { access, readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(packageRoot, "../..");

async function exists(target) {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
}

async function listFiles(root) {
  const files = [];
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const entryPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      files.push(...await listFiles(entryPath));
    } else {
      files.push(entryPath);
    }
  }
  return files;
}

for (const relativePath of ["src/memory-server.ts", "src/filesearch-server.ts"]) {
  if (await exists(path.join(repoRoot, relativePath))) {
    throw new Error(`retired root facade returned: ${relativePath}`);
  }
}

const duplicateRoot = path.join(repoRoot, "tools/memory-ts");
if (await exists(duplicateRoot)) {
  if ((await listFiles(duplicateRoot)).length > 0) {
    throw new Error("retired tools/memory-ts tree contains files");
  }
}

const canonicalServer = path.join(packageRoot, "memory-server.ts");
const canonicalInfo = await stat(canonicalServer);
if (!canonicalInfo.isFile() || canonicalInfo.size === 0) {
  throw new Error("canonical MCP memory server is missing or empty");
}

const staleTargets = [
  "dist/memory-ts/memory-server.js",
  "dist/filesearch-ts/filesearch-server.js",
  "tools/memory-ts/memory-server.ts",
  "tools/memory-ts/memory/skill-extractor.ts",
];
const contractFiles = [
  "package.json",
  "platform/mcp-cli/package.json",
  "platform/mcp-cli/test-skill-extraction.sh",
  "platform/docs/design/SKILL-EXTRACTION-DESIGN.md",
];
for (const relativePath of contractFiles) {
  const content = await readFile(path.join(repoRoot, relativePath), "utf8");
  for (const staleTarget of staleTargets) {
    if (content.includes(staleTarget)) {
      throw new Error(`${relativePath} references retired target ${staleTarget}`);
    }
  }
}

const skillSmoke = await readFile(
  path.join(repoRoot, "platform/mcp-cli/test-skill-extraction.sh"),
  "utf8",
);
if (!skillSmoke.includes('$SCRIPT_DIR/dist/memory-server.js')) {
  throw new Error("skill extraction smoke does not use the canonical MCP build target");
}

const authSource = await readFile(
  path.join(repoRoot, "platform/frontend/src/lib/auth.ts"),
  "utf8",
);
if (/export\s+function\s+onAuthChange\b/.test(authSource)) {
  throw new Error("frontend auth exposes the retired onAuthChange API");
}

console.log("ok architecture dead-wiring contracts");
