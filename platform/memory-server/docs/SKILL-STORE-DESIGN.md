# Skill Store — Schema Design

> **Status**: Historical design draft. The skill entity, migrations, service,
> REST bridge, history, rollback, and feedback paths are now implemented. This
> document records the original design rationale and future ideas; it is not the
> runtime contract. Use `../README.md` and the current source/tests for supported
> behavior.
> **Author**: auto-generated
> **Date**: 2026-06-03

## Problem

Skills are currently stored as generic `MemoryEntity` rows (`namespace = "skills"`).
This conflates ephemeral notes with reusable procedures. We lose:

1. Version history (overwrite = data loss)
2. Structured indexing (TF-IDF on prose ≠ intent matching)
3. Automatic activation (agent must explicitly search)
4. Feedback loop (no success/failure tracking)

---

## Schema

### Table: `skills`

```sql
CREATE TABLE skills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL,
    key             VARCHAR(255) NOT NULL,       -- e.g. "maven-stale-jar-fix"
    version         INT NOT NULL DEFAULT 1,      -- monotonic per (tenant, key)
    status          VARCHAR(16) NOT NULL DEFAULT 'active',
                    -- active | deprecated | draft

    -- ── Structured body ──────────────────────────────────────
    title           VARCHAR(255) NOT NULL,
    category        VARCHAR(64),                 -- build, debug, workflow, domain, infra
    problem         TEXT NOT NULL,               -- when to use this skill
    preconditions   TEXT,                        -- what must be true before applying
    steps           JSONB NOT NULL,              -- ordered array of step objects
    expected_outcome TEXT,                       -- what success looks like
    pitfalls        JSONB,                       -- array of common mistakes

    -- ── Activation metadata ──────────────────────────────────
    trigger_patterns JSONB,                      -- array of regex/keyword patterns
    trigger_tools   JSONB,                       -- tools that hint this skill is relevant
    trigger_errors  JSONB,                       -- error patterns that activate this skill

    -- ── Relationships ────────────────────────────────────────
    depends_on      JSONB,                       -- array of skill keys this builds upon
    tags            JSONB NOT NULL DEFAULT '[]',

    -- ── Feedback ─────────────────────────────────────────────
    use_count       INT NOT NULL DEFAULT 0,
    success_count   INT NOT NULL DEFAULT 0,
    failure_count   INT NOT NULL DEFAULT 0,

    -- ── Audit ────────────────────────────────────────────────
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      VARCHAR(128),                -- agent or user who created it

    CONSTRAINT uk_tenant_key_version UNIQUE (tenant_id, key, version)
);

CREATE INDEX idx_skills_tenant_status ON skills(tenant_id, status);
CREATE INDEX idx_skills_tenant_category ON skills(tenant_id, category);
CREATE INDEX idx_skills_trigger_tools ON skills USING GIN (trigger_tools);
CREATE INDEX idx_skills_trigger_patterns ON skills USING GIN (trigger_patterns);
CREATE INDEX idx_skills_tags ON skills USING GIN (tags);
```

### `steps` JSONB structure

```json
[
  {
    "order": 1,
    "action": "Run `mvn install -DskipTests` on mcp-common",
    "tool": "code_shell",
    "verification": "exit code 0"
  },
  {
    "order": 2,
    "action": "Run `mvn test` on memory-server",
    "tool": "code_shell",
    "verification": "BUILD SUCCESS in output"
  }
]
```

Each step has:
- `order` — sequence number
- `action` — human-readable instruction (also serves as LLM instruction)
- `tool` — which MCP tool to use (optional, aids auto-activation)
- `verification` — how to confirm this step succeeded (optional)

### `trigger_patterns` JSONB structure

```json
[
  {"type": "regex", "pattern": "ClassNotFoundException.*mcp\\.common"},
  {"type": "keyword", "terms": ["stale jar", "mcp-common", "NoClassDefFound"]},
  {"type": "error_class", "pattern": "java.lang.ClassNotFoundException"}
]
```

