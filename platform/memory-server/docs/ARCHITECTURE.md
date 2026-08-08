# Memory Server Architecture

> Current implementation reference. Last reviewed: 2026-08-07.
>
> Status labels in this document are part of the contract:
> **Implemented** means reachable in the current source tree;
> **Optional** means conditionally activated by configuration;
> **Historical** belongs in a design draft, not this file.

## Scope

The module contains two related surfaces:

1. `com.example.memoryserver`: the Spring Boot HTTP/MCP service used by Agent
   runtimes for tenant-scoped memories and versioned skills.
2. `com.example.memorystore`: a smaller storage SPI and service-discovery layer
   with file, Redis, and Elasticsearch backends.

The primary public runtime is the `memoryserver` HTTP service on port `8180`.

## Runtime Topology

```text
Agent server / MCP client
          |
          | REST /api/tools/* or Spring AI MCP transport
          v
+----------------------------------------------------------+
| Memory Server (Spring Boot 3.4, Java 21, port 8180)      |
|                                                          |
| JWT filter -> tenant context -> tool bridge              |
|                    |                                     |
|          +---------+----------+                          |
|          |                    |                          |
|     MemoryService        SkillService                    |
|          |                    |                          |
|     Repository            Repository                     |
|       /      \                                         |
| PostgreSQL   H2/LIKE fallback                            |
|    FTS          |                                        |
|       \      TF-IDF                                    |
|          |                                               |
|     Redis cache (optional)                               |
+----------------------------------------------------------+
          |
          +-> audit logs / metrics / traces
          +-> Kafka analytics consumers (optional)
```

## Implemented Components

### Ingress

- Spring AI MCP server transport supplied by
  `spring-ai-starter-mcp-server-webflux`.
- Authenticated REST bridge under `/api/tools`.
- Memory endpoints: set, get, search, delete, context, list, and pin.
- Skill endpoints: set, get, list, history, rollback, feedback, and triggers.
- Tool responses use the REST envelope `{ "result": "..." }`.

### Security and tenancy

- Shared security configuration from `mcp-common`.
- Stateless JWT verification using either an HMAC secret or JWKS.
- Startup fails when neither verifier is configured.
- `/api/**` requires `ROLE_SERVICE`.
- Tenant identity is derived from verified JWT claims.
- Repository and service methods scope reads and writes by tenant.
- PostgreSQL RLS connection setup is optional behind
  `memory.security.rls.enabled=true`.

### Memory domain

- Versioned writes with optimistic locking and bounded conflict retries.
- Direct access-counter updates avoid mutating the versioned content entity on
  a hot read path.
- Namespace, tag, pin, recency, quota, and deletion operations.
- PostgreSQL `tsvector`/GIN full-text search when available.
- H2 and unsupported-database fallback through keyword search and the
  in-process TF-IDF scorer.

### Skill domain

- Separate versioned `skills` schema and repositories.
- Active, draft, and deprecated lifecycle states.
- Version history, rollback, trigger catalog, and success/failure feedback.
- JPA is the default repository path.

### Cache and transaction boundary

- `MemoryCache` is the abstraction used by the domain service.
- `MemoryCacheService` activates when a `StringRedisTemplate` is available.
- `NoOpMemoryCache` activates otherwise, so Redis is not required for service
  startup.
- Cache mutations are scheduled after successful database commit through
  `CacheAfterCommitExecutor`.

### Resilience and observability

- Resilience4j rate limiting and circuit breaking on selected tool endpoints.
- Retry policy for transient database failures.
- Actuator health, metrics, and Prometheus endpoints.
- Audit aspect and OpenTelemetry/Micrometer tracing.
- Kafka tool-event analytics activate only when
  `spring.kafka.bootstrap-servers` is configured.
- User lifecycle Kafka consumption activates only when
  `kafka.consumer.enabled=true`.

## Persistence Modes

### Development default

- H2 in-memory database.
- Hibernate `ddl-auto=update`.
- Flyway disabled.
- Redis optional with no-op fallback.

This mode is for local development and focused tests. It does not prove
PostgreSQL-specific FTS or RLS behavior.

### PostgreSQL deployment

- PostgreSQL datasource supplied through environment configuration.
- Flyway migrations enabled.
- Hibernate schema validation recommended.
- Migration V3 adds the generated `tsvector` column and GIN index.
- RLS can be enabled only with the PostgreSQL connection aspect configured.

### Repository implementation profile

- Default profile: JPA repositories.
- `mybatis` profile: MyBatis-Plus memory repository with LIKE search fallback.

## Storage SPI

The `com.example.memorystore` package is an additional implemented extension
surface:

```text
MemoryStoreService
    -> MemoryStorageBackend
         -> FileMemoryBackend          (default)
         -> RedisMemoryBackend         (optional)
         -> ElasticsearchMemoryBackend (optional)
    -> ServiceDiscovery
         -> StaticServiceDiscovery     (default)
         -> NacosServiceDiscovery      (optional)
```

`CircuitBreakerBackend` can wrap a backend with OPEN, HALF_OPEN, and CLOSED
state transitions. A single HALF_OPEN probe is enforced by its concurrency
contract.

This SPI is not the persistence implementation behind every
`MemoryService` REST operation; it is a separate extension surface in the same
Maven module.

## Security Boundary

Current route policy:

| Route | Policy |
|---|---|
| `/api/**` | JWT with `ROLE_SERVICE` |
| `/actuator/health`, `/actuator/info` | Public probe endpoints |
| `/sse/**`, `/mcp/**` | Public transport paths |
| Other routes | Denied |

The public MCP transport paths must be bound to a trusted network or protected
by an authenticated gateway before internet-facing deployment. Actuator health
is configured to show details for local operations and should be reviewed for a
production profile.

Checked-in database passwords and JWT values are local-test fixtures, not
production secrets.

## Failure and degradation behavior

| Dependency | Behavior when unavailable |
|---|---|
| Redis cache | Service continues through `NoOpMemoryCache` |
| PostgreSQL FTS on H2 | Search falls back to LIKE/in-process scoring |
| Kafka analytics | Consumers are not created unless configured |
| JWT/JWKS verifier | Application startup fails |
| Circuit breaker open | Tool endpoint returns an explicit unavailable result |

## Verification map

| Contract | Test evidence |
|---|---|
| Raw HTTP authentication | `RawHttpAuthTest`, `ApiAuthContractTest` |
| Tenant isolation | `TenantIsolationIntegrationTest` |
| Memory service behavior | `MemoryServiceTest`, `MemoryServiceIntegrationTest` |
| Search fallback | `MemorySearchEngineTest` |
| Skill versioning | `SkillServiceTest`, `SkillToolServiceTest` |
| Redis lock | `RedisDistributedLockTest` |
| Kafka idempotency | `UserEventConsumerTest` |
| Backend failover | `DistributedFailoverTest` |
| Circuit-breaker concurrency | `CircuitBreakerBackendConcurrencyTest` |

Run Java validation from the repository root with:

```bash
make test-java
```

Docker-backed integration tests are separate and must be invoked explicitly.

## Related documents

- `../README.md`: setup, endpoints, and operator-facing security notes.
- `CONVENTIONS.md`: local code conventions.
- `SKILL-STORE-DESIGN.md`: historical design rationale and future ideas; not a
  runtime status document.
