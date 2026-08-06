# Memory Server — Architecture Reference

> Consolidated from the original MVP design fragments (modules 0-7)
> and the distributed memory store design.

---

# Memory MCP Platform — Enterprise MVP Design

## 1. MVP Scope Definition

### 核心问题
把个人版 memory MCP server 升级为企业级多租户平台，支持团队共享记忆、安全访问、可观测运维。

### MVP 必须有 vs 可以没有

| Feature | MVP | 理由 |
|---------|:---:|------|
| MCP SSE Transport | ✅ | 核心：多客户端连一个服务 |
| Multi-tenancy (多租户) | ✅ | 企业核心需求，从第一天就要设计进去 |
| JWT Authentication | ✅ | 最基础的安全要求 |
| PostgreSQL 持久化 | ✅ | JSON 文件不适合并发写 |
| Redis 缓存 | ✅ | 读多写少场景，缓存价值大 |
| TF-IDF 搜索 | ✅ | 核心功能，先用内置实现 |
| Health Check + Metrics | ✅ | 运维最低要求 |
| Retry + Circuit Breaker | ✅ | 生产级容错 |
| Audit Log | ✅ | 企业合规要求 |
| Elasticsearch 搜索 | ❌ | MVP 后期，先用 DB LIKE + 内存 TF-IDF |
| Kafka 异步导入 | ❌ | MVP 用同步导入，够用 |
| 向量搜索 / Embedding | ❌ | 未来方向，不影响 MVP |
| Spring Cloud Gateway | ❌ | MVP 单服务，网关后加 |
| K8s 部署 | ❌ | MVP 用 Docker Compose |
| 灰度发布 | ❌ | MVP 后期 |

### Tradeoff 记录

| 决策 | 选择 | 放弃 | 理由 |
|------|------|------|------|
| 搜索引擎 | 内置 TF-IDF + DB LIKE | Elasticsearch | MVP 减少依赖，ES 后期加不影响接口 |
| 消息队列 | 同步写 | Kafka | 导入量不大，同步够用 |
| 网关 | 内嵌 Filter | 独立 Gateway | 单服务不需要独立网关 |
| 缓存 | Redis write-through | 本地 Caffeine | 多实例部署需要共享缓存 |
| 数据库 | PostgreSQL | MySQL/MongoDB | JSONB 支持好，全文搜索内置 |
| 鉴权 | JWT (stateless) | Session (stateful) | 无状态，适合多实例 |
| 多租户 | 共享表 + tenant_id | 独立 schema/库 | MVP 简单，数据量小时够用 |

## 2. Architecture (MVP)

```
┌──────────────────────────────────────────────────┐
│              Spring Boot 3.4 (Java 21)           │
│                                                  │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐ │
│  │  Security   │  │   MCP SSE  │  │  Actuator  │ │
│  │  Filter     │  │  Transport │  │  /health   │ │
│  │  (JWT)      │  │  /sse      │  │  /metrics  │ │
│  └──────┬─────┘  └──────┬─────┘  └────────────┘ │
│         │               │                        │
│  ┌──────▼───────────────▼──────────────────────┐ │
│  │            Tool Service Layer                │ │
│  │  memory_set | memory_get | memory_search     │ │
│  │  memory_delete | memory_context | memory_pin │ │
│  └──────────────────┬──────────────────────────┘ │
│                     │                            │
│  ┌──────────────────▼──────────────────────────┐ │
│  │           Domain Service Layer               │ │
│  │  MemoryService (business logic)              │ │
│  │  SearchEngine (TF-IDF in-memory)             │ │
│  │  AuditService (log who did what)             │ │
│  └──────┬───────────────┬──────────────────────┘ │
│         │               │                        │
│  ┌──────▼─────┐  ┌──────▼──────┐                │
│  │  Redis     │  │ PostgreSQL  │                 │
│  │  (Cache)   │  │ (Storage)   │                 │
│  └────────────┘  └─────────────┘                 │
└──────────────────────────────────────────────────┘
```

