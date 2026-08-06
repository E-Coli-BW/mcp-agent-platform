# API Reference — AI Coding Agent Platform

## Overview

| Service | Port | Base URL | Description |
|---------|------|----------|-------------|
| Agent Server (Python) | 8500 | `http://localhost:8500` | 主入口，OpenAI 兼容接口 + 管理 API |
| Auth Service (Java) | 8090 | `http://localhost:8090` | RS256 JWT 认证中心 |
| Memory Server (Java) | 8180 | `http://localhost:8180` | 持久化记忆存储 + TF-IDF 搜索 |
| Code Exec Server (Java) | 8380 | `http://localhost:8380` | Docker 沙箱代码执行 |

---

## 场景分类

### 🔐 Scene 1: 认证与会话管理

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/auth/signup` | Auth | 用户注册 |
| POST | `/auth/login` | Auth | 用户登录（返回 JWT + refresh_token） |
| POST | `/auth/token` | Auth | OAuth2 token（client_credentials / password grant） |
| POST | `/auth/refresh` | Auth | 刷新 access_token |
| POST | `/auth/logout` | Auth | 登出（token 加黑名单） |
| POST | `/auth/register` | Auth | 注册 M2M 客户端（需 ADMIN） |
| GET | `/auth/jwks` | Auth | JWKS 公钥端点（服务间验证） |
| GET | `/auth/check-blacklist?jti=xxx` | Auth | 检查 token 是否被吊销 |
| GET | `/auth/health` | Auth | 健康检查 |

### 💬 Scene 2: AI 对话（核心业务流）

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/v1/chat/completions` | Agent | OpenAI 兼容聊天（SSE 流式） |
| GET | `/v1/models` | Agent | 列出可用模型 |
| POST | `/v1/sessions/{id}/children/{child}/cancel` | Agent | 取消子 agent |

### 🧠 Scene 3: 记忆管理（MCP Tools）

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/memory_set` | Memory | 写入记忆 |
| POST | `/api/tools/memory_get` | Memory | 读取记忆 |
| POST | `/api/tools/memory_search` | Memory | TF-IDF 搜索记忆 |
| POST | `/api/tools/memory_delete` | Memory | 删除记忆 |
| POST | `/api/tools/memory_list` | Memory | 列出所有记忆 |
| POST | `/api/tools/memory_pin` | Memory | 置顶/取消置顶 |
| POST | `/api/tools/memory_context` | Memory | 记忆系统概览 |

### 🎓 Scene 4: 技能存储（Skill Store）

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/skill_set` | Memory | 保存技能 |
| POST | `/api/tools/skill_get` | Memory | 获取技能 |
| POST | `/api/tools/skill_list` | Memory | 列出技能 |
| POST | `/api/tools/skill_history` | Memory | 技能版本历史 |
| POST | `/api/tools/skill_rollback` | Memory | 回滚技能版本 |
| POST | `/api/tools/skill_feedback` | Memory | 技能使用反馈 |
| GET | `/api/tools/skill_triggers` | Memory | 技能触发规则 |

### 🖥️ Scene 5: 代码执行

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| POST | `/api/tools/code_run` | CodeExec | 运行代码（Docker 隔离） |
| POST | `/api/tools/code_shell` | CodeExec | 执行 shell 命令 |

### 📁 Scene 6: 工作区管理

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| GET | `/api/workspace/current` | Agent | 当前工作区路径 |
| POST | `/api/workspace/open` | Agent | 打开工作区 |
| GET | `/api/workspace/browse?path=xxx` | Agent | 浏览目录 |
| GET | `/api/workspace/files` | Agent | 列出文件 |
| GET | `/api/workspace/file?path=xxx` | Agent | 读取文件内容 |

### 📊 Scene 7: 运维与可观测性

| Method | Endpoint | Service | Description |
|--------|----------|---------|-------------|
| GET | `/health` | Agent | 健康检查 |
| GET | `/metrics` | Agent | 请求指标（QPS、延迟） |
| GET | `/api/usage` | Agent | Token 用量与成本 |
| GET | `/api/prompts` | Agent | Prompt 版本与灰度配置 |
| GET | `/v1/runs/{run_id}/events?after_seq=0` | Agent | 查询 runtime run 事件流（按 tenant 隔离，支持 cursor） |
| GET | `/api/reranker` | Agent | Reranker 权重信息 |
| POST | `/api/reranker/retrain` | Agent | 触发 reranker 再训练 |
| GET | `/api/reranker/evaluate` | Agent | 评估 reranker 效果 |
| GET | `/api/analytics` | Memory | 事件分析统计 |

