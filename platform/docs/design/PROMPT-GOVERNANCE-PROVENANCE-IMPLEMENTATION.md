# Prompt Governance + Model-Call Provenance Implementation Blueprint

This document is an execution-level design for two controls:

1. **Prompt governance** (versioning, canary rollout, rollback)
2. **Model-call provenance** (reproducibility-grade call logging)

It is designed to be incremental and reversible for both runtimes:
- Python: `platform/agent-server/`
- Java: `platform/agent-server-java/`

---

## 1) Objectives

## Functional

1. Prompt text is no longer hardcoded-only; runtime resolves prompt artifacts by policy.
2. Prompt versions can be canaried and rolled back without runtime redeploy.
3. Every model call records enough metadata to reproduce behavior.

## Non-functional

1. P95 overhead from governance/provenance path <= 5 ms per call.
2. No single point of failure in prompt resolution (cache + fallback).
3. Provenance event delivery is best-effort with local fallback buffering.

---

## 2) Verified current insertion points

## Prompt sources

- Python static prompt source: `platform/agent-server/app/agent/prompts.py`
- Java static prompt source: `platform/agent-server-java/src/main/java/com/example/agent/agent/PromptService.java`

## Model call sites

- Python v1 path: `platform/agent-server/app/agent/graph.py` (`_create_chat_model`, state modifier)
- Python v2 path: `platform/agent-server/app/agent/graph_v2.py` (`make_call_llm_node`)
- Java main loop: `platform/agent-server-java/src/main/java/com/example/agent/agent/AgentLoopService.java`
- Java subagent path: `platform/agent-server-java/src/main/java/com/example/agent/agent/SubagentSpawner.java`

## Request envelope / tenant/session context

- Python API: `platform/agent-server/app/api/chat.py`
- Java API: `platform/agent-server-java/src/main/java/com/example/agent/api/ChatController.java`

---

## 3) Architecture

## 3.1 Components

1. **Prompt Registry** (new service or module)
   - Stores immutable prompt artifacts + rollout policy pointers.
2. **Prompt Resolver SDK** (Python + Java adapters)
   - Runtime contract to resolve effective prompt version.
3. **Provenance Emitter SDK** (Python + Java adapters)
   - Emits model-call events to Kafka/topic + local fallback.
4. **Provenance Store** (OLAP/warehouse)
   - Queryable by `run_id`/`request_id`/`session_id`.

## 3.2 Trust boundaries

1. Runtime must not trust caller-provided prompt version unless policy allows.
2. Tenant routing/overrides are server-side only.
3. Sensitive content is redacted before persistence.

---

## 4) Prompt governance design

## 4.1 Prompt artifact model

Use immutable records:

- `prompt_id`: logical name (e.g. `coding-agent.system`)
- `version`: semantic or monotonic
- `template`: prompt body
- `content_hash`: SHA-256(template)
- `status`: `draft|candidate|canary|active|deprecated|archived`
- `input_schema_json`: variables allowed in rendering
- `owner`, `risk_tier`, `changelog`
- `created_at`, `created_by`

## 4.2 Rollout model

Separate artifact from assignment:

- Global assignment: default active version per `prompt_id`
- Tenant assignment: override by `tenant_id`
- Scenario assignment: optional (`domain`, `agent_name`, `task_type`)
- Canary policy:
  - `mode`: `tenant_allowlist|percentage|scenario_allowlist`
  - `canary_version`
  - `stable_version`
  - `start_at`, `end_at`

## 4.3 Resolver contract

```text
resolve(tenant_id, prompt_id, context, requested_version?)
-> {
  prompt_id,
  version,
  rendered_prompt,
  content_hash,
  assignment_source,
  rollout_policy_id
}
```

Resolution order:

1. explicit request version (if policy-permitted)
2. tenant assignment
3. scenario assignment
4. global default

## 4.4 Rollout state machine

```text
draft -> candidate -> canary -> active -> deprecated -> archived
```

Promotion gates (all required):

1. quality no-regression (golden eval suites)
2. latency delta within threshold
3. token/cost delta within threshold
4. error-rate delta within threshold

## 4.5 Rollback policy

Rollback is pointer switch, not content mutation.

- Keep previous stable pointer for every assignment.
- Switch assignment pointer to prior version on rollback.
- Emit rollback audit event with reason and incident link.

Optional auto-rollback triggers:

1. quality drop > configured threshold
2. P95 latency increase > configured threshold
3. model/tool error rate increase > configured threshold

---

## 5) Model-call provenance design

## 5.1 Event schema (minimum)

See: `platform/docs/design/schemas/model-call-provenance.schema.json`

Required fields:

- identity: `event_id`, `run_id`, `request_id`, `trace_id`
- tenancy/session: `tenant_id`, `session_id`, optional `subagent_session_id`
- runtime: `runtime` (`python-v2`, `java-loop`), `call_site`
- model path: `provider`, `model`, `temperature`, `max_tokens`, `route_decision`
- prompt path: `prompt_id`, `prompt_version`, `prompt_hash`
- behavior flags: full flag snapshot used for this call
- budgets: `prompt_tokens`, `completion_tokens`, `duration_ms`
- fallback/retry: counts and terminal status
- timestamp