## 3. Module Breakdown (实现顺序)

按依赖关系从底向上：

| # | Module | 文件 | 依赖 | 估计行数 |
|---|--------|------|------|---------|
| 1 | **Data Model** | `model/` | 无 | ~100 |
| 2 | **Persistence** | `repository/` | JPA, PostgreSQL | ~80 |
| 3 | **Cache** | `cache/` | Redis | ~60 |
| 4 | **Search** | `search/` | 无外部依赖 | ~80 |
| 5 | **Domain Service** | `service/` | 2,3,4 | ~200 |
| 6 | **Security** | `security/` | Spring Security | ~100 |
| 7 | **MCP Tools** | `tool/` | 5 | ~150 |
| 8 | **Audit** | `audit/` | AOP | ~60 |
| 9 | **Observability** | `config/` | Actuator, Micrometer | ~50 |
| 10 | **Docker Compose** | `docker/` | — | ~30 |

**总计：~900 行 Java + 配置**

## 4. Package Structure

```
com.example.memoryserver/
├── MemoryServerApplication.java          # Entry point
├── model/
│   ├── MemoryEntity.java                 # JPA Entity (DB table)
│   └── dto/
│       ├── MemoryRequest.java            # Tool input DTO
│       └── MemoryResponse.java           # Tool output DTO
├── repository/
│   └── MemoryRepository.java            # Spring Data JPA
├── cache/
│   └── MemoryCacheService.java          # Redis read-through cache
├── search/
│   └── MemorySearchEngine.java          # TF-IDF in-memory search
├── service/
│   └── MemoryService.java               # Core business logic
├── security/
│   ├── SecurityConfig.java              # JWT + RBAC config
│   └── TenantContext.java               # Multi-tenancy thread-local
├── tool/
│   └── MemoryToolService.java           # @Tool MCP definitions
├── audit/
│   └── AuditAspect.java                 # AOP audit logging
└── config/
    ├── RedisConfig.java
    ├── MetricsConfig.java
    └── ResilienceConfig.java            # Circuit breaker, retry
```

## 5. API Contract

所有 MCP Tool 的输入输出契约（不随实现变化）：

### memory_set
```json
Input:  { "key": "str", "content": "str", "namespace?": "str", "tags?": ["str"], "pinned?": bool }
Output: "✅ Memory created: \"key\" (namespace: default)"
```

### memory_get
```json
Input:  { "key": "str" }
Output: { "key": "...", "content": "...", "tags": [...], "namespace": "...", ... }
```

### memory_search
```json
Input:  { "query": "str", "namespace?": "str", "limit?": int }
Output: "🔍 Found N result(s): [{ rank, key, content, score }, ...]"
```

### memory_context
```json
Input:  {}
Output: { "status": "ready", "totalMemories": N, "namespaces": {...}, "recentEntries": [...] }
```

### memory_delete
```json
Input:  { "key": "str" }
Output: "🗑️ Deleted: \"key\""
```

### memory_pin
```json
Input:  { "key": "str", "pinned?": bool }
Output: "📌 Memory pinned: \"key\""
```

## 6. Non-functional Requirements

| 指标 | 目标 | 度量方式 |
|------|------|---------|
| 读延迟 (P99) | < 10ms (cache hit) | Micrometer timer |
| 写延迟 (P99) | < 50ms | Micrometer timer |
| 可用性 | 99.9% | Actuator health |
| 并发用户 | 100+ | Virtual Threads |
| 单租户记忆上限 | 10,000 条 | DB count 检查 |
| 搜索结果延迟 | < 100ms | Timer |
| 启动时间 | < 5s | 日志时间戳 |
| 内存占用 | < 512MB | JVM metrics |

---

# Module 1: Data Model

## What
JPA entity and DTOs for memory storage with multi-tenancy support.

## Why
- Single source of truth for data shape across all layers
- JPA Entity maps to PostgreSQL table with proper indexes
- DTOs decouple API contract from storage schema
- Multi-tenancy baked in from day one (tenant_id on entity)

## Interface