### `trigger_tools` JSONB structure

```json
["code_shell", "file_edit"]
```

Meaning: when these tools are bound to the current agent session AND context matches, surface this skill.

---

## Auto-Activation Design

### The Problem with Manual Activation

Current flow:
```
User query → LLM → (maybe) calls memory_search("skill...") → finds skill → follows it
```

This fails when:
- LLM doesn't know a skill exists
- LLM doesn't think to search for it
- The relevant trigger is an ERROR, not a user intent

### Three-Layer Activation

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: PASSIVE (catalog in system prompt — existing)      │
│  How: skills_index.py injects compact key+summary list       │
│  When: Every request                                         │
│  Cost: ~1K tokens (bounded)                                  │
│  Limitation: LLM must notice and call skill_get              │
└─────────────────────────────────────────────────────────────┘
                            ↓ (if Layer 1 misses)
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: REACTIVE (error-triggered)                         │
│  How: After a tool call FAILS, match error output against    │
│       trigger_errors patterns. If match → inject skill       │
│       body into next LLM turn as a "hint" message.           │
│  When: On tool error, before next LLM call                   │
│  Cost: One DB query per error (cached trigger index)         │
│  Example: ClassNotFoundException → "maven-stale-jar-fix"     │
└─────────────────────────────────────────────────────────────┘
                            ↓ (proactive, not error-driven)
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: PROACTIVE (context-triggered)                      │
│  How: Before the first LLM call, match user query against    │
│       trigger_patterns (regex/keyword). If match → prepend   │
│       skill reference to system prompt or inject as a        │
│       "recommended skill" block.                             │
│  When: At intent-routing time (before call_llm node)         │
│  Cost: In-memory trie/regex scan on cached trigger index     │
│  Example: "deploy to staging" → "skill-k8s-deploy-staging"  │
└─────────────────────────────────────────────────────────────┘
```

### Layer 2 Detail: Error-Triggered Activation

```python
# In the `tools` node, after a tool call returns an error:

async def maybe_activate_skill_on_error(
    tenant_id: str,
    tool_name: str,
    error_output: str,
    skill_cache: SkillTriggerCache,
) -> Optional[str]:
    """
    Match error output against trigger_errors of all active skills.
    Returns skill body if a match is found, else None.
    """
    candidates = skill_cache.get_error_triggers(tenant_id)
    for skill_key, patterns in candidates.items():
        for pattern in patterns:
            if re.search(pattern["pattern"], error_output):
                # Fetch full skill body
                skill = await skill_store.get_active(tenant_id, skill_key)
                return format_skill_as_hint(skill)
    return None
```

The hint is injected as a system message in the next turn:
```
[SKILL ACTIVATED: maven-stale-jar-fix]
You hit a known error. Follow these steps:
1. Run `mvn install -DskipTests` on mcp-common ...
```

### Layer 3 Detail: Context-Triggered Activation

```python
# In the intent_router node, before routing to call_llm:

async def proactive_skill_match(
    tenant_id: str,
    user_message: str,
    bound_tools: set[str],
    skill_cache: SkillTriggerCache,
) -> list[SkillHint]:
    """
    Scan user message against trigger_patterns.
    Also cross-reference bound_tools with trigger_tools.
    Returns top-N matching skills (max 2 to avoid prompt bloat).
    """
    hits = []
    for skill_key, meta in skill_cache.get_all_triggers(tenant_id).items():
        score = 0.0

        # Keyword match
        for trigger in meta.get("trigger_patterns", []):
            if trigger["type"] == "keyword":
                if any(term.lower() in user_message.lower() for term in trigger["terms"]):
                    score += 1.0
            elif trigger["type"] == "regex":
                if re.search(trigger["pattern"], user_message, re.IGNORECASE):
                    score += 1.5

        # Tool overlap bonus
        skill_tools = set(meta.get("trigger_tools", []))
        if skill_tools & bound_tools:
            score += 0.5

        if score > 0:
            hits.append((score, skill_key))

    # Return top 2 by score
    hits.sort(reverse=True)
    return [SkillHint(key=k, score=s) for s, k in hits[:2]]
