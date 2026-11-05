# Test Assurance Matrix

> Updated 2026-11-05. This matrix turns declared platform capabilities into
> executable production-wiring contracts. Unit tests remain necessary, but a
> capability is not accepted until its production composition and observable
> effect are tested.

## Assurance Model

Every supported capability needs four independent proofs:

1. **Declaration:** configuration/schema/manifest accepts only supported input.
2. **Composition:** the production factory, dependency injection, or loader
   constructs the real path with the required dependencies.
3. **Effect:** invocation through the public boundary produces an observable
   result, not a silent empty fallback.
4. **Failure boundary:** missing dependencies, invalid identity, and denied
   operations fail explicitly and do not execute side effects.

Tests cannot prove the absence of every defect. Confidence comes from combining
independent oracles: production composition tests, protocol contracts, process
smoke tests, hidden outcome tests, mutation tests, and live observability.

## Current Remediation Status

This is an implementation and validation inventory, updated after running
`make verify` on 2026-11-05. The aggregate passed: Java, Python, TypeScript,
frontend, production-wiring checks, MCP initialize handshakes, executable-JAR
health probes, and Python preflight. These checks do not prove every production
failure mode. In particular, plugin context still needs a real HTTP/SSE-to-tool
integration check. Java RAG now keeps bounded indexes per tenant/workspace and
serializes operations per index, but freshness uses a metadata walk on each
query (path, size, and mtime), so same-size edits with preserved timestamps can
be missed; a workspace revision or filesystem change feed is a stronger future
signal.

The intended PR-level aggregate gates are:

- `make test-wiring`: SDK/plugin/config, Java RAG/timeout/config, and Spring
  production-composition contracts.
- `make test-entrypoints`: package mapping, dead-tree architecture checks, MCP
  initialize handshakes, executable Spring Boot jars, and live health probes.
- `make test`: all deterministic Python, Java, TypeScript, frontend, wiring,
  and process-smoke coverage.

## Test Layers And CI Cadence

| Layer | Purpose | CI cadence |
|---|---|---|
| Unit | Pure parsing, algorithms, state transitions | Every PR |
| Architecture/static | Unsupported fields, dead configuration, package entrypoint mapping | Every PR |
| Composition/wiring | Production loader/Spring context/registry with only external edges faked | Every PR |
| Contract | Client/server request, response, auth, and error schema compatibility | Every PR |
| Process smoke | Build artifact, spawn advertised entrypoint, readiness/handshake, terminate | Every PR |
| Vertical slice | Public request through production composition to observable effect | Every PR for deterministic fixtures |
| Security/negative | Deny, missing secret, tenant escape, unavailable backend, fail-closed behavior | Every PR |
| Mutation/fault injection | Prove tests fail when critical boundaries are deliberately broken | Nightly and release |
| Real-model E2E | n-of-k task outcome and trajectory attribution | Nightly/profile promotion |

## Audit Remediation Matrix

