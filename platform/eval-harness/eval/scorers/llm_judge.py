"""Scorer: llm_judge — use a stronger model to grade open-ended quality.

When to reach for this scorer
-----------------------------
ONLY when deterministic scorers genuinely can't express the criterion.
Examples of things only a judge can grade reliably:

  - "Did the agent reason about the user's intent before acting?"
  - "Is the explanation clear and well-structured?"
  - "Is the tone appropriate for a customer-facing reply?"

Examples where a judge is the WRONG tool (use substring/regex instead):

  - "Does the answer contain the string 'blue'?"     → answer_grounded
  - "Did the agent call memory_search?"               → tool_selection
  - "Did the response stay under 30s?"                → budgets

Why? Judges cost 100–1000× more than deterministic scorers, are flakier
on model swaps, and harder to debug ("why did the judge say 4/5 today
and 5/5 yesterday for the same answer?"). Deterministic scorers also
fail loudly with a clear "this exact thing was missing"; judges fail
fuzzy ("the answer was missing some clarity"). Use the cheap, sharp
tool first.

Design non-negotiables (these are the ones that bite in production)
-------------------------------------------------------------------

1. **Different model family** than the SUT (System Under Test).
   Qwen-judging-Qwen has been measured to inflate scores ~15% (the
   judge "agrees with itself"). If the agent runs on Qwen, set the
   judge to GPT-4o or Claude. If you can't, at least use a *bigger*
   variant from the same family.

2. **Stronger** model than the SUT. A weak judge on a strong SUT is
   noise. The judge needs to be at least as capable as the SUT for
   the criterion being graded.

3. **Structured rubric** with a JSON-schema output. "Score 1–10" is
   garbage — judges hug 7. Use a 1–5 Likert scale with explicit
   anchors per level, AND ask for per-criterion sub-scores.

4. **Self-consistency**: sample N=3 (or 5) at temp=0.3, take the
   median of overall_score. A single judge call has ~10% disagreement
   with itself on borderline cases.

5. **Calibrate quarterly** against ~50 human-labeled examples. Judge
   drift is real — model providers update silently.

Configuration (env vars, all OPTIONAL)
--------------------------------------

  EVAL_JUDGE_MODEL       — e.g. "openai/gpt-4o", "anthropic/claude-3.5".
                            If unset → scorer is skipped entirely.
  EVAL_JUDGE_BASE_URL    — OpenAI-compatible endpoint.
                            Default: env OPENAI_BASE_URL or
                            "http://localhost:11434/v1" (Ollama).
  EVAL_JUDGE_API_KEY     — API key for the judge endpoint.
                            Default: env OPENAI_API_KEY or "ollama".
  EVAL_JUDGE_SAMPLES     — Self-consistency N. Default 3.
                            Override per-case via `expected.judge.samples`.
  EVAL_JUDGE_TEMPERATURE — Default 0.3.

YAML usage
----------

    expected:
      judge:
        rubric: |
          The assistant must explain WHY before showing any code,
          and the explanation must reference the user's original goal.
        criteria: [reasoning, completeness, tone]
        min_score: 4                       # average across criteria, out of 5
        samples: 3                         # override env

If `judge` is unset on a case, this scorer returns None (not applicable).
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from typing import Any

from eval.case import Case, Score, Trajectory
from eval.scorers import register

log = logging.getLogger("eval.scorers.llm_judge")


# ── Judge prompt (lifted from the academic literature, then trimmed) ─

JUDGE_SYSTEM = """You are an impartial evaluator of an AI assistant's response.
You will be given:
  1. The user's prompt
  2. The assistant's final answer
  3. A rubric describing what "good" looks like
  4. A list of criteria to score individually

For each criterion, assign an integer score 1–5 using these anchors:
  1 = unacceptable (criterion completely failed)
  2 = poor (significant problems)
  3 = acceptable (criterion partially met, no major failures)
  4 = good (criterion met with minor room for improvement)
  5 = excellent (criterion fully met, nothing to improve)

Be strict — most responses should land at 3 or 4. Reserve 5 for genuinely
exceptional output. Reserve 1 for output that is wrong or harmful.

Respond ONLY with a JSON object of the form:
{
  "scores": { "<criterion>": <int 1-5>, ... },
  "rationale": "<2-3 sentence justification>",
  "overall": <float 1-5, average of scores>
}
No prose outside the JSON."""

JUDGE_USER_TEMPLATE = """## User prompt
{prompt}

## Assistant answer
{answer}

## Rubric
{rubric}

## Criteria
{criteria}