### MemoryEntity (JPA)
```
Fields: id (UUID), tenantId, key, content, namespace, tags (JSONB),
        createdAt, updatedAt, lastAccessedAt, accessCount, pinned, version
Unique constraint: (tenantId, key)
Indexes: (tenantId, namespace), (tenantId, updatedAt DESC)
```

### MemoryRequest (input DTO)
```
Fields: key, content, namespace?, tags?, pinned?
```

### MemoryResponse (output DTO)
```
Fields: all entity fields except id and version (internal)
```

## Tradeoffs

| Decision | Choice | Alternative | Reason |
|----------|--------|-------------|--------|
| Tags storage | JSONB column | Separate join table | Simpler queries, good enough for search. Join table if we need tag-level indexes later |
| ID strategy | UUID | Auto-increment | Multi-instance safe, no ID collision |
| Optimistic lock | @Version | Pessimistic lock | Read-heavy workload, conflicts rare |
| Timestamp type | Instant | LocalDateTime | UTC always, no timezone bugs |

## Future
- Add `contentVector` column (float[]) when we add embedding search
- Add `expiresAt` for TTL-based auto-deletion
- Consider table partitioning by tenantId at >1M rows

---

# Module 2: Persistence (Repository)

## What
Spring Data JPA repository for MemoryEntity with tenant-scoped queries.

## Why
- Spring Data auto-generates SQL from method names — zero boilerplate
- Every query is tenant-scoped by design (no accidental cross-tenant leak)
- Pagination built-in for memory_list and context
- Custom queries for search fallback (DB LIKE when cache miss)

## Interface
```java
findByTenantIdAndKey(tenantId, key) → Optional<MemoryEntity>
findByTenantIdAndNamespace(tenantId, namespace) → List
findRecentByTenantId(tenantId, Pageable) → Page
countByTenantId(tenantId) → long
searchByKeyword(tenantId, keyword) → List  // ILIKE fallback
deleteByTenantIdAndKey(tenantId, key)
```

## Tradeoffs
- No raw SQL: Spring Data method naming is sufficient for MVP
- Full-text search: PostgreSQL `ILIKE` for now, not `tsvector` (simpler, good enough for <10K entries per tenant)
- No soft delete: hard delete is simpler; audit log preserves history

## Future
- Add `@Query` with PostgreSQL full-text search (`to_tsvector`, `ts_rank`)
- Read replica for search queries
- Custom batch operations for import

---

# Module 3: Cache (Redis)

## What
Redis-based read-through cache for individual memory entries.

## Why
- Read-heavy workload (memory_get, memory_search context): cache eliminates DB roundtrip
- Multi-instance deployment: shared cache across all Spring Boot instances
- Write-through: cache consistency guaranteed (write to DB + cache atomically)

## Strategy
- **Read**: cache hit → return | cache miss → DB → put cache → return
- **Write**: write DB → evict/update cache → return
- **Delete**: delete DB → evict cache
- **TTL**: 1 hour per entry (stale reads acceptable for memory system)

## Cache Key Design
```
memory:{tenantId}:{key}     → single entry
memory:ctx:{tenantId}       → context overview (short TTL: 30s)
```

## Tradeoffs
- Cache individual entries, NOT search results (search is complex, cache invalidation hard)
- Context overview cached with short TTL (30s) — acceptable staleness
- No cache warming on startup — lazy load, warms up naturally

## Future
- Caffeine L1 + Redis L2 two-level cache for ultra-low latency
- Cache search results with event-driven invalidation

---

# Module 4: Search Engine

## What
In-memory TF-IDF keyword search with tag matching, operating on lists of MemoryEntity.

## Why
- Core feature: users search memories by natural language query
- No external dependency (no ES in MVP) — pure Java, zero infra cost
- Same algorithm as TypeScript version for consistent cross-language behavior

## Interface
```java
List<ScoredResult> search(List<MemoryEntity> entries, String query, List<String> tags, int limit)
```

