import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const readme = fileURLToPath(new URL("../../README.md", import.meta.url));
const server = fileURLToPath(
  new URL("./dist/filesearch/filesearch-server.js", import.meta.url),
);
const client = new Client({ name: "local-mcp-demo", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [server],
});

try {
  await client.connect(transport);
  assert.equal(client.getServerVersion()?.name, "local-file-search");
  console.log("MCP stdio: connected to local-file-search");

  const { tools } = await client.listTools();
  assert.ok(tools.some((tool) => tool.name === "read_file_content"));
  console.log("tools/list: read_file_content available");

  const result = await client.callTool({
    name: "read_file_content",
    arguments: { filePath: readme, startLine: 1, endLine: 3 },
  });
  assert.notEqual(result.isError, true);
  const text = result.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n");
  const expected = (await readFile(readme, "utf8"))
    .split("\n")
    .slice(0, 3)
    .map((line, index) => `${String(index + 1).padStart(5)} | ${line}`)
    .join("\n");
  assert.equal(text.split("\n").slice(2).join("\n"), expected);
  console.log("tools/call: README.md, lines 1-3");
  console.log(expected);
  console.log("PASS: file content matches the repository README");
} finally {
  await client.close();
  await transport.close();
}
