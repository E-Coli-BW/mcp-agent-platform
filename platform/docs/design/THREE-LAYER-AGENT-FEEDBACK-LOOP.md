# Three-Layer Agent Feedback Loop

## Goal

The system should improve in three different dimensions without confusing
retrieval failures with planning or answer failures:

1. **Retrieval quality**: return better evidence for the same question.
2. **Tool and policy quality**: choose the right tool, with the right
   arguments, only when the task requires it.
3. **Answer quality**: produce grounded, complete, policy-compliant answers.

Each layer has its own labels, evaluator, update mechanism, and rollback
boundary. A thumbs-up is useful evidence, but it must not directly mutate
production behavior without an offline quality gate.

## Layer 1: Retrieval

**Signals**

- retrieved chunks and scores
- chunks used by a later `file_read` or citation
- explicit reasons such as `irrelevant_source` or `missing_evidence`
- expected source labels from the QA case set

**Update**

The learned reranker updates feature weights with logistic regression after a
minimum sample count. The candidate model is written as a versioned artifact;
it is promoted only when recall@k, MRR, grounded-answer rate, and refusal
accuracy do not regress on the held-out set.

**Rollback**

Keep the previous weight file and revert if the candidate loses its quality
gate or increases latency beyond the budget.

## Layer 2: Tool and Policy

**Signals**

- requested tool versus observed tool calls
- unnecessary tool calls
- invalid arguments and policy denials
- approval requests, retries, and tool execution errors
- case-level `tool_selection` and efficiency scorer failures

**Update**

Do not train the model online. Aggregate failures by intent and agent, then
produce a candidate policy/prompt/tool-catalog change. Run the same case set
against the old and candidate policy. Promote only when tool-selection recall
improves without exceeding latency, safety, or unnecessary-call budgets.

This layer is best served by structured routing rules, narrower tool schemas,
few-shot examples for recurring intents, and explicit refusal/approval gates.
Fine-tuning is a later option, not the first response to sparse local data.

## Layer 3: Answer Quality

**Signals**

- groundedness and citation checks
- refusal and handoff accuracy
- user ratings and reasons
- answer completeness and policy compliance
- Customer QA cases with expected behavior and source evidence

**Update**

Cluster failures by intent and failure reason. Update the answer contract,
retrieval context format, or agent-specific response policy. Re-run the
regression set and a small human review sample before promotion. A negative
rating should create a triage item first; it should not directly rewrite a
prompt or model.

## Shared Promotion Protocol

Every candidate change records:

- agent id, model id, prompt/policy version
- embedding, index, and reranker versions
- dataset/case-set version
- before/after metrics and latency
- operator, timestamp, and rollback artifact

The dashboard should show the loop as:

`collect -> label -> evaluate -> propose -> gate -> promote/rollback`

The current implementation has Layer 1 collection, candidate reranker
training, explicit rollback, and a held-out-lift-gated promotion endpoint
(``/api/reranker/promote-gated``). Tool-policy and answer-quality signals are
evaluated and persisted; broader corpus-scale promotion automation remains a
separate follow-up.
