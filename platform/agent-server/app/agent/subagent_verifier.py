"""C3 — Verifier model gate on subagent outputs.

When a subagent finishes, an OPTIONAL cheap verifier model grades the
child's answer against the brief. If the answer is below the threshold
we either:

  (a) auto-retry the child ONCE with the verifier's feedback prepended
      to the brief, OR
  (b) tag the result as ``verified=False`` and return it anyway with
      the verifier reasoning in the SubagentResult's metadata.

This pairs with C1 reflexion for "defense in depth":
  - C1: the PARENT agent self-critiques its own final answer
  - C3: every SUBAGENT's output is independently graded BEFORE the
        parent ever sees it

Two independent quality gates on different message graphs, with
different prompts, different visibility scopes. A subagent answer
making it back to the parent has been through both: the parent's
critic later, and a verifier just now.

WHY this design:
==========================================

1. SEPARATE FROM C1, not shared.
   The C1 critic sees the user's question + the agent's final answer.
   The C3 verifier sees the parent's BRIEF (a sub-task spec, not a
   user question) + the child's answer. Different prompts because the
   grading question is different — "did this child do what the brief
   asked?" not "did this answer satisfy the user?". We could reuse
   the C1 module, but conflating "user question" with "parent brief"
   would have produced confusing wire-format / log-message ambiguity.

2. THE BRIEF IS THE GROUND TRUTH.
   The verifier doesn't see the child's tool trajectory, just the
   brief and the answer. This is the same hindsight-bias avoidance
   from C1: tool errors in the trajectory bias the verifier to mark
   the answer down even when the final answer is fine.

3. FAIL-OPEN IS NON-NEGOTIABLE.
   Verifier exception OR unparseable output → return the unverified
   result. We set ``verified=None`` (NOT False) so downstream can
   distinguish "verifier failed" from "verifier ran and rejected".
   The whole point of the verifier is to IMPROVE results. If it
   can't run, the unverified result is still better than nothing.

4. AUTO-RETRY IS BOUNDED TO 1.
   Subagents already run for 5-20s in the typical case. A retry
   doubles that. Beyond 1 retry the cost stops being defensible
   for the marginal quality lift. The settings knob can disable
   retry entirely (``subagent_verifier_auto_retry=False``) — useful
   for tenants with tight latency SLOs who'd rather ship the
   ``verified=False`` answer and let the parent decide.

5. OBSERVABILITY MARKER ON ``child_end``.
   The fleet_bus ``child_end`` event grows a ``verified`` field
   (None | True | False). The dashboard can render verified rates
   per role / per session. The C1 marker ``router_dispatched=True``
   set a precedent — boolean markers on terminal events are how we
   measure feature impact in production.

6. OPT-IN.
   ``subagent_verifier_enabled = False`` by default. Existing
   deployments see zero behavior change. Same opt-in pattern as
   C1 and C2.

WIRE-FORMAT
-----------
When ``verified=False`` is returned (whether after retry or skipping
retry), the SubagentResult's ``answer`` carries a prepended marker:

    ⚠️ VERIFIER (grade N/5): <reasoning>
    ---
    <original answer>

The ⚠️ prefix is a visual marker for the operator skimming logs and
a search anchor for the eval harness. The parent LLM will see the
marker and can choose to discount the answer in its own reasoning.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings

logger = logging.getLogger(__name__)


# ── Verifier prompt ────────────────────────────────────────────────────────
# Deliberately different from CRITIC_SYSTEM_PROMPT in reflexion.py — same
# 1-5 scale, but the rubric is "did the answer satisfy the BRIEF" rather
# than "did it satisfy the USER". The distinction matters: a brief is a
# scoped task spec ("find the JWT secret"), not an open question, and
# answers should be judged on completeness against the spec.
VERIFIER_SYSTEM_PROMPT = """\
You are a strict but fair grader of subagent work.

You will be given:
  1. The BRIEF the subagent was given (a scoped sub-task spec).
  2. The subagent's ANSWER.