| ID | Capability | Acceptance test | Negative oracle | Resolution |
|---|---|---|---|---|
| P1 | Plugin knowledge | Load a synthetic plugin through the production loader; indexed fixture search returns a source | Missing retriever returns typed unavailable error, never `[]` | Tenant/collection-aware retriever factory wired |
| P2 | Plugin memory | Production-loaded plugin sets/searches/gets through the authenticated bridge contract | Missing service token or backend returns explicit error | Authenticated transport wired; `top_k` maps to bridge `limit` |
| P3 | Plugin permissions | JWT without declared permission invokes loaded tool; function-body sentinel remains untouched | Required permission absent -> deny before invocation | Manifest/decorator permissions enforced before the body |
| P4 | Plugin request context | Loaded plugin observes current tenant, session, identity permissions, and immutable run workspace | No active request context -> fail explicitly | Auth/request context is explicitly bound to the chat stream; real HTTP/SSE-to-tool propagation still needs integration validation |
| P5 | Plugin secrets | Plugin with required secret loads and receives it | Missing required secret prevents tool registration | Load and invocation both fail closed on missing secrets |
| P6 | Plugin manifest schema | Every accepted field changes production behavior | Unsupported class/config/language fails validation | Unsupported declarations and missing implementations rejected |
| C1 | Python model selection config | Changing supported model config changes constructed provider/model | Unsupported routing keys fail validation | Inert routing/strong-model declarations removed; directory load fails closed |
| C2 | Per-agent verification | Toggle profile and observe critic node enabled/disabled | Invalid boolean fails config load | Profile verification flag wired into graph composition |
| C3 | Rerank candidate pool | Override pool and observe bounded candidate count passed to reranker | Non-positive value rejected/clamped | Candidate-pool setting wired at retrieval boundary |
| J1 | Java RAG | Boot production Spring context, index fixture workspace, then `rag_search` returns fixture source | No index and failed refresh report explicit unavailable state | Bounded LRU indexes are keyed by tenant and normalized workspace; per-index locks allow independent workspaces to proceed. Metadata freshness checks the exact indexable files but walks the workspace for every query and can miss same-size edits with preserved mtimes. |
| J2 | Java YAML config | Boot with temporary YAML; model/tool/prompt change is observable; edit and await reload | Invalid/unsupported YAML fails loudly | Disconnected YAML loader/watcher surface retired |
| J3 | Java model timeout | Never-returning fake model is cancelled at configured deadline | Non-positive timeout rejected | Configured deadline enforced at model-call boundary |
| J4 | Java model tiers | Each supported model tier has a production consumer | Unused tier property is absent from schema | Inert strong/cheap model properties removed |
| T1 | MCP CLI entrypoints | Build, verify every `main/bin/start*` target exists, spawn and complete MCP initialize handshake | Missing target fails CI with package/script name | Paths corrected; MCP initialize handshakes and eight Spring Boot jar health probes passed. MCP smoke does not yet call `tools/list` or a real tool. |
| D1 | Alternate memorystore | Supported application context contains backend/service beans | If unsupported, no production docs/config claim it | Auto-configuration imported into production HTTP application; production composition test passed. |
| D2 | Root TypeScript facade | Every root build/start artifact maps to a non-empty source | Empty/missing entrypoint fails static check | Empty sources/config/start removed; root is a workspace aggregator |
| D3 | Duplicate `tools/memory-ts` | One canonical memory server source tree remains | CI rejects duplicate/dead documented roots | Duplicate retired; docs and scripts point to `platform/mcp-cli` |
| F1 | Auth subscription | Subscriber receives login/refresh/logout updates | If no consumer is intended, exported API is absent | Unconsumed listener API removed and architecture-checked |

## Concrete Test Suites

### `test-plugin-wiring`

- Use a temporary plugin manifest and module.
- Enter production auth and request contexts.
- Call `load_plugins`, resolve the registered LangChain tool, and invoke it.
- Fake only external Memory/RAG HTTP or retriever edges.
- Assert context identity, workspace, positive data, permission denial, missing
  secret rejection, and structured backend errors.

### `test-config-liveness`

For every public setting, compare two compositions:

```text
default A -> observed behavior A
override B -> observed behavior B
invalid value -> configuration/startup failure
```

A setting with no observable behavioral delta is either removed or marked as
unsupported; parsing alone is not acceptance.

### `test-entrypoints`

For every tracked `package.json` and executable Spring Boot artifact:

1. build in a clean dependency environment;
2. resolve `main`, `bin`, and every `start*` script target;
3. assert the target exists and is non-empty;
4. spawn with a hard timeout;
5. wait for health or MCP initialize handshake;
6. terminate the process group and assert cleanup.

### Java composition tests

- Use `ApplicationContextRunner` for bean/component-scan contracts.
- Use `@SpringBootTest` only for public HTTP vertical slices.
- Use Testcontainers for PostgreSQL/Redis semantics.
- Inject a never-returning model for deadline/cancellation behavior.

## AI-Generated Code Controls

- Contract and hidden-oracle directories require human/CODEOWNERS approval.
- The implementation author may propose tests, but those tests are not the sole
  acceptance oracle for the same change.
- A bug fix includes red-before/green-after evidence. For critical paths, run
  the regression test against the parent implementation or an equivalent
  deliberate mutation.
- Track capability-contract coverage and mutation score; line coverage alone is
  not a release signal.
- Silent fallbacks are forbidden at capability boundaries. Optional capability
  absence must be visible in readiness/capability status.
- Production factories must be exercised directly; tests must not replace the
  object whose wiring they claim to verify with a mock.

## Required CI Gates

### Pull request

1. lint/typecheck and unit tests;
2. architecture/config-liveness checks;
3. production composition and client/server contracts;
4. build-and-start smoke for every advertised artifact;
5. deterministic vertical slices and security negatives;
6. coding E2E preflight with hidden/control oracles.

### Nightly/release

1. all Java modules plus real PostgreSQL/Redis Testcontainers;
2. mutation tests for auth, tenant/workspace, plugin permissions, timeout,
   idempotency, and entrypoint checks;
3. dependency outage, process crash, retry, cancellation, and race injection;
4. weak/strong model n-of-k E2E and guard-activation comparison.

## Definition Of Done

A capability is done only when its declaration, production composition,
positive effect, negative boundary, telemetry, and artifact startup are covered
by independent tests. A green unit suite without these proofs means the code is
implemented, not integrated.
