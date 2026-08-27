# RCA: Local Coding Agent Runaway Generation and macOS Watchdog Reset

**Incident date:** 2026-08-26<br>
**Environment:** Apple M4 Pro, 24 GB unified memory, macOS 26.6.2<br>
**Severity:** SEV-1 for the affected local workstation<br>
**Status:** Root cause identified; P0 safeguards implemented; guarded live retest pending

## Executive Summary

A request to read the repository and explain its architecture caused the local Coding Agent to remain in `Thinking...` for several minutes. The third Ollama model call failed to emit an end-of-sequence token and generated at least 8,681 completion tokens. The configured 4,096-token limit was not included in the `ChatOllama` request, and the configured 60-second LLM timeout did not wrap the model invocation. Ollama therefore continued generating while repeatedly shifting its 4,096-token context window.

The workstation was already under memory pressure from the local 7B model, the MCP application stack, optional observability/search infrastructure, and an unrelated Docker stack. Ollama reported only 4.2 GiB of free system memory before allocating approximately 4.5 GiB of Metal model/context/compute memory. Docker Desktop reported host memory pressure during the runaway generation. Ollama and Docker monitoring stopped making progress at approximately 22:15:53, and macOS rebooted at 22:17 with a watchdog/reset crash reason.

The application stall is proven by provider logs and code inspection. The system reset is proven to be a watchdog reset. Resource exhaustion combined with sustained Metal inference is the high-confidence trigger, but the absence of a kernel panic report prevents attribution to a specific macOS or Metal subsystem.

## User Impact

- The second chat request never produced a completed response.
- The UI remained in `Thinking...` without a way to stop generation.
- The workstation became unresponsive and restarted.
- All local Java, Python, Node, and Docker processes were terminated.
- The Agent's in-memory run timeline and stderr logs were lost at restart.

No repository corruption was observed. Docker reported the affected containers as exit code 255 with `OOMKilled=false`, consistent with loss of the host rather than a container-level OOM kill.

## Timeline

All times are local time (`UTC+08:00`).

| Time | Event |
|---|---|
| 21:59:43 | README request loads Qwen2.5 7B. Ollama reports 3.6 GiB free system memory and no free swap. |
| 22:00:33 | README request completes successfully. |
| 22:01:28 | Docker Desktop reports `host memory pressure: signalled guest drop-caches`. |
| 22:07:00 | Architecture request reloads Qwen2.5 7B. Ollama reports 4.2 GiB free system memory. |
| 22:07:05 | Ollama reports a projected 4,521 MiB Metal allocation plus 328 MiB host allocation. |
| 22:07:23 | First model step completes: 2,156 prompt tokens, 73 completion tokens. |
| 22:07:37 | Second model step completes: 2,665 prompt tokens, 200 completion tokens. |
| 22:07:37 onward | Third model step starts with 2,881 prompt tokens and does not terminate. |
| 22:10:48 | Docker Desktop reports host memory pressure again. |
| 22:15:53 | Ollama's final record shows 8,681 generated tokens; no release or HTTP completion follows. Docker monitoring also stops normal progress. |
| 22:17:01 | New macOS boot begins. `last` has no matching orderly shutdown record. |
| After boot | `kern.shutdownreason` contains `wdog/reset crash`; no kernel panic report is present. |

## Evidence

### Provider evidence

The retained Ollama log at `~/.ollama/logs/server-2.log` shows:

- model file size: 4.36 GiB;
- projected Metal allocation: 4,521 MiB;
- context size: 4,096 tokens;
- third-call generation reaching at least 8,681 tokens;
- four context shifts after the active sequence filled the context window;
- no final timing summary, slot release, or HTTP 200 completion for the third call.

### Host evidence

- Docker Desktop's retained monitor log records host memory pressure at 22:01:28 and 22:10:48.
- `kern.shutdownreason` reports a watchdog/reset crash.
- `last reboot shutdown` records a reboot at 22:17 without a preceding orderly shutdown.
- The data volume retained approximately 49 GiB free, excluding disk exhaustion.
- Affected containers have `OOMKilled=false`, excluding a container-level OOM as the direct reset mechanism.

### Code evidence

Before remediation:

- `app/agent/model/factory.py` passed `max_tokens` to OpenAI-compatible providers but did not pass Ollama's equivalent `num_predict`.
- `app/config.py` defined `llm_timeout=60`, but `app/agent/model/request_recovery.py` used it only to size a durable retry lease.
- `model.ainvoke()` had no timeout boundary.
- `agents/coding-agent.yaml` declared `guardrails.max_tokens`, but graph construction did not apply that value to the model.
- `platform/frontend/src/components/ChatPanel.tsx` exposed no root-run cancellation control.
- `app/logging_config.py` wrote only to stderr, while the default run recorder was in memory.

## Root Cause

The primary application root cause was an incomplete provider abstraction: the generic generation budget was not translated to Ollama's provider-native `num_predict` option. When Qwen2.5 failed to emit EOS, Ollama's context-shift behavior allowed generation to continue beyond the context length and intended output budget.

The primary containment failure was that the declared LLM timeout was not an execution timeout. There was no server-side deadline capable of cancelling the active provider request.

The workstation reset was enabled by insufficient resource headroom. A 4.5 GiB Metal allocation was admitted while only 4.2 GiB of system memory was reported free, alongside multiple JVMs, Docker infrastructure, IDEs, and an unrelated container stack. Host memory-pressure events and the watchdog reset align temporally with the unbounded inference.

## Five Whys

1. **Why did the UI remain in `Thinking...`?**

   The final model call never completed, so the runtime never emitted the buffered final answer.

2. **Why did the model call never complete?**

   The model did not emit EOS and Ollama received no `num_predict` output limit.