Grade the answer on a 1-5 scale where:
  5 = fully addresses the brief, accurate, ready to consume
  4 = addresses the brief but has minor gaps a parent agent can work around
  3 = partial — addresses some aspects of the brief but misses others
  2 = mostly off-brief OR contains likely errors
  1 = fails to address the brief, off-topic, or hallucinated

Respond with ONLY a JSON object on a single line, no other text:
  {"grade": <int 1-5>, "reasoning": "<one sentence, max 200 chars>"}

Be terse. Do not include markdown code fences. Do not add commentary.\
"""


# ── Verdict object ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VerifyVerdict:
    """The verifier's decision about a subagent answer.

    ``passed=True`` means grade >= settings.subagent_verifier_min_grade.
    ``passed=False`` means below threshold OR the answer was empty.
    ``passed=None`` means the verifier failed to produce a usable
    verdict (LLM error, unparseable output, disabled) — caller treats
    this as "unknown, pass through unmodified".
    """

    passed: Optional[bool]
    grade: Optional[int]
    reasoning: str

    @classmethod
    def skipped(cls, reason: str) -> "VerifyVerdict":
        """Verifier didn't run (disabled, no answer, etc.). Treat as unknown."""
        return cls(passed=None, grade=None, reasoning=reason)


# ── Forgiving JSON parse (same approach as reflexion._parse_critic_response) ──
_JSON_OBJ_RE = re.compile(r"\{[^}]*\}", re.DOTALL)


def _parse_verifier_response(raw: str) -> Optional[tuple[int, str]]:
    """Extract (grade, reasoning) from the verifier's raw text.

    Tolerant: small models often wrap JSON in markdown fences or add
    a polite intro sentence. We search for the first {...} block,
    parse it, validate the grade is an int 1-5. On any failure return
    None and the caller treats it as "unknown → pass through".

    Intentionally duplicated from reflexion._parse_critic_response
    rather than imported. The two parsers happen to have the same
    shape today but will likely drift: a verifier could grow extra
    fields (``missing_topics``, ``suspect_identifiers``) that the
    critic shouldn't have to know about. Coupling them via shared
    import would be premature.
    """
    if not raw or not raw.strip():
        return None
    match = _JSON_OBJ_RE.search(raw)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    grade = obj.get("grade")
    reasoning = obj.get("reasoning", "")
    if not isinstance(grade, int) or not (1 <= grade <= 5):
        return None
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    return grade, reasoning[:200]


# ── Lazy verifier model cache ──────────────────────────────────────────────
# Same pattern as the critic cache in reflexion.py. Keyed by model name
# so a tenant switching `subagent_verifier_model` at runtime doesn't
# reuse a stale chat client.
_verifier_cache: dict[str, BaseChatModel] = {}


def _get_verifier(injected: Optional[BaseChatModel]) -> BaseChatModel:
    """Resolve the verifier model. Tests inject a fake; prod lazily
    builds from settings.subagent_verifier_model or cheap_model."""
    if injected is not None:
        return injected
    # Lazy import: graph.py pulls in langchain providers and is heavy.
    from app.agent.graph import _create_chat_model

    name = settings.subagent_verifier_model or settings.cheap_model
    if name not in _verifier_cache:
        # T=0 so grades are reproducible across identical inputs.
        _verifier_cache[name] = _create_chat_model(name, temperature=0.0)
    return _verifier_cache[name]


# Test helper.
def _reset_verifier_cache_for_tests() -> None:
    _verifier_cache.clear()