```

Output is injected as a compact block in the system prompt:
```
[SUGGESTED SKILLS — call skill_get(key) if relevant]
- maven-stale-jar-fix (score: 2.0) — Fixes ClassNotFoundException after mcp-common changes
- spring-boot-debug-port (score: 1.0) — Attaching debugger to running Spring Boot
```

### Trigger Cache Architecture

```
                    ┌──────────────────┐
                    │  PostgreSQL      │
                    │  skills table    │
                    └────────┬─────────┘
                             │ (on startup + every TTL)
                    ┌────────▼─────────┐
                    │  SkillTriggerCache│  (in-process, per-tenant)
                    │  - error_triggers │  Map<tenant, Map<key, patterns>>
                    │  - context_triggers│
                    │  - tool_triggers  │
                    │  TTL: 60s        │
                    └──────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
         Layer 2         Layer 3        Layer 1
       (on error)     (on request)    (catalog)
```

Only `status = 'active'` skills are loaded into the cache.
Cache invalidation: on skill write (pub/sub from memory-server) or TTL expiry.

---

## Versioning Strategy

```
skill_set(key="maven-fix", ...) on existing key:
  1. Mark current version as deprecated (status = 'deprecated')
  2. Insert new row with version = prev + 1, status = 'active'
  3. Old versions remain queryable: skill_history(key) → [v1, v2, v3]
  4. Rollback: skill_rollback(key, version=2) → marks v3 deprecated, v2 active