3. **Why did the configured limit not stop it?**

   The provider factory mapped `max_tokens` for OpenAI-compatible providers only, and the agent YAML limit was not passed into model construction.

4. **Why did the 60-second timeout not stop it?**

   `llm_timeout` controlled only retry-checkpoint lease duration; `model.ainvoke()` was awaited without a deadline.

5. **Why did this escalate from one stuck request to a workstation reset?**

   The full local stack left inadequate unified-memory headroom, there was no cancellation control, and sustained Metal generation continued under host memory pressure until the system watchdog reset the machine.

## Corrective Actions

### Completed P0

| Action | Implementation | Verification |
|---|---|---|
| Enforce an Ollama output limit | Map the effective budget to `ChatOllama(num_predict=...)`. | Provider factory unit test. |
| Enforce agent-specific budget | Apply `guardrails.max_tokens` during configured graph construction. | Model factory and runtime-profile tests. |
| Reduce local 7B output budget | Set Coding Agent output limit to 1,024 tokens. | Coding Agent config contract tests. |
| Enforce a real model deadline | Wrap each `model.ainvoke()` in `asyncio.timeout(llm_timeout)`. | Never-returning model timeout test. |
| Prevent timeout retry storms | Treat the enforced deadline as non-retryable. | Assert exactly one provider call after timeout. |
| Add user cancellation | Replace Send with Stop during generation; call the root-run cancel API and abort SSE. | Frontend type-check and production build. |
| Interrupt silent runs immediately | Race the runtime event wait against the root cancellation signal instead of polling every 15 seconds. | Silent fan-in cancellation test with a 0.5-second bound. |
| Close the initial Stop race | Read `X-Run-ID` from response headers before the first SSE status event. | Frontend type-check and production build. |
| Bound runtime response memory | Fail the run if final-answer buffering exceeds 64,000 characters, independent of provider token controls. | Chat protocol overflow test. |
| Fence durable retry ownership | Require the active lease owner for model-retry checkpoint transitions. | Stale-owner takeover test. |
| Preserve Agent logs | Write stderr plus a private 10 MiB x 5-backup rotating file at `.runtime/agent-server.log`. | Initial and post-rollover mode-0600 tests. |

### Required Before Live Retest

1. Start only Auth, Python Agent, and Frontend for the first guarded test.
2. Leave Nacos, Elasticsearch, Jaeger, Kafka UI, and unrelated Docker application stacks stopped.
3. Use `AGENT_RUNTIME_STORE_BACKEND=sqlite` so run timelines survive an Agent crash.
4. Confirm `.runtime/agent-server.log` is being written before sending a prompt.
5. Exercise a synthetic non-terminating model in tests before invoking the real 7B model.
6. During the first live run, capture memory pressure and process RSS; stop if the host enters sustained pressure.

### P1 Follow-up

- Apply the OpenAI-compatible request's `max_tokens` override, bounded by the agent/global ceiling.
- Add a whole-turn wall-clock budget in addition to the per-model-call deadline.
- Stream bounded final-answer progress instead of buffering all model text until graph completion.
- Persist the run recorder by default in the local development profile.
- Add a startup resource preflight and a documented lightweight/full-stack split.
- Add a browser-level test proving Stop terminates a real in-flight SSE request.

## Verification Results

- Model factory regression tests confirm the Ollama request receives the configured `num_predict` value.
- A never-returning fake model is cancelled at the configured deadline and is invoked exactly once.
- Model retry/recovery tests pass with the new timeout semantics.
- Secure rotating-log tests confirm file creation, retained content, and mode `0600`.
- Silent-run cancellation closes the active fan-in and reaches `CANCELLED` within the 0.5-second test bound.
- Runtime final-response buffering rejects provider output beyond 64,000 characters.
- An expired retry worker cannot transition a checkpoint after another worker claims its lease.
- Agent Server full suite: **642 passed, 1 skipped**.
- Frontend TypeScript check and Vite production build: **passed**.
- Ruff checks for all changed Python files: **passed**.

A real 7B guarded live retest has deliberately not been run yet. The previous full
stack remains stopped; live acceptance requires the minimal startup profile and
resource monitoring described below.

The rotating log contains bounded user-message and assistant-response previews.
It is sensitive local data, is stored under a mode-`0700` directory with mode-`0600`
files, and may retain approximately 60 MiB across the active file plus five backups.
Set `AGENT_LOG_FILE=` to disable file retention when that tradeoff is not acceptable.

Crash recovery deliberately waits for an existing retry lease to expire. A due
checkpoint with a live lease returns `lease held` plus `retry_after_ms`; stealing
that lease immediately would permit the original worker and recovery worker to
execute the same model step concurrently.

## Safe Retest Acceptance Criteria

The incident is considered resolved when all of the following hold:

- an intentionally non-terminating fake model is cancelled at the configured deadline;
- Ollama logs show `num_predict=1024` for the Coding Agent;
- the same architecture prompt completes or terminates safely without exceeding 1,024 output tokens;
- Stop changes the run to a terminal cancelled state and releases the Ollama runner;
- no Docker host-memory-pressure event occurs during the minimal-stack test;
- Agent logs and the SQLite run timeline remain readable after a forced Agent process termination;
- the full Python and frontend test suites pass.

## Lessons Learned

- Provider-neutral configuration is not enforcement until every provider-native request is inspected in tests.
- A retry lease is not an execution timeout.
- Context-window limits do not imply output limits when context shifting is enabled.
- Cooperative cancellation must be reachable from the UI.
- A successful functional smoke test is insufficient when local inference and infrastructure share unified memory.
- Incident evidence must survive the process and host it is intended to diagnose.