Score each criterion 1–5 and respond with the JSON object."""


def _get_settings(case_judge: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve judge settings from env + per-case override. None → skip."""
    model = os.environ.get("EVAL_JUDGE_MODEL")
    if not model:
        return None
    base_url = os.environ.get(
        "EVAL_JUDGE_BASE_URL",
        os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1"),
    )
    api_key = os.environ.get(
        "EVAL_JUDGE_API_KEY",
        os.environ.get("OPENAI_API_KEY", "ollama"),
    )
    samples = int(case_judge.get("samples") or os.environ.get("EVAL_JUDGE_SAMPLES", 3))
    temperature = float(os.environ.get("EVAL_JUDGE_TEMPERATURE", 0.3))
    return {
        "model": model,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "samples": max(1, samples),
        "temperature": temperature,
    }


def _call_judge_once(
    settings: dict[str, Any],
    prompt: str,
    answer: str,
    rubric: str,
    criteria: list[str],
) -> dict[str, Any] | None:
    """One judge call. Returns parsed JSON or None on error."""
    import httpx  # local import keeps the module importable without httpx

    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": JUDGE_USER_TEMPLATE.format(
                prompt=prompt[:4000],
                answer=answer[:4000],
                rubric=rubric.strip(),
                criteria=", ".join(criteria),
            )},
        ],
        "temperature": settings["temperature"],
        "response_format": {"type": "json_object"},   # OpenAI-compatible; Ollama silently ignores
    }
    try:
        r = httpx.post(
            f"{settings['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        log.warning("judge call failed: %s", e)
        return None

    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    # Some models wrap the JSON in ```json ... ``` despite response_format.
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        log.warning("judge response had no JSON object: %s", content[:200])
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log.warning("judge JSON parse failed: %s — content=%s", e, content[:200])
        return None


def _aggregate(samples: list[dict[str, Any]]) -> tuple[float, dict[str, float], list[str]]:
    """Median aggregation across N judge samples.

    Returns (median_overall, per_criterion_medians, all_rationales).
    """
    overalls = [float(s.get("overall") or 0.0) for s in samples]
    median_overall = statistics.median(overalls) if overalls else 0.0

    # Per-criterion medians (handle missing criteria gracefully)
    all_criteria: set[str] = set()
    for s in samples:
        all_criteria.update((s.get("scores") or {}).keys())
    per_crit: dict[str, float] = {}
    for c in sorted(all_criteria):
        vals = [float((s.get("scores") or {}).get(c, 0)) for s in samples]
        vals = [v for v in vals if v > 0]
        per_crit[c] = round(statistics.median(vals), 2) if vals else 0.0

    rationales = [s.get("rationale", "")[:200] for s in samples]
    return round(median_overall, 2), per_crit, rationales


@register
def llm_judge(case: Case, traj: Trajectory) -> Score | None:
    """LLM-as-judge scorer. Opt-in via `expected.judge` in the YAML case."""
    judge_cfg = case.expected.judge
    if not judge_cfg:
        return None                                   # not opted in
    if traj.error or traj.http_status != 200:
        return None                                   # transport failed; nothing to judge
    if not traj.final_answer.strip():
        return Score(name="llm_judge", passed=False,
                     detail="judge skipped: empty final_answer")

    settings = _get_settings(judge_cfg)
    if settings is None:
        # Soft-skip: judge not configured. We DON'T mark this as failed
        # because that would break local dev runs for anyone without a
        # judge API key. CI can enforce judge availability separately.
        log.info("[%s] llm_judge skipped: EVAL_JUDGE_MODEL not set", case.id)
        return None

    rubric = judge_cfg.get("rubric") or "Is the answer correct, complete, and well-explained?"
    criteria = judge_cfg.get("criteria") or ["correctness", "completeness", "clarity"]
    min_score = float(judge_cfg.get("min_score", 4.0))

    # Self-consistency: N samples, take median.
    raw_samples: list[dict[str, Any]] = []
    for i in range(settings["samples"]):
        s = _call_judge_once(settings, case.prompt, traj.final_answer, rubric, criteria)
        if s is not None:
            raw_samples.append(s)

    if not raw_samples:
        return Score(name="llm_judge", passed=False,
                     detail=f"judge returned no parseable samples (model={settings['model']})")

    median_overall, per_crit, rationales = _aggregate(raw_samples)
    passed = median_overall >= min_score

    crit_str = ", ".join(f"{k}={v}" for k, v in per_crit.items())
    detail = (
        f"overall={median_overall} (min={min_score}) "
        f"[{crit_str}] n={len(raw_samples)} model={settings['model']} "
        f"// {rationales[0] if rationales else ''}"
    )
    return Score(name="llm_judge", passed=passed, detail=detail)