```

Only ONE version per (tenant, key) has `status = 'active'` at a time.

---

## Migration Path from Current System

1. **Phase 1** (backward-compatible): New `skills` table lives alongside `memories`.
   `skills_index.py` reads from BOTH — existing `namespace=skills` memories + new table.
   New `skill_set` tool writes to new table. Old `memory_set(namespace=skills)` still works.

2. **Phase 2**: Migrate existing skill memories → new table via one-time script.
   Parse free-text content into `problem` + `steps` best-effort.

3. **Phase 3**: Remove `namespace=skills` from memories. `skill_set`/`skill_get` are the only API.

---

## API (MCP Tool Interface)

```
skill_set(key, title, problem, steps, category?, trigger_patterns?, trigger_tools?, tags?)
skill_get(key, version?) → full body (default: active version)
skill_list(category?, tag?) → compact index
skill_history(key) → all versions with metadata
skill_rollback(key, version) → reactivate old version
skill_feedback(key, success: bool) → increment success/failure count
```

---

## Context Window Budget vs. Retrieval Accuracy

### The Core Tension

```
MORE in prompt  → LLM always knows what's available → bloats context, drowns signal
LESS in prompt  → saves tokens, keeps focus         → LLM can't retrieve what it doesn't know exists
```

### The Spectrum

| Strategy | Tokens/skill | LLM can find it? | Risk |
|----------|:---:|:---:|------|
| Full body in system prompt | ~200-500 | ✅ Always | 50 skills × 300 tokens = 15K tokens. Unusable. |
| Summary + key (current Layer 1) | ~20-30 | ⚠️ If summary is good | LLM sees "maven-stale-jar-fix — Fixes stale JAR after common changes" but might not connect it to `ClassNotFoundException` |
| Key only | ~5-10 | ❌ Almost never | LLM sees `maven-stale-jar-fix` and has no idea when to use it |
| Nothing (pure retrieval) | 0 | ❌ Never spontaneously | Only works if something else triggers retrieval |

### Our Design Decision: Tiered Disclosure

```
┌─────────────────────────────────────────────────────────────────┐
│  System prompt: SUMMARY CATALOG (~25 tokens/skill, bounded)      │
│  Contains: key + 1-line problem description + top tags           │
│  Example: "maven-stale-jar-fix — ClassNotFoundException after    │
│            editing mcp-common [build, maven]"                    │
│  Budget: max 40 skills × 25 tokens = 1K tokens                  │
└─────────────────────────────────────────────────────────────────┘
                           ↓
        LLM decides to call skill_get(key) — OR —
        Layer 2/3 auto-injects the full body
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│  Injected on-demand: FULL SKILL BODY (~150-400 tokens)           │
│  Contains: problem, preconditions, all steps, pitfalls           │
│  Only loaded when: LLM calls skill_get, OR trigger fires         │
└─────────────────────────────────────────────────────────────────┘
```

### Will the LLM Correctly Identify When to Retrieve?

**Honest answer: Not always.** Here's the failure analysis:

| Scenario | Does summary-only work? | Why / Why not |
|----------|:---:|------|
| User says "build memory-server" | ✅ | Summary mentions "maven", "build" — LLM connects |
| Tool returns `ClassNotFoundException` | ⚠️ | Summary says "ClassNotFoundException" — but LLM might not re-read catalog after an error |
| User says "deploy the thing" | ❌ | If summary says "k8s deploy" but user says "the thing", no match |
| Novel error the LLM hasn't seen | ❌ | LLM doesn't know to look for a skill |

**This is why Layers 2 and 3 exist** — they compensate for the LLM's inability to always self-retrieve:

- **Layer 1** (catalog): Works when the LLM is smart enough to notice
- **Layer 2** (error-trigger): Works when the LLM fails to notice but the error is recognizable
- **Layer 3** (context-trigger): Works proactively before the LLM even runs

### Summary Quality is Critical

The entire Layer 1 strategy collapses if summaries are bad. Rules for catalog entries:

1. **Include the error class/keyword** — not just the fix, but the *symptom*
   - ❌ `"Rebuilds common modules"` — too vague
   - ✅ `"Fixes ClassNotFoundException after mcp-common edits"` — contains the trigger word
2. **Max 80 chars** — forces prioritization
3. **Lead with the WHEN, not the WHAT** — LLM needs to match situation, not understand solution
   - ❌ `"Runs mvn install on mcp-common then mvn test on memory-server"`
   - ✅ `"When memory-server fails to start after editing mcp-common"`
4. **Include 1-2 keywords the LLM would see in error output** — bridges the gap between "error on screen" and "skill in catalog"

### Fallback: Embedding Similarity (Layer 4, future)

When regex and keywords miss (user paraphrases, novel phrasing), vector similarity on the `problem` field catches it:

```
User: "the server won't boot after I changed the shared library"
   → No regex match
   → No keyword match ("shared library" ≠ "mcp-common")
   → Embedding similarity: problem="memory-server fails after mcp-common edits" → 0.82 cosine
   → Surface skill
```

Cost: One embedding call per request (cacheable). Only fires if Layers 1-3 miss.

### Token Budget Guardrails

| Guardrail | Value | Rationale |
|-----------|-------|-----------|
| Max skills in catalog | 40 | Beyond this, catalog itself is noise |
| Max tokens per catalog entry | 30 | 40 × 30 = 1,200 tokens — acceptable |
| Max skills auto-injected per turn | 2 | More than 2 full bodies = context pollution |
| Max tokens per injected skill body | 400 | Hard-truncate steps beyond this |
| Skill body TTL in conversation | 1 turn | Don't carry injected skills across turns — re-trigger if still relevant |

### The "Will It Work?" Honest Assessment

```
                                    ┌─ Layer 1 catalog catches: ~60% of cases
                                    │  (LLM is smart enough to self-retrieve)
                                    │
User needs a skill ────────────────┼─ Layer 2 error-trigger catches: +25%
                                    │  (error patterns are precise signals)
                                    │
                                    ├─ Layer 3 proactive catches: +10%
                                    │  (keyword/regex on user input)
                                    │
                                    └─ Missed entirely: ~5%
                                       (novel phrasing, no error yet, LLM oblivious)
                                       → Layer 4 embedding would reduce to ~2%