## Tradeoffs
- In-memory on full entity list: O(n) scan. Fine for <10K entries per tenant
- No stemming/lemmatization: prefix matching compensates for most cases
- No IDF (inverse document frequency): TF-only is simpler, good enough for small corpus
- Loads all tenant entries into memory for search: acceptable at MVP scale

## Future
- PostgreSQL `tsvector` full-text search for >10K entries
- Elasticsearch integration via interface swap (same `search()` signature)
- Embedding vectors + cosine similarity

---

# Module 5: Domain Service

## What
Core business logic layer. Orchestrates Repository, Cache, and Search.

## Why
- Single point for all memory operations — tools call service, service calls infra
- Transaction boundaries live here
- Cache coordination (read-through, write-invalidate) lives here
- Business rules (e.g., "max 10K entries per tenant") enforced here

## Interface
```java
MemoryEntity       get(tenantId, key)
MemoryEntity       set(tenantId, MemoryRequest)
void               delete(tenantId, key)
void               pin(tenantId, key, pinned)
List<ScoredResult> search(tenantId, query, tags, namespace, limit)
ContextOverview    context(tenantId)
List<MemoryEntity> list(tenantId, namespace, tags)
```

## Tradeoffs
- Service is synchronous (Virtual Threads handle concurrency at the thread level)
- No async/reactive: simpler code, Virtual Threads make blocking OK
- Cache miss → DB read → cache fill: one extra DB call on cold start, amortized over session
- search() loads all tenant entries into memory: fine for <10K, replace with DB search at scale

## Future
- Add @Retryable on write operations (optimistic lock conflict)
- Circuit breaker around cache calls (already graceful in CacheService)
- Event publishing for search index updates

---

# Module 6: Security

## What
JWT-based authentication + multi-tenant context extraction.

## Why
- Enterprise requirement: know who is calling what
- Multi-tenancy isolation: JWT carries tenantId, extracted per-request
- Stateless: no server-side session, scales horizontally

## Design
- JWT validated by Spring Security (signature + expiry)
- `tenantId` extracted from JWT claim `tenant_id` or `sub`
- Stored in `TenantContext` (ThreadLocal) for the request lifecycle
- All downstream service calls read from TenantContext, never from request params

## MVP Simplification
- JWT issuer/keys hardcoded in properties (no external IdP integration)
- Single role: authenticated = full access. RBAC deferred to post-MVP
- For local dev: a `/dev/token` endpoint generates test JWTs

## Tradeoffs
- ThreadLocal for tenant context: works with Virtual Threads (scoped values in future)
- No RBAC in MVP: every authenticated user can read/write/delete. Fine for team-internal use
- Actuator endpoints unprotected (health/metrics): acceptable for internal deployment

## Future
- Integrate with company IdP (Keycloak, Okta, Azure AD)
- RBAC: read-only vs read-write vs admin roles
- API key support as alternative to JWT (for CLI tools)

---

# Module 7: MCP Tool Layer

## What
@Tool annotated methods that map MCP protocol calls to MemoryService operations.

## Why
- Thin translation layer: MCP params → service call → format response string
- All error handling here (never let exceptions leak to MCP client)
- Tenant context extracted from TenantContext (set by JWT filter)
- No business logic — just parameter mapping and response formatting

## Design Rules
1. Every tool method reads tenantId from TenantContext.get()
2. Every tool method returns String (MCP protocol requirement)
3. Every tool method catches all exceptions and returns error message
4. Emoji prefix convention: ✅❌🔍📌🗑️📋

## Tradeoffs
- String return (not structured JSON): MCP limitation, LLM parses text fine
- No pagination in search results: limit param sufficient for MCP use case
- Tool descriptions in English: LLM prompt quality depends on description clarity

---

# Memory Store Service: Distributed Architecture Design

## 1. Background & Motivation

The Memory Store service is a core component for persistent, cross-session memory in the MCP platform. As the system scales to support large organizations (e.g., Taobao, Tencent), the current single-node, local-JSON-file approach is insufficient for reliability, scalability, and multi-tenant needs. This document proposes a distributed, microservice-based architecture for the Memory Store, with pluggable service discovery and storage backends.