---

## 时序图

### 完整对话流程

```
┌──────┐       ┌──────────┐       ┌────────┐       ┌──────────┐       ┌──────────┐
│Client│       │Agent Svc │       │Auth Svc│       │Memory Svc│       │CodeExec  │
└──┬───┘       └────┬─────┘       └───┬────┘       └────┬─────┘       └────┬─────┘
   │                │                  │                  │                  │
   │ POST /auth/login                  │                  │                  │
   │────────────────────────────────► │                  │                  │
   │ ◄─── {access_token, refresh_token}│                  │                  │
   │                │                  │                  │                  │
   │ POST /v1/chat/completions         │                  │                  │
   │ [Authorization: Bearer xxx]       │                  │                  │
   │───────────────►│                  │                  │                  │
   │                │ verify JWT (JWKS)│                  │                  │
   │                │─────────────────►│                  │                  │
   │                │◄─── valid        │                  │                  │
   │                │                  │                  │                  │
   │                │ ReAct Loop ──────────────────────────────────────────  │
   │                │ │                │                  │                  │
   │ ◄── SSE: thinking                │                  │                  │
   │                │ │ LLM decides: use memory_search   │                  │
   │ ◄── SSE: tool_start              │                  │                  │
   │                │ │────────────────────────────────►  │                  │
   │                │ │◄─── search results               │                  │
   │ ◄── SSE: tool_end                │                  │                  │
   │                │ │                │                  │                  │
   │                │ │ LLM decides: use code_run         │                  │
   │ ◄── SSE: tool_start              │                  │                  │
   │                │ │───────────────────────────────────────────────────► │
   │                │ │◄─── execution result             │                  │
   │ ◄── SSE: tool_end                │                  │                  │
   │                │ │                │                  │                  │
   │                │ │ LLM generates final answer        │                  │
   │ ◄── SSE: content tokens (streamed)│                  │                  │
   │ ◄── SSE: [DONE]                  │                  │                  │
   │                │                  │                  │                  │
```

### 认证流程泳道图

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Authentication Flow                            │
├──────────┬───────────────────────┬──────────────────────────────────┤
│  Client  │     Agent Server      │          Auth Service             │
├──────────┼───────────────────────┼──────────────────────────────────┤
│          │                       │                                    │
│ signup ──────────────────────────────► POST /auth/signup              │
│          │                       │  ◄── {user_id, tenant_id}         │
│          │                       │                                    │
│ login ───────────────────────────────► POST /auth/login              │
│          │                       │  ◄── {access_token (15min),       │
│          │                       │       refresh_token (7d)}         │
│          │                       │                                    │
│ chat ───►│ JWT middleware        │                                    │
│          │ ├─ decode token       │                                    │
│          │ ├─ verify signature ──────► GET /auth/jwks (cached)       │
│          │ ├─ check expiry       │                                    │
│          │ ├─ extract tenant_id  │                                    │
│          │ └─ set request.state  │                                    │
│          │ [proceed to handler]  │                                    │
│          │                       │                                    │
│ refresh ─────────────────────────────► POST /auth/refresh            │
│          │                       │  ◄── {new access_token}           │
│          │                       │                                    │
│ logout ──────────────────────────────► POST /auth/logout             │
│          │                       │  (token blacklisted)              │
└──────────┴───────────────────────┴──────────────────────────────────┘
```

### 多租户隔离泳道

```
┌────────────────────────────────────────────────────────────────────┐
│                    Multi-Tenancy Isolation                           │
├──────────┬───────────────────────┬─────────────────────────────────┤
│ Request  │     Middleware Stack   │         Data Layer               │
├──────────┼───────────────────────┼─────────────────────────────────┤
│          │                       │                                   │
│ HTTP ───►│ 1. CORS check         │                                   │
│          │ 2. JWT decode ────────────► tenant_id = jwt["tenant_id"]  │
│          │ 3. Rate limit (IP)    │                                   │
│          │ 4. Rate limit (tenant)│    ← lookup RPM for tenant       │
│          │ 5. Set context ───────────► TenantContext.set(tenant_id)  │
│          │                       │                                   │
│          │ Handler executes      │                                   │
│          │ ├─ Redis key: ────────────► conv:{tenant}:{session}       │
│          │ ├─ Memory API: ───────────► header X-Tenant-Id            │
│          │ ├─ LangGraph thread: ─────► thread_id = {tenant}:{sid}    │
│          │ └─ Kafka event: ──────────► event.tenant_id = tenant      │
│          │                       │                                   │
└──────────┴───────────────────────┴─────────────────────────────────┘
```