# ── Public API ─────────────────────────────────────────────────────────────
async def verify_subagent_answer(
    *,
    brief: str,
    answer: str,
    verifier_model: Optional[BaseChatModel] = None,
) -> VerifyVerdict:
    """Grade the subagent's answer against the brief.

    Returns ``VerifyVerdict.skipped`` (passed=None) when the verifier
    is disabled, the answer is empty, or the verifier fails. Returns
    a populated verdict (passed=True/False, grade, reasoning) on
    successful grading.

    This function NEVER raises on the happy path — even verifier model
    failures collapse to ``skipped``. The fail-open invariant is the
    most important contract here.

    Args:
        brief: The task spec the subagent was given (parent's input).
        answer: The subagent's final answer string.
        verifier_model: Optional pre-built chat model for tests.

    Returns:
        VerifyVerdict.
    """
    if not settings.subagent_verifier_enabled:
        return VerifyVerdict.skipped("verifier disabled")
    if not answer or not answer.strip():
        # Empty answer is a separate signal — the subagent failed to
        # produce. Verifier doesn't need to grade nothing. Caller
        # already has `error` set on the SubagentResult.
        return VerifyVerdict.skipped("empty answer")
    if not brief or not brief.strip():
        # Defensive — empty brief shouldn't happen but if it does we
        # can't grade against it. Pass through.
        return VerifyVerdict.skipped("empty brief")

    verifier_input = [
        SystemMessage(content=VERIFIER_SYSTEM_PROMPT),
        HumanMessage(content=(
            "BRIEF:\n"
            f"{brief}\n\n"
            "ANSWER:\n"
            f"{answer}"
        )),
    ]

    try:
        verifier = _get_verifier(verifier_model)
        response = await verifier.ainvoke(verifier_input)
    except Exception as e:
        # Catch broadly because verifier failures must not fail the
        # subagent. This is fail-open by design — see C2 commit body
        # for the "don't be too defensive too high in the stack"
        # lesson; here it's INTENTIONAL because the verifier sits at
        # the very end of an otherwise-completed subagent and its
        # failure mode is exactly "the answer the child already
        # produced is what we ship". A narrower except clause would
        # only let unknown errors crash the entire spawn, not improve
        # anything.
        logger.warning("⚠️ verifier invocation failed, passing through: %s", e)
        return VerifyVerdict.skipped(f"verifier error: {type(e).__name__}")

    raw = response.content if isinstance(response.content, str) else str(response.content)
    parsed = _parse_verifier_response(raw)
    if parsed is None:
        logger.warning(
            "⚠️ verifier produced unparseable output, passing through. raw=%r",
            raw[:200],
        )
        return VerifyVerdict.skipped("unparseable verifier output")

    grade, reasoning = parsed
    passed = grade >= settings.subagent_verifier_min_grade
    logger.info(
        "⚠️ verifier grade=%d/5 passed=%s: %s",
        grade, passed, reasoning,
    )
    return VerifyVerdict(passed=passed, grade=grade, reasoning=reasoning)


def format_verifier_marker(verdict: VerifyVerdict, original_answer: str) -> str:
    """Prepend a ⚠️ VERIFIER marker to the answer when verdict failed.

    Used when auto-retry is disabled OR the retry also failed.
    The parent LLM will see this marker and can choose to discount
    the subagent's answer in its own reasoning.

    Pass-through (returns original_answer unchanged) when verdict
    passed or was skipped — adding markers to passing answers would
    confuse the parent with noise.
    """
    if verdict.passed is not False:  # passed=True or passed=None
        return original_answer
    grade_str = f"{verdict.grade}/5" if verdict.grade is not None else "?/5"
    return (
        f"⚠️ VERIFIER (grade {grade_str}): {verdict.reasoning}\n"
        f"---\n"
        f"{original_answer}"
    )


def format_retry_brief(original_brief: str, verdict: VerifyVerdict) -> str:
    """Build the brief for the auto-retry attempt.

    Prepends the verifier's reasoning so the retried child sees what
    the verifier thought was wrong. The original brief is preserved
    verbatim below — we want the child to attempt the same task with
    more context, not a substituted task.
    """
    grade_str = f"{verdict.grade}/5" if verdict.grade is not None else "?/5"
    return (
        f"Your previous attempt at this brief was graded {grade_str} by a "
        f"verifier. The verifier said: {verdict.reasoning}\n\n"
        f"Please retry, addressing the issue above. The original brief follows:\n\n"
        f"{original_brief}"
    )
