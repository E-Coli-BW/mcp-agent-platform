# Customer QA RAG Quality and Feedback Design

## Decision

Improve retrieval quality and measurement before attempting model fine-tuning.
Customer QA failures can come from document parsing, chunking, stale policy,
tenant leakage, weak retrieval, reranking, or unsupported generation. Fine-tuning
the LLM cannot reliably repair those upstream problems.

The near-term quality chain is:

```text
source documents -> structured chunks -> embeddings + BM25 -> ACL/version filter
-> vector/BM25/RRF -> cross-encoder rerank -> evidence threshold -> cited answer
```

## Embedding and Chunking Risks

### Model/index compatibility

The current default `mxbai-embed-large` produces 1024-dimensional vectors;
`nomic-embed-text` produces 768-dimensional vectors. An index must record the
embedding model, model revision, dimension, normalization method, and chunking
version. Startup and query paths must reject incompatible indexes instead of
silently returning poor scores.

### Truncation

The current embedder caps input at 1500 characters because the mxbai model has a
small context window. This can discard the condition or exception that changes a
policy answer. Markdown should be split by heading and paragraph, with a small
overlap and a retained heading path. FAQ question/answer pairs should remain in
one chunk where possible.

### Lexical and semantic mismatch

Order IDs, SKU codes, policy names, abbreviations, and exact deadlines often
need lexical matching. Keep BM25 alongside dense retrieval and merge with RRF;
do not replace it with embeddings alone. Cross-encoder reranking is a second
stage, not a substitute for a missing document.

### Short and ambiguous queries

Queries such as “退款呢？” or “地址能改吗？” lack context. The agent should
rewrite the query using the conversation or ask a clarifying question. It must
not invent missing product, order, region, or policy scope.

### Freshness and provenance

Every chunk should carry `source_url`, `document_version`, `updated_at`, and
optional `effective_from`/`effective_to`. Retrieval should support freshness
filters and the answer should expose the source used. A newer policy must not
lose to an older chunk merely because the wording is more similar.

### Isolation and safety

Public FAQ, tenant policy, and private order facts must use separate collections
or enforceable metadata filters. Retrieval ACLs must be applied before ranking,
not only described in the system prompt. Missing or conflicting evidence should
cause a refusal or human handoff.

## Feedback Loop

Store a retrieval/answer trace for each QA turn:

```json
{
  "query": "who pays return shipping",
  "retrieved_chunk_ids": ["..."],
  "retrieval_scores": [0.0328],
  "reranked_order": ["..."],
  "answer": "...",
  "citations": ["https://..."],
  "embedding_version": "mxbai-embed-large@...",
  "reranker_version": "cross-encoder/ms-marco-MiniLM-L-6-v2",
  "latency_ms": 420,
  "feedback": {"rating": "negative", "reason": "irrelevant_source"}
}
```

Feedback reasons should include:

- `unanswered`
- `irrelevant_source`
- `stale_source`
- `incorrect_answer`
- `should_handoff`
- `too_verbose`
- `missing_citation`

The first use of feedback is evaluation and triage, not training. Accepted
answers become positive retrieval examples; rejected answers are labeled with a
failure cause and reviewed before becoming negative training data.

## Evaluation Gates

M17 should maintain a small, versioned set of questions with expected source
documents and expected behavior. Track:

- recall@k and MRR for retrieval;
- grounded-answer rate and citation correctness;
- refusal accuracy for missing evidence;
- escalation accuracy for order/account requests;
- cross-tenant leakage rate;
- p50/p95 latency and tool count.

Every change to chunking, embedding, reranking, or prompt policy runs this set.

## Fine-Tuning Decision

Do not start with full-parameter local training. A 7B Q4 model is suitable for
inference and small LoRA/QLoRA experiments, but local hardware is unlikely to be
the efficient path for production SFT or preference optimization.

After M16-M18, consider LoRA only if failures remain in language style,
classification, or response format after retrieval and policy fixes. Keep the
base model unchanged for the first experiment, use a few hundred to a few
thousand reviewed examples, and compare against the non-fine-tuned baseline.
Use cloud GPU training if local memory or throughput is insufficient.
