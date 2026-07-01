# Java vs Python Agent Server: Experiment Design & Strategic Analysis

## Part 1: Experiment Design

### Hypothesis

> Given the same LLM, same tools, and same workspace — the Java agent server
> (with the new streaming ReAct loop) should produce **identical correctness**
> and **comparable latency** to the Python agent server.

### Pre-Registered Hypotheses

| ID | Hypothesis | Threshold | Rationale |
|----|-----------|-----------|-----------|
| H1 | Java pass rate within ±10pp of Python | |java - python| ≤ 10pp | Same LLM + same tools → same decisions |
| H2 | Java median latency within ±20% of Python | ratio ∈ [0.8, 1.2] | JVM startup is one-time; streaming overhead is similar |
| H3 | Java tool_call count within ±1 of Python per case | |Δ| ≤ 1 | Same prompt → same tool selection |
| H4 | Same SSE event types emitted in same order | exact match | Protocol compatibility requirement |

### Controlled Variables

```
┌─────────────────────────────────────┐
│ FIXED (identical for both servers)  │
├─────────────────────────────────────┤
│ • LLM: Ollama qwen2.5:7b           │
│ • Temperature: 0.0 (deterministic)  │
│ • Tool backends: same memory/exec   │
│ • Workspace: same filesystem path   │
│ • JWT: same auth token              │
│ • Eval cases: same golden YAML      │
│ • Repetitions: 5 per case           │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│ VARIED (independent variable)       │
├─────────────────────────────────────┤
│ • Server: Python (port 8500)        │
│           Java   (port 8580)        │
└─────────────────────────────────────┘
```

### Test Matrix (7 cases × 5 reps × 2 servers = 70 runs)

| Case | Tests | Why |
|------|-------|-----|
| `no_tool_simple_chat` | Zero-tool smoke test | Baseline: can both servers just relay LLM text? |
| `code_run_basic` | Remote code execution | Tool calling fidelity (args serialized correctly?) |
| `efficient_memory_recall` | Memory search accuracy | REST bridge tool (McpRestClient) correctness |
| `memory_set_then_search` | Write + read memory | Multi-turn tool sequence |
| `lift_memory_recall_across_session` | Cross-session persistence | Conversation store + checkpointing |
| `subagent_avoid_overuse_single_file` | Subagent governance | Budget envelope enforcement |
| `subagent_parallel_file_summary` | Subagent fleet | Spawn + fan-out + answer synthesis |

### Run Protocol

```bash
# 1. Start infrastructure (one-time)
docker-compose up -d redis ollama memory-server codeexec-server

# 2. Pre-warm the LLM (one throwaway request to each server)
curl -s http://localhost:8500/health
curl -s http://localhost:8580/health

# 3. Start Python server (port 8500)
cd platform/agent-server
AGENT_DEFAULT_MODEL=qwen2.5:7b AGENT_PORT=8500 \
  AGENT_AGENT_GRAPH_VERSION=v2 \
  .venv/bin/uvicorn app.main:app --port 8500 &

# 4. Start Java server (port 8580)  
cd platform/agent-server-java
AGENT_DEFAULT_MODEL=qwen2.5:7b SERVER_PORT=8580 \
  command mvn spring-boot:run \
  -s tmp-mvn-settings.xml -Dmaven.repo.local=./tmp-m2-repo &

# 5. Run the experiment
cd platform/eval-harness
python java_vs_python_experiment.py \
  --python-url http://localhost:8500 \
  --java-url http://localhost:8580 \
  --runs 5
```

### Measurements

| Metric | How Measured | Success Criterion |
|--------|-------------|-------------------|
| Pass rate | Golden assertion grading | H1: within ±10pp |
| Total latency | Wall clock: request → [DONE] | H2: within ±20% |
| Time to first token | Request → first content chunk | Informational |
| Tool call count | Count of tool_start SSE events | H3: within ±1 |
| SSE event types | Ordered list of event names | H4: same sequence |
| Token efficiency | response_length / 4 (approx) | Informational |
| Error rate | Requests that return error/timeout | Should be 0 for both |

### Expected Failure Modes

| Failure | Root Cause | Diagnosis |
|---------|-----------|-----------|
| Java pass rate < Python by >10pp | Context window management not compressing old tool messages → LLM "forgets" earlier results | Check `msgs` count in LLM call logs |
| Java latency > Python by >20% | Blocking tool execution on boundedElastic not yielding to SSE emitter fast enough | Profile thread scheduling |
| Java different tool count | System prompt differs or tool descriptions differ between servers | Diff the actual system prompts |
| Java wrong SSE format | Event naming mismatch (`tool_start` vs `toolStart`) | Compare raw SSE output byte-for-byte |

---

## Part 2: Architecture Decision — Hybrid Strategy

**Keep Python as the "agent brain" and Java as the "platform muscle."**

```
┌─────────────────────────────────────────────────────────┐
│ PYTHON: Agent intelligence layer                        │
│ • LangGraph ReAct loop (state machine)                  │
│ • Prompt engineering & system prompt iteration          │
│ • Reflexion / self-critique                             │
│ • Tool router heuristics                                │
│ • Subagent verifier                                     │
│ • Eval harness + golden case iteration                  │
│ • Plugin SDK (@tool decorator, ToolContext)             │
│                                                         │
│ WHY: These change WEEKLY based on eval results.         │
│ Python's iteration speed is the competitive advantage.  │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ JAVA: Platform infrastructure layer                     │
│ • Memory server (persistence, TF-IDF, multi-tenant)     │
│ • Auth service (JWT, JWKS, policy engine)               │
│ • Code execution (Docker sandbox, resource limits)      │
│ • File search (sandboxed FS, access control)            │
│ • Model router (load balancing, failover, cost caps)    │
│ • Kafka event bus (audit, analytics)                    │
│                                                         │
│ WHY: These change MONTHLY. Correctness > velocity.      │
│ Java's type safety and Spring ecosystem win here.       │
└─────────────────────────────────────────────────────────┘
```

### The Java Agent Server's Role

The Java agent server (`agent-server-java/`) is **NOT** a replacement for the Python server. It is:

1. **A drop-in fallback** — swap to Java with zero API change if needed.
2. **A load-testing vehicle** — JVM profiling tools (JFR, async-profiler) find streaming bottlenecks.
3. **A proof of concept** — demonstrates the platform CAN run without Python, de-risking the architecture.

### Bottom Line

> **Run the experiment first.** If Java is within ±10pp of Python on correctness
> (H1), keep both alive with the hybrid strategy. If Java significantly
> underperforms, it reveals gaps in the streaming loop that need fixing.
> Either way, the experiment produces actionable data instead of opinions.

