# RAG Quality Optimization Execution

## Goal

Improve Customer QA retrieval in an order that preserves attribution:

1. Chunk, metadata, source hierarchy, and hard-negative labels.
2. Intent-aware retrieval and filtering.
3. Cross-encoder or stronger reranker comparison.
4. Embedding replacement or reranker fine-tuning decision based on measured
   results.

Every stage needs a versioned benchmark, before/after metrics, latency, and a
rollback-compatible artifact. No model change is accepted without showing
which earlier layer it improves.

The first implementation now enriches every parser output with stable
`document_id`, `parent_id`, `chunk_id`, `source_uri`, `section_path`, `tenant_id`,
`version`, and `acl` metadata. Section chunks now use a stable
`document_parent_id -> parent_id(section) -> chunk_id` hierarchy. This is a
canonical baseline; parser-specific hierarchy and human-reviewed ACLs still
override the safe defaults during ingestion.

## Acceptance Metrics

- Recall@K, MRR, nDCG, target@1
- hard-negative@1 and hard-negative@K
- citation precision and coverage
- groundedness and abstention accuracy
- p50/p95 retrieval and reranking latency
- index size and embedding cost

The comparison harness records an unavailable cross-encoder as a skipped arm
rather than silently falling back to the baseline. The locally cached model is
now available, and the same adversarial candidate set is evaluated for the
baseline, heuristic, and cross-encoder arms.

The dependency is now installed and the model is locally cached. With
`HF_HUB_OFFLINE=1`, the expanded 18-query benchmark with adjacent-intent and
condition/exception hard negatives produced:

```text
none:          Recall@3 0.667, MRR 0.435, 0.0ms
heuristic:     Recall@3 1.000, MRR 0.750, 0.0ms
cross_encoder: Recall@3 1.000, MRR 0.889, 345.2ms
```

The benchmark is intentionally candidate-adversarial: the fixed baseline is
not perfect and every result records the target rank per query. It shows the
cross-encoder improves MRR over the heuristic by 0.139 on this harder sample,
but costs about 345ms per query on the local CPU path. This is evidence that
the earlier tie was a benchmark-size/design limitation; it is still not enough
to justify fine-tuning, and online use should be gated by latency budget.

### 300-case public-FAQ-seeded candidate run

The generated 300-case set was run against the current
`qa-eval-tenant/customer-faq` index as a candidate benchmark, not human-
verified golden data:

```text
Before source completion:
Recall@5:           0.500
MRR@5:              0.495

After adding the four missing source families and rebuilding the index:
Recall@5:           0.900
MRR@5:              0.715
target@1:           0.587
hard-negative leak: 0.950
```

The intent breakdown is more actionable than the aggregate. Delivery exception,
package tracking, profile address, refund request, and returns policy are
covered by the current index. Refund status, payment dispute, shipping address,
and order cancellation have little or no matching source coverage, so their
zero recall was primarily a corpus/source-hierarchy gap rather than evidence
that reranking failed. After ingestion, recall recovered, while the high
hard-negative leakage shows that adjacent-intent ranking and source granularity
remain unresolved.

The run is persisted to `~/.mcp-local/customer-qa-retrieval.json` with
`dataset_quality=synthetic_candidate` and per-intent metrics. It should not be
used as a model-quality gate until missing source families are ingested and the
generated gold assignments are reviewed.

## Serving Follow-up

After RAG quality is stable, create a separate serving track. On local
hardware, the realistic scope is a benchmark/demo rather than production
capacity claims:

- KV cache: compare repeated-prefix and fresh-prefix decode memory/latency;
- paged attention: document and, if the local runtime exposes it, measure
  allocator behavior rather than pretending to reproduce GPU kernel gains;
- prompt/prefix caching: measure cache hit/miss TTFT and correctness of cache
  invalidation when prompt or tool schema versions change;
- first-token issue: record queue time, prefill time, decode time, TTFT, and
  inter-token latency separately;
- serving comparison: Ollama/MLX/vLLM only where the machine and runtime
  actually support the feature.

The serving track must clearly separate a functional demo from a production
benchmark. A local Mac without CUDA cannot establish GPU paged-attention
throughput.

### Serving acceptance evidence

The MLX serving module has an executable prefix-cache control-plane test for
hit/miss accounting, bounded eviction, and prompt invalidation. Its payload is
opaque by design: the current `mlx_lm.generate` path does not accept externally
supplied KV tensors, so a cache hit must not be reported as a measured prefill
reduction until an incremental-KV integration is added.

The local capability probe reports MLX available, but CUDA and vLLM unavailable.
Therefore prefix-cache bookkeeping can be demonstrated locally, while paged
attention is documented but not runnable on this host. KV cache, prefix cache,
and paged attention are related but not interchangeable; only engine-level KV
reuse changes prefill latency.
