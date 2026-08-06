# Customer QA Pack

The first domain migration uses the existing runtime with a narrow, read-only
tool surface. It is intentionally not a customer-service fork. The current
implementation and its gaps are summarized in
[Current Architecture](design/CURRENT-ARCHITECTURE.md).

## Scope

The initial pack is suitable for FAQ and knowledge-base questions. It can:

- retrieve relevant knowledge with `rag_search`;
- use `memory_search` for previously stored support context;
- answer only from evidence;
- ask for clarification or recommend human support when evidence is missing.

It cannot yet:

- look up private order/account state;
- issue refunds or modify orders;
- create tickets automatically;
- perform identity or entitlement checks.

## Run

Use the same agent server and request the model id `customer-qa-agent`:

```bash
curl -sS -X POST http://localhost:8580/v1/chat/completions \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"model":"customer-qa-agent","stream":true,"messages":[{"role":"user","content":"What is the return policy?"}]}'
```

The runtime, event log, policy metadata, failure semantics, and evaluation
harness are shared with `coding-agent`.

## Acceptance bar

- Knowledge questions call `rag_search` before answering.
- Unsupported order/account questions do not invent an answer.
- No filesystem, shell, write, git, or ticket mutation tool is available.
- The final answer is grounded in retrieved evidence or explicitly says that
  evidence is missing.

## First Live Check

The first 7B live check passed the safety cases: unsupported order status and
refund/address changes were handed off instead of invented or executed. A
public demo corpus now exists under `platform/agent-server/knowledge/customer-qa/`
and real Ollama embeddings have been generated locally. Production-quality
grounding still requires versioned source metadata, evidence thresholds, and
feedback-driven regression tests.
