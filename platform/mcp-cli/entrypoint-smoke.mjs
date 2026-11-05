import { spawn } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const root = path.dirname(new URL(import.meta.url).pathname);
const manifest = JSON.parse(await readFile(path.join(root, "package.json"), "utf8"));
const entries = [];
if (manifest.main) entries.push(["main", `node ${manifest.main}`]);
if (typeof manifest.bin === "string") {
  entries.push(["bin", `node ${manifest.bin}`]);
} else {
  for (const [name, target] of Object.entries(manifest.bin || {})) {
    entries.push([`bin:${name}`, `node ${target}`]);
  }
}
entries.push(...Object.entries(manifest.scripts || {})
  .filter(([name]) => name.startsWith("start:")));

if (entries.length === 0) {
  throw new Error("package exposes no main, bin, or start:* MCP entrypoints");
}

async function assertTarget(scriptName, command) {
  const match = /^node\s+([^\s]+)$/.exec(command.trim());
  if (!match) {
    throw new Error(`${scriptName}: unsupported smoke command: ${command}`);
  }
  const target = path.resolve(root, match[1]);
  let info;
  try {
    info = await stat(target);
  } catch {
    throw new Error(`${scriptName}: advertised target does not exist: ${match[1]}`);
  }
  if (!info.isFile() || info.size === 0) {
    throw new Error(`${scriptName}: advertised target is not a non-empty file: ${match[1]}`);
  }
  return target;
}

async function initialize(scriptName, target) {
  const child = spawn(process.execPath, [target], {
    cwd: root,
    env: { ...process.env, TOOL_PRESET: "minimal" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });

  const request = {
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "entrypoint-smoke", version: "1.0.0" },
    },
  };
  child.stdin.write(`${JSON.stringify(request)}\n`);

  try {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`${scriptName}: initialize timed out; stderr=${stderr.slice(-500)}`));
      }, 5_000);
      const check = () => {
        for (const line of stdout.split("\n")) {
          if (!line.trim()) continue;
          try {
            const message = JSON.parse(line);
            if (message.id === 1 && message.result?.serverInfo?.name) {
              clearTimeout(timeout);
              resolve();
              return;
            }
          } catch {
            // Wait for a complete JSON line.
          }
        }
      };
      child.stdout.on("data", check);
      child.once("exit", (code) => {
        clearTimeout(timeout);
        reject(new Error(`${scriptName}: exited before initialize (${code}); stderr=${stderr.slice(-500)}`));
      });
    });
  } finally {
    child.kill("SIGTERM");
  }
}

for (const [scriptName, command] of entries) {
  const target = await assertTarget(scriptName, command);
  await initialize(scriptName, target);
  console.log(`ok ${scriptName}`);
}
