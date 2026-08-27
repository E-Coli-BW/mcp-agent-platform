# Full Test and Local Runtime Design Validation - 2026-08-26

## Result

**Overall: PASS**

All non-skipped tests and builds passed after remediation.

| Outcome | Count |
|---|---:|
| Passed | 1,163 |
| Failed | 0 |
| Errors | 0 |
| Skipped | 1 |
| Total tests executed | 1,164 |

The earlier failure baseline is preserved in
[`FULL-TEST-REPORT-2026-08-26.md`](FULL-TEST-REPORT-2026-08-26.md).

## Remediation Results

| Earlier issue | Root cause | Fix | Verification |
|---|---|---|---|
| Python package-layout failures | Three untracked pre-refactor copies remained under `app.agent` | Removed retired root modules; canonical implementations remain in `agent_graph.nodes` and `delegation` | Package-layout tests `3/3` pass; agent-server suite passes |
| Memory integration rollback errors | `recordAccess()` used default `REQUIRED`, joining an outer read-only transaction; a caught telemetry-write error marked the user transaction rollback-only | Changed access-counter update to `REQUIRES_NEW` | Previously failing classes `24/24`; complete Docker suite `195/195` |
| `review-agent` dependency blocked | Obsolete Spring AI starter coordinate and removed `InMemoryChatMemory` API | Switched to `spring-ai-starter-model-ollama:1.1.6`, `MessageWindowChatMemory`, and advisor builder API | `review-agent` `9/9` pass |
| Root TypeScript build failed | Root config compiled every `platform/*/src`, including frontend TSX without frontend JSX/path settings | Restricted root `tsconfig.json` ownership to root `src` | Root TypeScript 5.6 compile passes; frontend build independently passes |

## Final Test Matrix

### Python

| Component | Result |
|---|---:|
| Agent server | 634 passed, 1 skipped |
| Agent SDK | 5 passed |
| Evaluation harness | 83 passed |

Python total: **723 executed: 722 passed, 1 skipped, 0 failed**.

### Java

| Module | Tests | Result |
|---|---:|---:|
| `mcp-common` | 6 | PASS |
| `common-spi` | 13 | PASS |
| `memory-server` unit + Docker integration | 195 | PASS |
| `auth-service` | 72 | PASS |
| `agent-server-java` | 63 | PASS |
| `codeexec-server` | 30 | PASS |
| `filesearch-server` | 23 | PASS |
| `completion-server` | 18 | PASS |
| `model-router` | 7 | PASS |
| `review-agent` | 9 | PASS |

Java total: **436/436 passed**.

### TypeScript And Frontend

| Surface | Result |
|---|---:|
| MCP CLI TypeScript + Node tests | 5/5 passed |
| Root TypeScript 5.6 compile | PASS |
| Frontend `tsc -b && vite build` | PASS |

The frontend still reports a non-fatal chunk warning: the main JavaScript asset
is 1,083.29 kB (366.20 kB gzip), above Vite's 500 kB warning threshold.

## Did The Borrowed Local-Agent Design Work?

**Yes, for the two mechanisms that were measured: deterministic direct routing
and cache-friendly rolling context.** The evidence combines deterministic graph
A/B tests with two real Ollama cold/warm runs.

### 1. Direct Routing A/B

Test: `platform/agent-server/tests/test_local_runtime_effect.py`

The same `read README.md` task ran through the complete LangGraph with an
identical tool and deterministic actor model.

| Mode | Actor model calls | Final answer |
|---|---:|---|
| Direct routing off | 2 | `README inspected` |
| Direct routing on | 1 | `README inspected` |

Result: **50% fewer actor calls with identical output**. The router replaces the
first model planning hop with a validated, read-only synthetic tool call. Write
and execution tools remain outside the direct-routing allowlist.

### 2. Rolling Context And Stable Prefix Contract

The deterministic context test verifies that a follow-up turn:

