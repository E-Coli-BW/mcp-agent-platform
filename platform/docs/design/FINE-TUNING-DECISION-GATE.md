# Fine-Tuning Decision Gate

The local 7B deployment should not be fine-tuned merely because a feedback
loop exists. Raw ratings are noisy, retrieval failures are often better fixed
with corpus/chunking/reranking changes, and a local update can make rollback
harder.

The gate therefore requires:

1. A minimum number of immutable feedback samples.
2. A versioned held-out set that is never used for training.
3. A measured quality gap after retrieval, tool-contract, and policy fixes.
4. A candidate model evaluated beside the current model before promotion.

Run the deterministic pre-check with:

```bash
python platform/eval-harness/ci/fine_tuning_gate.py summary.json
python platform/eval-harness/ci/hardware_probe.py
```

The default outcome for the current local project is expected to be
`defer_insufficient_data` or `defer_no_held_out_set`. If the gate eventually
allows training, use LoRA/QLoRA as an offline candidate experiment, keep the
base model immutable, and promote only after the Customer QA safety suite and
coding-agent regression suite both pass. Cloud training is an optional
fallback when local VRAM/Metal memory or iteration time is insufficient.
