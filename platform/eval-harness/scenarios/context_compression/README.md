# Context Compression & Observability

> **Status**: Implemented (Phase 4-6)  
> **Depends on**: Skill Store (Phase 1-3)

## Overview

Long multi-turn agent sessions (especially debugging) scatter critical information across dozens of turns. When the context window fills up, naive truncation loses important facts. This feature adds:

1. **Intelligent Context Compression** — tiered strategy that preserves critical facts while aggressively compressing noise
2. **Structured Investigation State** — extracts confirmed facts, eliminations, and artifacts that survive any compression
3. **Observability** — structured decision logs tracking what was compressed, why, and what was retained
4. **Eval Harness** — benchmarks measuring compression quality (fact retention, redundant rework)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  LangGraph StateGraph (graph_v2.py)                              │
│                                                                  │
│  ... → tools → track_errors → compress_history →                 │
│        ┌──────────────────────────────┐                          │
│        │  context_compressor (NEW)    │ ← fires at 75% budget   │
│        │  - estimate tokens           │                          │
│        │  - classify messages         │                          │
│        │  - compress middle zone      │                          │
│        │  - inject investigation state│                          │
│        └──────────────────────────────┘                          │
│        → call_llm → ...                                          │
│                                                                  │
│  AgentState:                                                     │
│    messages         ← sliding window (compressed)                │
│    investigation_summary  ← structured facts (NEW)               │
│    compression_summary    ← what was dropped (NEW)               │
└─────────────────────────────────────────────────────────────────┘
```

### Compression Strategy (Tiered)

```
┌─────────────────────────────────────────────────────────────┐
│  HEAD: First HumanMessage (original goal) — NEVER dropped    │
├─────────────────────────────────────────────────────────────┤
│  INVESTIGATION STATE: Injected facts block — ALWAYS present  │
│  (confirmed_facts, eliminated hypotheses, key artifacts)     │
├─────────────────────────────────────────────────────────────┤
│  MIDDLE: Compressed zone                                     │
│  - Skill activations → "[Applied skill:key]" (one-liner)    │
│  - Failed tools → first line only                            │
│  - Large tool outputs (>500 chars) → truncated to 200 chars  │
│  - Large AI responses → truncated with "...[truncated]"      │
│  - User messages → kept (intent signals)                     │
├─────────────────────────────────────────────────────────────┤
│  TAIL: Last 6 messages — kept verbatim (active context)      │
└─────────────────────────────────────────────────────────────┘
```

### Investigation State

The `InvestigationState` is a structured extraction that survives compression:

```python
@dataclass
class InvestigationState:
    goal: str                      # "Fix NPE in UserService.save()"
    confirmed_facts: list[str]     # ["NullPointerException at UserService.java:142"]
    current_hypothesis: str        # "Race condition in event loop"
    eliminated: list[str]          # ["read_file: ❌ config.yaml not found"]
    key_artifacts: dict[str, str]  # {"UserService.java": "NPE at line 142"}
    skills_used: list[str]         # ["maven-stale-jar-fix"]
    next_steps: list[str]          # ["Check if address is null"]
```

Facts are extracted automatically from:
- Stack traces (Java, Python, Go error patterns)
- AI assertions ("the root cause is...", "confirmed:...")
- Tool failures (tracked as eliminations)
- Skill activations

---

## How to Run

### Run the Eval Suite

```bash
cd platform/agent-server

# Run all scenarios, generate JSON report + HTML dashboard
.venv/bin/python ../eval-harness/scenarios/context_compression/run_eval.py \
  --dashboard -o /tmp/eval-report.json

# Run a specific scenario
.venv/bin/python ../eval-harness/scenarios/context_compression/run_eval.py \
  --scenario debug_needle --dashboard -o /tmp/eval-report.json

# Open the dashboard
open /tmp/eval-report.html
```

### What the Dashboard Shows

- **Summary cards**: Pass rate, total scenarios, pass/fail counts
- **Per-scenario metrics**: fact_recall, compression_ratio, file_coverage
- **Token usage chart**: Before vs. after compression (visual bars)
- **Decision events**: Expandable section with every compression decision

### Run Unit Tests

```bash
# Python — compressor logic + observability
cd platform/agent-server
.venv/bin/python -m pytest tests/test_compressor.py tests/test_observability.py -v

# Java — Skill Store (underlying persistence)
cd platform/memory-server
command mvn test -s tmp-mvn-settings.xml -gs tmp-mvn-settings.xml \
  -Dmaven.repo.local=./tmp-m2-repo -Dtest="SkillServiceTest,SkillToolServiceTest"
