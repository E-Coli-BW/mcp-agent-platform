# Controlled 4-Arm Experiment — Source of Truth

## TL;DR

Prove that agent engineering enhancements (hallucination guard + reflexion + verifier + 0.5B fallback chain) yield a quantifiable improvement in coding-task correctness.

Run **25 tasks × 4 arms × 5 trials = 500 runs**, blinded auto-judge, the key thing to watch
**The Δ of arm A − arm C** (engineering enhancements over the bare LLM).

---

## Why this experiment exists

### Self-evaluation trap (must avoid)

Copilot runs the agent → Copilot scores it = three layers of contamination (the same system picks the task, executes it, and judges it)

### Solution

Controlled 4-arm + control arm (arm C, **all engineering enhancements turned off**) + auto-judge with
blinded inputs. **arm A minus arm C is the true contribution of the engineering enhancements**. Model capability is
cancelled out by the control.

---

## 4 Arms

| Arm | Model | Hallucination Guard | Reflexion | Verifier | Role |
|---|---|---|---|---|---|
| **A** | qwen2.5:7b | ✅ on | ✅ on | ✅ on | full solution |
| **B** | qwen2.5:0.5b | ✅ on | ✅ on | ✅ on | small model + engineering enhancements |
| **C** | qwen2.5:7b | ❌ off | ❌ off | ❌ off | **control group (bare 7B)** |
| **D** | gpt-4o | ❌ off | ❌ off | ❌ off | commercial ceiling ref |

**Key interpretation**:
- `A − C`: **engineering-enhancement gain on 7B** ← core metric
- `B − C`: 0.5B + engineering enhancements vs bare 7B, to see whether the small model can catch up
- `A − D`: how far from GPT-4o (the honest gap)
- `B / D`: tiny model / commercial ceiling, used when discussing cost-per-correctness

---

## Task Selection (25 tasks)

### Source: fix-commits mined from this repo's history

3 filter exclusion criteria:
2. **Exclude env/build**: `ci/`, `dev-scripts/`, `fleet*`, `readme*`, `opensource*` — depend on external state
3. **Exclude unrelated modules**: `llm-infra/`, `fine-tuning/`, `post-training/`, `frontend/`, `review-agent/` — off-topic / cross-domain

### Tiering (by files changed)

- **L1 (8 tasks, 1-2 files)**: single-point bug fix, clear boundaries
- **L2 (10 tasks, 3-4 files)**: medium complexity, spanning 2-3 modules
- **L3 (7 tasks, 5-7 files)**: cross-service feature, system-level change

See `tasks.yaml` for the full list.

### Leak prevention: need_description is LLM back-written, **never fed the diff**

`prepare_tasks.py` feeds only the commit message + file list to the LLM, producing the "original user request".
Constraint: *"describe only the problem; don't say how to change it, which file, or which line"*.

Example:
- commit: `fix(agent): skip hallucination guard after real tool results`
- Output: *"the agent keeps running hallucination checks after tools already returned real results, causing an infinite loop — help me debug it"*

---

## Pass/Fail Judgment (auto)

Three signal tiers, in descending priority:

| Signal | Weight | When available |
|---|---|---|
| **test suite** | high (hard signal) | task ships with tests (~30%) |
| **LLM-judge** (3-input rubric) | medium (primary judge) | always |
| **diff overlap** | low (sanity) | always |

The LLM-judge is fed three things: `(need_description, ground_truth_diff, agent_output_diff)`,
Score 1-5, asking *"does it solve the same problem, allowing a different implementation"*.

**Known risk (P4 in backlog)**: the judge has not been human-calibrated. But for **relative comparison**
(arm A vs arm C) systematic bias cancels out — this is where the 4-arm design saves the day.

**Spot check**: after the run, manually sample 10/25 to see whether judge scores match intuition; if they don't,
Tune the rubric and re-run.

---

## Files

| File | Purpose |
|---|---|
| `README.md` | this — design source of truth |
| `tasks.yaml` | 25 task SHAs + metadata + placeholder need_description |
| `arms.yaml` | 4 arm config (model + feature toggles) |
| `prepare_tasks.py` | LLM back-writes need_description + extracts ground_truth diff |
| `run.py` | main driver (smoke / full) |
| `score.py` | post-eval aggregation + judge invocation (stub for now) |

---

## Phases

### Phase 0 — Scaffold (this commit, smoke validates pipeline)

```bash
cd platform/eval-harness/experiments/controlled_4arm
python prepare_tasks.py --limit 3        # back-write 3 descriptions
python run.py --smoke                    # arm_c × 3 task × 1 trial
# Expected: runs/smoke/arm_c/task_*/ produces 3 directories, each with trajectory.jsonl
```

Success criterion: **runs through without errors**. It doesn't verify the agent actually did it right (smoke isn't a quality check).

### Phase 1 — Hallucination subset (1 evening, verify arm A vs C really differ)

The 10 cases in the P1 backlog + this experiment's arm C/A, running 25×2×5=250 runs, producing hallucination
on/off comparison table.

### Phase 2 — Full experiment (1 weekend)

25 tasks × 4 arms × 5 trials = 500 runs. Produces the final 4×3 matrix (correctness / efficiency
/ failure-mode)。

---

## Out of scope (this scaffold)

- ❌ Don't actually run the 500 runs
- ❌ Don't touch agent server code (guard/reflexion/verifier toggle deferred to Phase 1)
- ❌ Don't write the P1 hallucination 10-case set (separate milestone)
- ❌ Don't write `report.py` (deferred to Phase 1)
- ❌ Don't commit (Rule 1: stage, then wait for review)

---

**Task pool**: 25 tasks selected from the repo's fix-commit history.
