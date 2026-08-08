# Memory Server

Long-running Java 21 / Spring Boot service for tenant-scoped memories and
versioned Agent skills. It exposes an authenticated REST tool bridge used by the
Agent servers and a Spring AI MCP transport for local integrations.

## Current Runtime

| Concern | Current implementation |
|---|---|
| HTTP port | `8180` |
| REST tools | `/api/tools/memory_*` and `/api/tools/skill_*` |
| Authentication | JWT through a shared HMAC secret or Auth Service JWKS |
| Development database | H2 in-memory |
| Deployment database | PostgreSQL with Flyway migrations and optional RLS |
| Cache | Redis when available; no-op fallback when it is unavailable |
| Search | PostgreSQL full-text search with in-process TF-IDF fallback |
| Resilience | Per-tool rate limit, retry, and circuit breaker |
| Operations | Actuator health, metrics, Prometheus, audit logging, tracing |

The TypeScript stdio memory tool at the repository root is a separate local
implementation. This module does not persist data to
`~/.mcp-local/memory-store.json`.

## Quick Start

Prerequisites: Java 21 and Maven 3.9+.

From the repository root:

```bash
make build-common

cd platform/memory-server
JWT_SECRET=local-dev-secret-at-least-32-bytes \
  command mvn spring-boot:run \
  -Dmaven.repo.local=./tmp-m2-repo
```

The service starts at `http://localhost:8180`. The default profile uses H2 and
does not require PostgreSQL or Redis.

Check readiness:

```bash
curl http://localhost:8180/actuator/health
```

## Authentication

Startup fails unless at least one verifier is configured:

```bash
# Local HMAC mode
export JWT_SECRET=local-dev-secret-at-least-32-bytes

# Auth Service / RS256 mode
export MCP_SECURITY_JWKS_URL=http://localhost:8090/auth/jwks
```

All `/api/**` routes require a JWT with `ROLE_SERVICE`. Tenant identity comes
from the verified token; client-provided tenant headers are not trusted.

Example REST bridge call:

```bash
curl -X POST http://localhost:8180/api/tools/memory_search \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"checkpoint recovery","limit":5}'
```

The response envelope is `{ "result": "..." }`.

## Tool Endpoints

Memory tools:

- `POST /api/tools/memory_set`
- `POST /api/tools/memory_get`
- `POST /api/tools/memory_search`
- `POST /api/tools/memory_delete`
- `POST /api/tools/memory_context`
- `POST /api/tools/memory_list`
- `POST /api/tools/memory_pin`

Skill tools:

- `POST /api/tools/skill_set`
- `POST /api/tools/skill_get`
- `POST /api/tools/skill_list`
- `POST /api/tools/skill_history`
- `POST /api/tools/skill_rollback`
- `POST /api/tools/skill_feedback`
- `GET /api/tools/skill_triggers`

## PostgreSQL and Redis

`docker-compose.yml` starts the optional local PostgreSQL and Redis dependencies:

```bash
docker compose -f platform/memory-server/docker-compose.yml up -d
```

Configure the application with environment variables:

```bash
export SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:5432/memorydb
export SPRING_DATASOURCE_USERNAME=memory
export SPRING_DATASOURCE_PASSWORD=memory-dev-pwd
export SPRING_DATASOURCE_DRIVER_CLASS_NAME=org.postgresql.Driver
export SPRING_FLYWAY_ENABLED=true
export SPRING_JPA_HIBERNATE_DDL_AUTO=validate
export SPRING_DATA_REDIS_HOST=localhost
```

The checked-in passwords are local-development examples only. Do not reuse them
outside an isolated development environment.

## Security Boundary

- `/api/**` requires `ROLE_SERVICE`.
- `/actuator/health` and `/actuator/info` are public for orchestration probes.
- `/sse/**` and `/mcp/**` are currently public transport paths. Bind the service
  to a trusted network or protect these routes at an authenticated gateway before
  any internet-facing deployment.
- Health details are configured for local operations. Review Actuator exposure
  before production deployment.

## Testing

From the repository root:

```bash
make test-java
```

Focused module tests, after installing `mcp-common`:

```bash
cd platform/memory-server
command mvn test -Dmaven.repo.local=./tmp-m2-repo
```

Docker-backed integration tests are opt-in through the repository integration
test target.

## Documentation

- `docs/ARCHITECTURE.md` - current architecture and implementation status
- `docs/CONVENTIONS.md` - module coding conventions
- `docs/SKILL-STORE-DESIGN.md` - historical design draft; not an implementation contract