```

---

## Eval Scenarios

| Scenario | What It Tests | Pass Criteria |
|----------|--------------|---------------|
| `debug_needle` | Recall a critical stack trace line after compression | Both "142" and "UserService" retained |
| `hypothesis_chain` | Retain 4 eliminated hypotheses + reasons | ≥75% of hypotheses recalled |
| `multi_file_debug` | Remember all 4 affected files after compression | ≥3 files retained |

### Metrics Collected

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `fact_recall` | Critical facts retained / total critical facts | < 0.85 |
| `compression_ratio` | tokens_after / tokens_before | < 0.3 (too aggressive) or > 0.9 (not compressing) |
| `reasoning_chain_integrity` | Eliminated hypotheses retained | < 0.75 |
| `file_coverage` | Number of files remembered post-compression | < 3/4 |
| `redundant_rework` | Steps repeated after compression | > 0 |

---

## Observability

### Decision Logs

Every compression, skill activation, and fact extraction emits a structured event:

```python
from app.observability.decision_log import get_decision_logger, CompressionEvent

logger = get_decision_logger()
logger.log_compression(CompressionEvent(
    session_id="abc-123",
    turn_number=22,
    trigger="token_budget_exceeded",
    before_tokens=5000,
    after_tokens=2000,
    messages_dropped=8,
    messages_summarized=12,
    facts_retained=["NPE at UserService.java:142"],
))
```

Events are emitted as:
- **Structured log lines** (always — for log aggregation/grep)
- **Kafka topics** (production — `agent.decisions.compression`, `agent.decisions.skill_activation`)
- **In-memory buffer** (testing — `logger.get_buffer()`)

### Event Types

| Event | When | Key Fields |
|-------|------|-----------|
| `CompressionEvent` | Context compressor fires | before/after tokens, messages dropped, facts retained |
| `SkillActivationEvent` | Skill auto-surfaced (Layer 2/3) | skill_key, layer, match_score, match_reason |
| `FactExtractionEvent` | Investigation state updated | new_facts, new_eliminations |
| `TokenBudgetEvent` | Every turn (token tracking) | total_tokens, budget_tokens, usage_pct |
| `StateSnapshot` | Key state mutations | message_count, token_estimate, investigation_summary |

### Metrics (Prometheus-compatible)

```python
from app.observability.decision_log import get_agent_metrics

metrics = get_agent_metrics()
metrics.record_compression(ratio=0.4)
metrics.record_skill_activation(layer=2)
metrics.to_dict()  # Export for /metrics endpoint
```

---

## File Layout

```
platform/agent-server/
  app/
    agent/
      compressor.py           # Context compression logic + InvestigationState
      skill_activation.py     # Skill auto-activation (Layer 2 & 3)
      graph_v2.py             # Updated: context_compressor node wired in
    observability/
      __init__.py
      decision_log.py         # Structured event logging + metrics
  tests/
    test_compressor.py        # 14 tests: compression, investigation, extraction
    test_observability.py     # 12 tests: event logging, metrics, singletons

platform/eval-harness/
  scenarios/
    context_compression/
      scenarios.yaml          # Scenario definitions + baselines + references
      run_eval.py             # Eval runner + HTML dashboard generator

platform/memory-server/
  docs/
    SKILL-STORE-DESIGN.md     # Design doc (updated with Phase 4-6 plan)
  src/test/java/.../
    service/SkillServiceTest.java       # 15 tests
    tool/SkillToolServiceTest.java      # 18 tests
```

---

## Configuration

In `app/config.py`:

```python
max_context_chars: int = 20_000  # Budget (÷4 ≈ 5K tokens for compressor)
```

The compressor triggers at 75% of `max_context_chars / 4` tokens. Adjust by changing `max_context_chars` or the threshold in `build_agent_graph()`.

---

## Public Benchmark References

| Benchmark | Relevance |
|-----------|-----------|
| [LOCOMO](https://arxiv.org/abs/2402.17753) (Stanford) | Long-conversation memory over 600+ turns |
| [TAU-bench](https://arxiv.org/abs/2406.12045) (Sierra) | Multi-turn tool-use conversations |
| [SWE-bench](https://arxiv.org/abs/2310.06770) (Princeton) | End-to-end code debugging |
| [∞Bench](https://arxiv.org/abs/2402.13718) | 100K+ token needle-in-haystack |

**Note**: No public benchmark specifically measures *compression quality* (what's retained vs. lost). Our eval harness fills this gap.
