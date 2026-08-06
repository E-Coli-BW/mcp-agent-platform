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

## Part 2: Should We Consolidate to Java?

### The Case FOR All-Java

| Advantage | Weight | Explanation |
|-----------|--------|-------------|
| **Unified stack** | ★★★★★ | 6 of 8 platform services are already Java. One build system, one deploy pipeline, one debugging mental model. |
| **Type safety at scale** | ★★★★ | As the fleet grows (10+ tools, multi-tenant governance), compile-time checks catch bugs that Python finds at runtime. |
| **JVM performance profile** | ★★★★ | Sustained throughput > Python for CPU-bound work (context compression, AST parsing, embedding math). GC-tuned for long-running servers. |
| **Spring ecosystem** | ★★★★ | Observability (Micrometer → Prometheus), circuit breakers (Resilience4j), config management (Spring Cloud), all native. |
| **Hiring/maintenance** | ★★★ | The team speaks Java. One fewer language in the repo means faster code review, less context switching. |
| **Deployment uniformity** | ★★★ | All services use same Dockerfile pattern, same health-check contract, same JVM tuning. Python uvicorn is the odd one out. |
| **Thread-per-request model** | ★★★ | Virtual threads (JDK 21) give Python-like async ergonomics with Java's debuggability. No colored-function problem. |
| **Compile-time DI** | ★★ | Spring's constructor injection catches wiring bugs at startup, not at the first request that hits a missing bean. |

### The Case FOR Keeping Python

| Advantage | Weight | Explanation |
|-----------|--------|-------------|
| **LangGraph maturity** | ★★★★★ | LangGraph has 2+ years of battle-testing. Spring AI's tool loop is 6 months old with known rough edges. |
| **Ecosystem velocity** | ★★★★★ | LangChain/LangGraph ships weekly. New model adapters, new tool protocols, new agentic patterns. Spring AI ships quarterly. |
| **Rapid prototyping** | ★★★★ | Python's dynamic typing + hot-reload lets you try a new tool in 5 minutes. Java requires recompile + restart. |
| **ML/AI ecosystem** | ★★★★ | Reranker models, embedding fine-tuning, eval scripts — all native Python. Java wrappers add a translation layer. |
| **Community & hiring** | ★★★★ | The AI/ML talent pool overwhelmingly uses Python. LangChain stackoverflow answers are all Python. |
| **LangGraph state machine** | ★★★★ | The explicit v2 StateGraph (inject → route → call_llm → critique → tools → compress → loop) has no Java equivalent. Spring AI's ChatClient is a black box. |
| **Eval harness integration** | ★★★ | The eval-harness, agent-sdk, and plugin system are all Python. Moving to Java means rewriting the entire tooling layer. |
| **Subagent verifier/reflexion** | ★★★ | C1/C2/C3 features are 900 lines of Python with sophisticated LLM-as-judge patterns. Porting introduces re-validation risk. |

### Decision Matrix

```
                     Python Agent        Java Agent
                    ┌────────────┐      ┌────────────┐
  Correctness      │  Proven     │      │  TBD (H1)  │
  Latency          │  Baseline   │      │  TBD (H2)  │
  Features         │  100%       │      │  ~60%      │
  Maintainability  │  Different  │      │  Same stack │
  Ecosystem fit    │  AI/ML      │      │  Enterprise │
  Team velocity    │  Fast proto │      │  Fast deploy│
                    └────────────┘      └────────────┘
```

### Recommendation: Hybrid Strategy (Not All-Java)

**Keep Python as the "agent brain" and Java as the "platform muscle."**

The right boundary is:

```
┌─────────────────────────────────────────────────────────┐
│ PYTHON: Agent intelligence layer                        │
│ • LangGraph ReAct loop (state machine)                  │
│ • Prompt engineering & system prompt iteration          │
│ • Reflexion / self-critique (C1)                        │
│ • Tool router heuristics (C2)                           │
│ • Subagent verifier (C3)                                │
│ • Eval harness + golden case iteration                  │
│ • Plugin SDK (@tool decorator, ToolContext)             │
│ • Rapid experimentation with new model adapters         │
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
│ • Session LANE (Redis-backed locking)                   │
│ • Kafka event bus (audit, analytics)                    │
│ • Rate limiting, circuit breakers                       │
│                                                         │
│ WHY: These change MONTHLY. Correctness > velocity.      │
│ Java's type safety and Spring ecosystem win here.       │
└─────────────────────────────────────────────────────────┘
```

### When to Reconsider (Triggers for Full Java Migration)

| Trigger | Condition | Action |
|---------|-----------|--------|
| Spring AI reaches LangGraph parity | Native StateGraph equivalent, reflexion hooks, verifier pattern | Re-evaluate in 12 months |
| Python eval shows Java is strictly better | H1 Java > Python AND H2 Java ≤ Python | Migrate agent brain |
| Team grows past 8 engineers | Context switching cost > Python velocity gain | Standardize on one language |
| Regulatory compliance requires | Single-language static analysis for SOC2/ISO | Forced migration |
| Python LangGraph breaks backward compat | Major rewrite required anyway | Piggyback on migration |

### The Java Server's Role Today

The Java agent server is **NOT** a replacement. It is:

1. **A drop-in fallback** — if the Python server has a dependency conflict or GIL issue under load, swap to Java with zero API change.
2. **A load-testing vehicle** — JVM profiling tools (JFR, async-profiler) are better than Python's for finding streaming bottlenecks.
3. **A proof of concept** — demonstrates the platform CAN run without Python, which de-risks the long-term architecture.
4. **A teaching tool** — seeing the same agent logic in two languages deepens understanding of what's essential vs incidental.

### Bottom Line

> **Run the experiment first.** If Java is within ±10pp of Python on correctness
> (H1), keep both alive with the hybrid strategy. If Java significantly
> underperforms, it reveals gaps in the streaming loop that need fixing.
> Either way, the experiment produces actionable data instead of opinions.

The 60-minute experiment (70 runs, ~2 mins each including tool calls) will give
you definitive numbers. Don't consolidate based on architectural taste —
consolidate based on measured capability.
