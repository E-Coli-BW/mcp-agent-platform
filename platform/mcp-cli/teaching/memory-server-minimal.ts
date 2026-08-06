#!/usr/bin/env node
/**
 * ═══════════════════════════════════════════════════════════════════════════
 *  Memory Store MCP Server — 教学最小骨架 (Minimal Teaching Skeleton)
 * ═══════════════════════════════════════════════════════════════════════════
 *
 *  只留 2 个工具：memory_set（写）+ memory_get（读）。
 *  搞懂一个 tool 的完整往返，其余 tool / resource 全是同一套路。
 *
 *  这份代码回答 3 个问题：
 *    1. 原理 —— MCP Server 是什么？stdio 模式怎么和 AI 客户端通信？
 *    2. 规范 —— 一个 tool 由哪几部分组成？入参/返回值有什么强制约定？
 *    3. 流程 —— 一次工具调用，从 LLM 到你的 handler 再回去，中间发生了什么？
 *
 *  ── 一次 tool 调用的生命周期 (stdio 模式) ──────────────────────────────
 *
 *    AI 客户端(VS Code)                    本进程 (MCP Server)
 *         │                                      │
 *         │  1. 启动时: tools/list  ──────────►  │  返回所有工具的
 *         │  ◄──────── [memory_set, memory_get]  │  name/description/schema
 *         │                                      │
 *         │  (LLM 根据 description 决定调用哪个)   │
 *         │                                      │
 *         │  2. tools/call            ────────►  │  SDK 用 zod schema
 *         │     {name:"memory_set",              │  校验入参 → 调 handler
 *         │      arguments:{key,content}}        │       │
 *         │                                      │       ▼ 执行业务逻辑
 *         │  ◄──── {content:[{type,text}]}       │  返回 textResult(...)
 *         │                                      │
 *    全程走 stdin/stdout 上的 JSON-RPC 2.0 消息
 * ───────────────────────────────────────────────────────────────────────────
 */

// ─── 依赖 ─────────────────────────────────────────────────────────────────────
// @modelcontextprotocol/sdk : 官方 SDK，替我们处理 JSON-RPC / 协议握手细节
// zod                       : 定义参数 schema，SDK 自动转成 JSON Schema 给 LLM 看
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// ─── ① 朴素持久化 (与 MCP 无关的业务存储) ────────────────────────────────────
// 生产版有 WAL / 原子写 / 并发锁；教学版就是"整个文件读进来、整个写回去"。
// 讲课时一句带过：这只是"数据存哪"，不是 MCP 的重点。
const STORE_FILE = path.join(os.homedir(), ".memory-store-minimal.json");

function loadStore(): Record<string, string> {
  try {
    return JSON.parse(fs.readFileSync(STORE_FILE, "utf-8"));
  } catch {
    return {}; // 文件不存在 = 空存储
  }
}

function saveStore(store: Record<string, string>): void {
  fs.writeFileSync(STORE_FILE, JSON.stringify(store, null, 2));
}

// ─── ② 响应辅助函数 (★ MCP 规范核心) ─────────────────────────────────────────
// 所有工具的返回值,都必须是这个形状: { content: [{ type: "text", text }] }
// 这是协议约定 —— client 靠它把结果解析出来喂给 LLM。封装成小函数复用。
function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

// ─── ③ 创建 Server 实例 ───────────────────────────────────────────────────────
// name + version 是这个 server 的身份,client 用它区分同时挂载的多个 server。
const server = new McpServer({ name: "memory-store-minimal", version: "1.0.0" });

// ─── ④ 工具一：memory_set (写) ────────────────────────────────────────────────
// server.tool(名字, 描述, 参数schema, 处理函数) —— 一个 tool 的四要素:
//   · 名字   → LLM 看到的函数名
//   · 描述   → ★LLM 判断"何时该调用我"的唯一依据,要写成 trigger 而非摘要
//   · schema → 用 zod 定义,SDK 自动转 JSON Schema:既告诉 LLM 参数长啥样,
//              又在调用时做运行时校验(类型不对直接被 SDK 挡下)
//   · handler→ 纯业务逻辑,拿到的已是校验过的参数
server.tool(
  "memory_set",
  "Save a memory: store a piece of text under a unique key.",
  {
    key: z.string().describe("Unique identifier for this memory"),
    content: z.string().describe("The text content to remember"),
  },
  async ({ key, content }) => {
    const store = loadStore();
    const existed = key in store;

    store[key] = content;
    saveStore(store);

    // ★ emoji 前缀是团队约定,给 LLM 和用户一个明确的成功/失败信号
    return textResult(existed ? `✅ Updated: "${key}"` : `✅ Created: "${key}"`);
  }
);

// ─── ⑤ 工具二：memory_get (读) ────────────────────────────────────────────────
// 和 set 完全同构,只是业务逻辑不同。搞懂这两个,search/list/delete 全是复制粘贴。
server.tool(
  "memory_get",
  "Retrieve a memory by its exact key.",
  {
    key: z.string().describe("The key to retrieve"),
  },
  async ({ key }) => {
    const store = loadStore();
    const content = store[key];

    // ★ 关键规范:找不到也返回正常的 text,绝不 throw。
    //   让 LLM 自己读到"没找到"再决定下一步,而不是让整个调用崩掉。
    if (content === undefined) return textResult(`❌ Not found: "${key}"`);
    return textResult(content);
  }
);

// ─── ⑥ 启动：连接 stdio 传输 ──────────────────────────────────────────────────
// ★ stdio 模式的本质:
//   client 把本进程当子进程拉起来,通过 stdin/stdout 收发 JSON-RPC 消息。
//   由此推出一条铁律:
//     · 业务输出绝不能用 console.log —— 会污染 stdout 的协议流,直接搞坏通信!
//     · 所有日志一律走 console.error(stderr),client 会忽略它。
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport); // 之后 SDK 开始监听 stdin 上的请求,阻塞常驻
  console.error("🧠 memory-store-minimal running on stdio (2 tools)");
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
