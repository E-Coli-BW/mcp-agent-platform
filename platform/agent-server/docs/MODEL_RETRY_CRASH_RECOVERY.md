# Model Retry Crash Recovery

Model retry recovery combines two durable stores when
`AGENT_RUNTIME_STORE_BACKEND=sqlite`:

- LangGraph's companion SQLite checkpointer owns conversation state and the
  pending graph node.
- `SQLiteCheckpointStore` owns retry attempts, backoff deadline, failure facts,
  and a worker lease.

## Lifecycle

```text
model_retry_executing
  -> model_retry_waiting
  -> model_retry_executing
  -> completed | failed
```

Before a provider request, the process owns an `executing` lease. A retryable
failure commits `model_retry_waiting` and `resume_at` before sleeping, then
keeps an owner lease through a five-second recovery grace after `resume_at`.
The live process must renew that same lease before its next provider request.
A successful response completes the checkpoint.

After a process restart, only a due `model_retry_waiting` checkpoint is safe to
resume automatically. Recovery claims its lease, rebuilds the configured Agent,
and calls the graph with `None` and the original `thread_id`; LangGraph resumes
the pending `call_llm` node from its durable state.

An expired `model_retry_executing` lease means the process died while a provider
request might have been in flight. Its outcome is unknown, so recovery marks the
checkpoint `reconciliation_required` instead of replaying automatically.

Recovery is explicit through `POST /v1/runs/{run_id}/resume`; startup never
initiates external model traffic automatically.