## 5.2 Event transport

Primary path:

1. emit to Kafka topic `agent_model_calls`
2. consumer writes to analytics store

Fallback path:

1. local append-only file buffer (bounded)
2. background retry to Kafka

No request should fail because provenance sink is down.

## 5.3 Reproducibility query contract

Given `run_id` or `request_id`, operator can reconstruct:

1. exact prompt version/hash used
2. exact model/provider/settings used
3. exact feature flags active
4. exact retries/fallback path
5. token and latency profile

Target SLO: reconstruction in <= 5 minutes via one query bundle.

---

## 6) Runtime integration details

## 6.1 Python runtime integration

### Files to modify

1. `platform/agent-server/app/api/chat.py`
   - generate `request_id`, `run_id`
   - attach to request context and pass through graph config
2. `platform/agent-server/app/agent/prompts.py`
   - replace static getter path with `PromptResolver` adapter
3. `platform/agent-server/app/agent/graph.py`
4. `platform/agent-server/app/agent/graph_v2.py`
   - emit provenance before/after each model call

### Adapter interfaces

```python
class PromptResolver:
    async def resolve(self, tenant_id: str, prompt_id: str, context: dict, requested_version: str | None = None) -> PromptResolution: ...

class ProvenanceEmitter:
    async def emit_model_call(self, event: dict) -> None: ...
```

## 6.2 Java runtime integration

### Files to modify

1. `platform/agent-server-java/src/main/java/com/example/agent/api/ChatController.java`
   - create request/run ids
2. `platform/agent-server-java/src/main/java/com/example/agent/agent/PromptService.java`
   - resolve prompts via adapter, return metadata tuple
3. `platform/agent-server-java/src/main/java/com/example/agent/agent/AgentLoopService.java`
4. `platform/agent-server-java/src/main/java/com/example/agent/agent/SubagentSpawner.java`
   - emit provenance before/after `chatModel.call(prompt)`

### Adapter interfaces

```java
public interface PromptResolver {
    PromptResolution resolve(String tenantId, String promptId, Map<String, Object> context, String requestedVersion);
}

public interface ProvenanceEmitter {
    void emitModelCall(ModelCallProvenance event);
}
```

---

## 7) Data schemas

## 7.1 Prompt record schema

See: `platform/docs/design/schemas/prompt-record.schema.json`

## 7.2 Model-call provenance schema

See: `platform/docs/design/schemas/model-call-provenance.schema.json`

---

## 8) Rollout plan (8-week)

## Week 1-2: Provenance foundation

1. Add IDs and context propagation in Python + Java API layers.
2. Add provenance event model and emitter abstraction.
3. Emit events at model call sites with static prompt metadata first.

Exit criteria:

- >= 95% of model calls produce provenance events.

## Week 3-4: Prompt registry read path

1. Build prompt registry schema + admin APIs.
2. Add runtime resolver with cache and fallback to static prompts.
3. Record prompt version/hash from resolver in provenance.

Exit criteria:

- Runtime can resolve prompt from registry and fallback safely.

## Week 5-6: Canary and rollback

1. Add assignment + canary policy tables.
2. Implement percentage and tenant-allowlist canary routing.
3. Implement one-command rollback pointer switch.

Exit criteria:

- Rollback requires no redeploy and completes in < 1 minute.

## Week 7-8: Promotion gates and automation

1. Connect eval-harness + perf checks to prompt promotion workflow.
2. Add auto-rollback guards for severe regressions.
3. Add dashboards for prompt/version route metrics.

Exit criteria:

- Prompt promotion blocked automatically on gate failure.

---

## 9) Test strategy

## 9.1 Unit

1. resolver precedence logic
2. canary assignment hashing stability
3. schema validation for provenance events
4. redaction logic

## 9.2 Integration

1. runtime -> resolver -> registry path
2. runtime -> emitter -> Kafka path
3. fallback when registry unavailable
4. fallback when Kafka unavailable

## 9.3 E2E

1. canary rollout with quality gate
2. rollback and post-rollback reproducibility
3. replay-by-request-id audit drill

---

## 10) Security and compliance controls

1. Never log raw secrets.
2. Redact/ tokenize PII fields before sink.
3. Tenant isolation in all prompt assignment and event queries.
4. Immutable audit log for prompt state transitions.

---

## 11) Operational runbook snippets

## Promote prompt

1. set candidate version
2. run eval and perf suites
3. enable canary policy
4. observe 30-60 min
5. promote pointer to active

## Rollback prompt

1. switch pointer to previous stable version
2. emit rollback audit event
3. verify recovery in quality/latency dashboards
4. create incident note and root-cause entry

---

## 12) Definition of done

Complete when:

1. Prompt version can be changed and rolled back without redeploy.
2. Every model call can be reproduced from provenance metadata.
3. Promotion is gate-controlled by quality + perf + cost thresholds.
4. Failure of governance/provenance services degrades gracefully.
