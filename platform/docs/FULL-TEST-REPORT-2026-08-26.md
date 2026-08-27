# Full Test Report - 2026-08-26

> Baseline report preserved for before/after comparison. The failures below
> have been remediated; see
> [`FULL-TEST-REPORT-2026-08-26-FIXED.md`](FULL-TEST-REPORT-2026-08-26-FIXED.md)
> for the passing test matrix and local-runtime design A/B results.

## Result

**Overall: FAIL**

The complete executable test matrix ran across Python, Java, TypeScript, the
frontend production build, and an isolated PostgreSQL/Redis integration stack.
A total of **1,317 tests** executed:

| Outcome | Count |
|---|---:|
| Passed | 1,304 |
| Assertion failures | 4 |
| Runtime/setup errors inside test cases | 8 |
| Skipped | 1 |
| Total | 1,317 |

Two additional build surfaces did not reach a green state:

- `review-agent` was blocked during Maven dependency resolution, before tests.
- The root TypeScript project failed compilation because its root `tsconfig.json`
  includes frontend TSX sources without the frontend JSX/path configuration.

## Environment

| Tool | Version / state |
|---|---|
| macOS | local workstation |
| Java | OpenJDK 21.0.8 LTS |
| Maven | 3.9.9 |
| Python | 3.13.3 (`platform/agent-server/.venv`) |
| Node.js | 23.11.0 |
| npm | 10.9.2 |
| Docker | available |

The shell `mvn` command is an alias that injects a company Nexus settings file.
That Nexus was unavailable and returned HTML for Maven artifacts. Java tests
were therefore rerun with the real executable (`command mvn`), an isolated local
repository under `/tmp`, and a temporary Maven-Central-only settings file.

Ports `6379` and `5432` were already owned by unrelated, long-running Docker
containers. They were not stopped or reused. Integration tests used temporary
containers on ports `16379` and `15432`; those containers and their network were
removed after the run.

## Unit And Build Matrix

| Component | Command/surface | Result | Tests |
|---|---|---:|---:|
| Python agent server | `pytest tests` | **FAIL** | 629 passed, 2 failed, 1 skipped |
| Python agent SDK | `pytest tests` | PASS | 5 passed |
| Evaluation harness | `pytest tests` | PASS | 81 passed |
| `mcp-common` | Maven test/install | PASS | 6 passed |
| `common-spi` | Maven test/install | PASS | 13 passed |
| `memory-server` unit suite | Maven default test selection | PASS | 167 passed |
| `auth-service` | Maven test | PASS | 72 passed |
| `agent-server-java` | Maven test | PASS | 63 passed |
| `codeexec-server` | Maven test | PASS | 30 passed |
| `filesearch-server` | Maven test | PASS | 23 passed |
| `completion-server` | Maven test | PASS | 18 passed |
| `model-router` | Maven test | PASS | 7 passed |
| `review-agent` | Maven test | **BLOCKED** | 0 executed |
| MCP CLI | TypeScript compile + Node tests | PASS | 5 passed |
| Frontend | `tsc -b && vite build` | PASS | build only |
| Root TypeScript | root `tsconfig.json` compile | **FAIL** | build only |

Unit-test subtotal: **1,122 tests: 1,119 passed, 2 failed, 1 skipped**.

## Docker Integration

The final run used real PostgreSQL 16 and Redis Stack containers, with the test
connection URL, JDBC driver, dialect, username, password, and Redis port
explicitly overridden to match the isolated ports.

Result: **195 tests: 185 passed, 2 failed, 8 errors**.

Affected suites:

| Suite | Result |
|---|---|
| `ApiAuthContractTest` | 1 assertion failure |
| `MemoryServiceIntegrationTest` | 1 assertion failure, 5 errors |
| `TenantIsolationIntegrationTest` | 3 errors |

Primary symptoms:

- `ApiAuthContractTest.jwtTenantId_isolatesDataAccess`: tenant A could not see
  data it had written (`expected true, was false`).
- `MemoryServiceIntegrationTest.concurrentGets_onHotKey_neverConflict`: an
  `UnexpectedRollbackException` escaped from a concurrent read.
- Eight tests ended with `Transaction silently rolled back because it has been
  marked as rollback-only`, covering round trips, updates, pin/unpin,
  concurrency, deletion, and tenant isolation.

These failures occur after the infrastructure and JDBC configuration are valid,
so they should be treated as application transaction/isolation failures rather
than Docker startup failures.

## Failure Details

### 1. Python Package Layout

Both Python failures come from three files that were already untracked before
this test run:

- `platform/agent-server/app/agent/reflexion.py`
- `platform/agent-server/app/agent/subagent.py`
- `platform/agent-server/app/agent/subagent_verifier.py`

They violate the package-layout contract requiring the `app.agent` root to expose
only `__init__.py`, and they reference retired root-module paths. The other 629
agent-server tests passed.

### 2. Review Agent Dependency Resolution

`review-agent` requests:

```text
org.springframework.ai:spring-ai-ollama-spring-boot-starter:1.1.6
```

The artifact was absent from both Maven Central and Spring Milestones, so no
review-agent tests could compile or execute.

### 3. Root TypeScript Configuration

Compilation with the repository's TypeScript 5-compatible toolchain produced:

- 53 `TS17004` errors: JSX is included but `jsx` is not configured.
- 6 `TS2307` errors: frontend `@/*` aliases resolve against root `src`, not
  `platform/frontend/src`.
- 1 `TS6142` error: a TSX module was resolved while JSX support was disabled.

The standalone frontend configuration is correct and its production build
passed. The defect is the root `tsconfig.json` including `platform/*/src` as if
all modules shared one compiler configuration.

## Warnings

- Frontend build output contains a `1,083.29 kB` JavaScript chunk (`366.20 kB`
  gzip), above Vite's 500 kB warning threshold.
- Python emitted Starlette/httpx and Pydantic class-config deprecation warnings.
- Java emitted Mockito dynamic-agent warnings for future JDK compatibility.
- The Redis Stack test image is `linux/amd64` on an ARM64 host; Docker used
  emulation successfully.
- `codeexec-server` tests intentionally exercised the development-only process
  sandbox and emitted its production-safety warning.

## Recommended Order

1. Resolve or remove the three retired `app.agent` root compatibility files so
   the Python package contract is green.
2. Investigate `memory-server` transaction rollback propagation and tenant
   visibility against PostgreSQL; start with the first transaction marked
   rollback-only rather than downstream `UnexpectedRollbackException` callers.
3. Correct the `review-agent` Spring AI starter artifact/version.
4. Narrow the root TypeScript project references or adopt project references so
   frontend TSX is compiled only by `platform/frontend/tsconfig.json`.
5. Address the frontend bundle split and dependency deprecation warnings after
   functional gates are green.

## Cleanup

- Temporary Maven settings, Maven cache, integration compose file, and JUnit
  XML were written under `/tmp` only.
- Temporary integration containers and network were removed.
- Temporary root/MCP CLI `node_modules` and root/MCP CLI/frontend `dist`
  outputs were removed.
- Unrelated pre-existing Docker containers were not modified.
- No source files were changed by test execution other than this report.
