# Memory Server — Architecture Reference

> Consolidated from the original MVP design fragments (modules 0-7)
> and the distributed memory store design.

---

# Memory MCP Platform — Enterprise MVP Design

## 1. MVP Scope Definition

### Core Problem
Upgrade the personal memory MCP server into an enterprise multi-tenant platform supporting team-shared memory, secure access, and observable operations.

### MVP must-have vs nice-to-have

| Feature | MVP | Rationale |
|---------|:---:|------|
| MCP SSE Transport | ✅ | core: multiple clients connect to one service |
| Multi-tenancy | ✅ | core enterprise requirement, designed in from day one |
| JWT Authentication | ✅ | most basic security requirement |
| PostgreSQL persistence | ✅ | JSON files aren't suited for concurrent writes |
| Redis cache | ✅ | read-heavy / write-light scenario, high cache value |
| TF-IDF search | ✅ | core feature, use the built-in implementation first |
| Health Check + Metrics | ✅ | minimum ops requirement |
| Retry + Circuit Breaker | ✅ | production-grade fault tolerance |
| Audit Log | ✅ | enterprise compliance requirement |
| Elasticsearch search | ❌ | later than MVP; use DB LIKE + in-memory TF-IDF first |
| Kafka async import | ❌ | MVP uses synchronous import, which is enough |
| Vector search / Embedding | ❌ | future direction, doesn't affect MVP |
| Spring Cloud Gateway | ❌ | MVP is single-service; add a gateway later |
| K8s deployment | ❌ | MVP uses Docker Compose |
| Canary release | ❌ | later than MVP |

### Tradeoff Notes

| Decision | Chosen | Rejected | Rationale |
|------|------|------|------|
| Search engine | built-in TF-IDF + DB LIKE | Elasticsearch | fewer deps for MVP; adding ES later doesn't change the API |
| Message queue | synchronous write | Kafka | import volume is small, synchronous is enough |
| Gateway | embedded Filter | standalone Gateway | a single service doesn't need a standalone gateway |
| Cache | Redis write-through | local Caffeine | multi-instance deployment needs a shared cache |
| Database | PostgreSQL | MySQL/MongoDB | great JSONB support, built-in full-text search |
| Authentication | JWT (stateless) | Session (stateful) | stateless, suits multiple instances |
| Multi-tenancy | shared table + tenant_id | separate schema/db | simple for MVP, enough at small data volumes |

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

## 3. Module Breakdown (implementation order)

Bottom-up by dependency order:

| # | Module | Files | Dependencies | Est. LOC |
|---|--------|------|------|---------|
| 1 | **Data Model** | `model/` | none | ~100 |
| 2 | **Persistence** | `repository/` | JPA, PostgreSQL | ~80 |
| 3 | **Cache** | `cache/` | Redis | ~60 |
| 4 | **Search** | `search/` | no external deps | ~80 |
| 5 | **Domain Service** | `service/` | 2,3,4 | ~200 |
| 6 | **Security** | `security/` | Spring Security | ~100 |
| 7 | **MCP Tools** | `tool/` | 5 | ~150 |
| 8 | **Audit** | `audit/` | AOP | ~60 |
| 9 | **Observability** | `config/` | Actuator, Micrometer | ~50 |
| 10 | **Docker Compose** | `docker/` | — | ~30 |

**Total: ~900 lines of Java + configuration**

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

Input/output contract for all MCP Tools (independent of implementation):

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

| Metric | Target | Measurement |
|------|------|---------|
| Read latency (P99) | < 10ms (cache hit) | Micrometer timer |
| Write latency (P99) | < 50ms | Micrometer timer |
| Availability | 99.9% | Actuator health |
| Concurrent users | 100+ | Virtual Threads |
| Per-tenant memory limit | 10,000 entries | DB count check |
| Search result latency | < 100ms | Timer |
| Startup time | < 5s | log timestamps |
| Memory footprint | < 512MB | JVM metrics |

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