- retains the exact earlier `ToolMessage` rather than asking the model to reread;
- appends the new user message without rewriting history while under budget;
- keeps the leading system-prefix SHA-256 unchanged;
- performs no compression in the measured 20,000-character budget.

The local runtime profile test also verifies that `spawn_subagent` is removed
from the bound tool set and that its context/direct-routing settings reach graph
construction.

### 3. Real Qwen 2.5 7B Cold/Warm Runs

Environment:

- Ollama `qwen2.5:7b`;
- current `coding-agent.yaml` local profile;
- repository explicitly configured as the agent workspace;
- optional remote memory/code-exec services disabled;
- prompt 1: `read README.md`;
- prompt 2: use the retained README content to name the runtime components;
- the automatic runner unloaded Ollama before every pair and retained all pairs.

Both first turns successfully read 3,082 characters from the real root README.
Both follow-up turns answered without another file tool call. No tool errors or
model retries occurred.

| Metric | Cold sample A | Warm A | Change | Cold sample B (model reset) | Warm B | Change |
|---|---:|---:|---:|---:|---:|---:|
| Prompt tokens | 2,884 | 3,039 | +5.4% | 2,884 | 3,039 | +5.4% |
| Prefill time | 18,439 ms | 108 ms | **-99.4%** | 14,719 ms | 123 ms | **-99.2%** |
| Internal actor-stream TTFT | 22,572 ms | 310 ms | **-98.6%** | 18,042 ms | 422 ms | **-97.7%** |
| HTTP latency | 31,211 ms | 3,381 ms | **-89.2%** | 26,525 ms | 3,888 ms | **-85.3%** |
| Actor model calls | 1 | 1 | unchanged | 1 | 1 | unchanged |
| Stable prefix match | yes | yes | maintained | yes | yes | maintained |

The important discriminator is that the warm prompt contained **more** tokens,
not fewer, while prefill and internal TTFT fell by roughly 98-99%. That rules out
a shorter logical prompt as the sole explanation and is consistent with
server-side prefix reuse under this protocol; it is not an isolated causal
estimate of the refactoring. The prefix was 4,910 characters with the same
SHA-256 in both turns.

### 4. Observability Worked

The benchmark did not infer timings from HTTP duration. The runtime captured
provider-native Ollama metadata for every model call:

- load, prefill, decode, and total durations;
- prefill/decode token counts and throughput;
- streamed or estimated TTFT marker;
- system-prefix hash and character count;
- run/request IDs used to join chat responses to durable events.

This made provider cache contamination visible: an additional new-session run
hit Ollama's global prompt cache before its first request. It was excluded from
the cold comparison instead of being mislabeled as an independent cold sample.

## Scope And Caveats

- End-to-end HTTP reduction includes fewer completion tokens in the follow-up,
  so prefill and TTFT are the cleaner cache-effect measurements.
- The A/B direct-routing result is deterministic and controls for answer quality;
  the live runs validate the actual Ollama/runtime integration.
- `AGENT_MLX_ENABLE_THINKING=false` is covered by model-factory contract tests,
  but no MLX/Qwen 3.6 endpoint was running, so this report makes no live
  performance claim for reasoning-off or int8/MTP inference optimizations.
- Tool-call progress filtering is covered by safety tests, including rejection
  of tagged and plain-text reasoning, but it was not enabled in the live runs.
- Remaining warnings are non-blocking: Starlette/httpx deprecation, Pydantic
  class-config deprecation, Mockito dynamic-agent loading, and frontend bundle
  size.

## Conclusion

The borrowed design is not merely wired: its two central agent-harness claims
are observed.

1. Deterministic read routing removes one of two actor calls without changing
   the answer.
2. Append-only rolling context with a stable prefix lets a larger follow-up
   prompt reuse prior processing, reducing real local-model prefill and TTFT by
   approximately 98-99% in two cold-start samples.

All repository test gates are green after the accompanying remediation.