## 2. Goals
- **High Availability (HA):** No single point of failure; support for failover and replication.
- **Horizontal Scalability:** Scale out with more nodes to handle increased load.
- **Multi-Tenancy:** Isolate data and access per tenant.
- **Pluggable Storage:** Support for different storage backends (e.g., Redis, Elasticsearch, distributed DBs).
- **Service Discovery:** Dynamic registration and lookup of service nodes (Nacos, SPI, etc.).
- **Observability & Resilience:** Integrate with Sentinel for circuit breaking, monitoring, and config.
- **Stateless Compute:** All compute nodes stateless; state is externalized.

## 3. Architecture Overview

### 3.1. Service Topology
- **Memory Store API Service:** Stateless microservice exposing memory CRUD/search APIs.
- **Distributed Storage Backend:** Pluggable via SPI (e.g., Redis cluster, Elasticsearch, RDBMS, etc.).
- **Service Registry:** Nacos (or similar) for node registration, discovery, and health checks.
- **Clients:** MCP agents and tools discover and connect to Memory Store nodes via registry.

### 3.2. Data Flow
1. Client requests memory operation (read/write/search).
2. Client discovers available Memory Store node via Nacos/SPI.
3. Node processes request, interacts with storage backend.
4. Node returns result to client.

### 3.3. SPI Extension Points
- **Service Discovery:** SPI interface for registry (Nacos, static, etc.).
- **Storage Backend:** SPI interface for storage (Redis, ES, file, etc.).

## 4. Key Components

### 4.1. MemoryStoreService (API Layer)
- Stateless REST/gRPC API.
- Handles authentication, tenant isolation, request routing.
- Delegates to storage backend via SPI.

### 4.2. StorageBackend SPI
- Interface: `MemoryStorageBackend`
- Implementations: `RedisMemoryBackend`, `ElasticsearchMemoryBackend`, `FileMemoryBackend` (for dev/testing)

### 4.3. ServiceDiscovery SPI
- Interface: `ServiceDiscovery`
- Implementations: `NacosDiscovery`, `StaticDiscovery`

### 4.4. Registry/Config
- Nacos for service registration, config, health checks.
- Sentinel for circuit breaking, monitoring, dynamic config.

## 5. Deployment & Scaling
- Deploy multiple stateless Memory Store nodes behind a load balancer.
- Nodes auto-register with Nacos.
- Storage backend can be clustered (e.g., Redis cluster, ES cluster).
- Rolling upgrades and scaling supported.

## 6. Multi-Tenancy & Security
- All APIs require tenant context.
- Data partitioned by tenant in storage backend.
- AuthN/AuthZ enforced at API layer.

## 7. Observability & Operations
- Metrics, tracing, and logging integrated with Sentinel and standard APM tools.
- Health checks and circuit breaking via Sentinel.

## 8. Migration Plan
- Phase 1: Abstract storage and discovery via SPI, keep file backend for dev.
- Phase 2: Implement Redis/ES backend, Nacos discovery.
- Phase 3: Deploy in distributed mode, test failover and scaling.

## 9. Risks & Mitigations
- **Data consistency:** Use strong consistency backends (e.g., Redis with persistence, ES with replicas).
- **Network partitions:** Use circuit breaking and retries.
- **Tenant isolation:** Strict partitioning and access control.

## 10. Open Questions
- Which storage backend(s) to prioritize?
- REST vs gRPC for API?
- How to handle schema evolution for memory data?

---

**Appendix: Example SPI Interfaces**

```java
public interface MemoryStorageBackend {
    void save(String tenant, String key, String value);
    String load(String tenant, String key);
    List<String> search(String tenant, String query);
    // ...
}

public interface ServiceDiscovery {
    List<String> getAvailableNodes(String serviceName);
}
```

---

**Summary:**
This design enables the Memory Store to scale, remain highly available, and support enterprise-grade requirements, while remaining flexible and pluggable for future needs.