```

The 5% miss rate is acceptable because:
- Those cases the agent would have failed WITHOUT skills too
- The user can always manually say "use skill X" or "search for skills about Y"
- Over time, missed cases generate errors → Layer 2 catches on retry

---

## What This Enables

| Before | After |
|--------|-------|
| LLM must remember to search for skills | Skills surface automatically on error or context match |
| Overwrite = lost history | Full version history, rollback |
| Free-text blob | Structured steps the LLM can execute as checklist |
| No feedback | success/failure counts → prune bad skills over time |
| Same index for all contexts | Trigger-based relevance → right skill at right time |
| Full bodies bloat context | Summary catalog (1K tokens) + on-demand full body |

---

## Next Steps: Context Compression & Observability

### Phase 4: Context Window Compression (post Skill Store MVP)

Long multi-turn sessions (especially debug) scatter critical info across 30+ turns.
When context window hits ~75% capacity, the agent needs to compress without losing critical facts.

**Implementation plan:**

1. **Add `compressor` node to `graph_v2.py`** — fires when token budget exceeded
   - Classifies each message block: PIN / KEEP / SUMMARIZE / DROP
   - Writes compressed history to `state.summary`, trims `state.messages`
   - Skill execution traces compressed to references (recoverable via `skill_get`)

2. **Add `InvestigationState` to `AgentState`** — structured extraction
   - `goal`, `confirmed_facts`, `current_hypothesis`, `eliminated`, `artifacts`, `next_steps`
   - Updated after every tool call — makes compression decisions trivial

3. **Skill-aware compression** — when a skill was used during session, replace full body
   with `[Applied skill:key steps 1-3. Step 1 ✅, Step 2 ❌]` (full body always recoverable)

### Phase 5: Observability Stack

Three-layer observability for agent behavior tracking:

1. **OpenTelemetry traces** (`app/observability/tracing.py`)
   - Every graph node = a span (token counts, tool calls, latencies)
   - Visualize in Jaeger/Tempo

2. **Decision logs** (`app/observability/decision_log.py`)
   - Structured events: compression decisions, skill activations, fact extraction
   - Emit to Kafka topic `agent.decisions.*` for post-hoc analysis
   - Records: what was dropped, what was retained, why

3. **Session replay** (`app/observability/session_replay.py`)
   - Before/after snapshots at key state mutations
   - Enables: "replay from pre-compression snapshot to see what was lost"

### Phase 6: Eval Harness for Context Retention

**New eval scenarios** in `platform/eval-harness/scenarios/context_compression/`:

| Scenario | What it Tests |
|----------|--------------|
| `debug_needle` | 30-turn debug, critical fact at turn 5, ask about it at turn 30 |
| `skill_recovery` | Agent uses skill, session compresses, must re-apply variant |
| `hypothesis_chain` | 5 eliminated hypotheses — compression must retain WHY each failed |
| `multi_file_debug` | Fix spans 4 files, compress mid-task, agent remembers all files |

**Metrics:**
- `fact_retention` — facts_post / facts_pre (alert if < 0.85)
- `compression_ratio` — tokens_after / tokens_before (alert if < 0.3)
- `redundant_rework` — repeated steps post-compression (should be 0)
- `task_completion` — can agent still finish the task after compression?

**Public benchmark baselines:** LOCOMO (long-conversation memory), TAU-bench (multi-turn tool-use), SWE-bench (end-to-end code tasks). Note: no public benchmark tests compression quality directly — this is a differentiator.

### Priority Order

```
✅ Phase 1-3: Skill Store MVP (DONE — entity, service, tools, tests, activation)
⬜ Phase 4: Context compression (compressor node + InvestigationState)
⬜ Phase 5: Observability (OTel + decision logs + replay)
⬜ Phase 6: Eval scenarios for retention benchmarking
```
