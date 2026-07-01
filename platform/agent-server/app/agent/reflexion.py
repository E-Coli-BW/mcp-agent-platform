"""Reflexion / self-critique node for the v2 StateGraph.

WHAT this is
------------
A LangGraph node that, given the AgentState, inspects the LAST AIMessage.
If that message is a "final answer" turn (no tool calls), the critic
asks a small/cheap LLM to grade the answer 1-5 on a composite axis
(correctness + completeness + evidence) and either:

  - pass through silently (grade >= ``reflexion_min_grade``), OR
  - return a HumanMessage that says "your answer was rated N/5
    because X — please revise" so the next call_llm iteration sees
    the critique and can fix the gap.

A separate state field ``critique_attempts`` is incremented per revision
so we can cap retries and avoid infinite oscillation between actor and
critic. The existing ``loop_counter`` (which counts ReAct iterations)
is NOT touched — critique revisions and tool-use rounds are different
failure domains.

DESIGN DECISIONS
----------------

1. **Composite single grade, not 3 separate dimensions.**
   We considered (correctness, completeness, evidence) as 3 grades the
   critic returns separately. In small-model probing the model often
   conflated them anyway, and the 3-tuple added prompt complexity
   without changing downstream behavior (we just min() them). Single
   grade is what we route on; the reasoning string preserves the
   nuance for logs and the revision hint.

2. **Hard cap at ``reflexion_max_attempts`` (default 2).**
   Reflexion has sharp diminishing returns past 2 retries — the model
   starts rephrasing rather than fixing. We picked 2 by inspection;
   the eval harness will measure whether 1 or 3 is better per agent
   config. Always-pass-through after the cap so the user gets *some*
   answer even if the critic is grumpy.

3. **JSON output with a graceful fallback.**
   Small models botch JSON ~10-15% of the time. If parsing fails we
   treat it as "grade unknown → pass through" rather than treating
   the malformed output as a revision request. A botched critique
   should never make the agent worse — that's the bar.

4. **The critic gets NO tools.**
   A critic with tools is just another actor. We pass an unbounded
   pure-LLM ``ainvoke`` here. This also makes the critic deterministic
   given the messages — useful when debugging revision loops.

5. **Critic sees ONLY the user's last question + the agent's answer.**
   We deliberately DON'T pass the full conversation history or the tool
   trace. Rationale: we want the critic to grade the *answer* the user
   sees, not to second-guess the trajectory that produced it. Passing
   the trajectory tempts the critic into "you should have used tool X
   instead" hindsight which makes a worse user experience for the same
   correct answer.

6. **Critic is opt-in via ``settings.reflexion_enabled``.**
   Off by default because the cost-vs-quality trade is per-deployment.
   When off, ``maybe_critique_node`` is a no-op pass-through (still
   wired in the graph, but cheap to call — one dict access).

WIRE-FORMAT
-----------
When a revision is triggered the appended HumanMessage has the format:

    🪞 SELF-CRITIQUE (grade N/5): <critic's reasoning>.
    Please revise your previous answer to address the issues above.

The 🪞 prefix is a visual marker for the operator skimming logs and a
search anchor for the eval harness ("did the critic actually fire on
this case?").

EVAL HOOKS
----------
The reasoning + grade are logged at INFO so the existing audit pipeline
captures them. A future commit can attach them to the AuditAspect line
as a `critique_grade=N` field for the dashboard.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.config import settings

logger = logging.getLogger(__name__)


# ── Critic prompt ──────────────────────────────────────────────────────────
# Deliberately TERSE. The critic is small/cheap and we want it to commit
# to a grade rather than reason at length. The "respond with ONLY JSON"
# instruction is a small-model concession; bigger models would accept
# structured-output schemas but we want this to work on local 7Bs too.
CRITIC_SYSTEM_PROMPT = """\
You are a strict but fair grader.

You will be given:
  1. A user's question.
  2. An assistant's draft answer.

Grade the draft answer on a 1-5 scale where:
  5 = correct, complete, and well-supported by evidence
  4 = correct and complete, but evidence is thin or implicit
  3 = mostly correct but missing a meaningful aspect of the question
  2 = partially wrong OR misses the main point
  1 = wrong, hallucinated, or completely off-topic

Respond with ONLY a JSON object on a single line, no other text:
  {"grade": <int 1-5>, "reasoning": "<one sentence, max 200 chars>"}

Be terse. Do not include markdown code fences. Do not add commentary.\
"""


# ── State helpers ──────────────────────────────────────────────────────────
def _find_last_user_question(messages: list[BaseMessage]) -> Optional[str]:
    """Walk backwards to find the user's most recent question.

    Skips our own critique HumanMessages (they have the 🪞 marker) so
    the critic grades against the ORIGINAL user question, not its own
    earlier critique. Without this guard the critic would re-judge the
    revised answer against its own critique text — circular.
    """
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content.startswith("🪞"):
                continue
            return content
    return None


def _find_last_assistant_answer(messages: list[BaseMessage]) -> Optional[AIMessage]:
    """The most recent AIMessage with no tool calls. That's the 'answer'
    turn — the one the user will actually see. AIMessages with tool calls
    are intermediate reasoning, not for grading."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content.strip():
                return msg
    return None


# ── JSON parsing with a forgiving fallback ─────────────────────────────────
_JSON_OBJ_RE = re.compile(r"\{[^}]*\}", re.DOTALL)


def _parse_critic_response(raw: str) -> Optional[tuple[int, str]]:
    """Extract (grade, reasoning) from the critic's raw text.

    Tolerant: small models often wrap JSON in markdown fences or add
    a polite intro sentence. We search for the first {...} block,
    parse it, and validate the grade is an int 1-5. On any failure
    we return None and the caller treats it as "unknown → pass through".
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


# ── The node ───────────────────────────────────────────────────────────────
def make_maybe_critique_node(critic_model: Optional[BaseChatModel] = None):
    """Factory returning a LangGraph node that may inject a revision request.

    Returns a coroutine fn that takes AgentState and returns a dict patch
    in the LangGraph node convention. The patch is:

      - ``{}`` (no-op) when:
          * reflexion is disabled in settings, OR
          * the last AIMessage has tool calls (not a final answer), OR
          * we've already hit ``reflexion_max_attempts``, OR
          * the critic gave a passing grade, OR
          * the critic failed to produce parseable output (fail-open)

      - ``{"messages": [HumanMessage("🪞 SELF-CRITIQUE ...")],
            "critique_attempts": N+1}``
        when the critic returned grade < ``reflexion_min_grade``.

    The factory takes an optional pre-built critic model so tests can
    inject a fake. In production, ``critic_model`` is None and we
    lazily build one from ``settings.reflexion_model`` (or fall back to
    ``settings.cheap_model``) on first invocation.
    """
    _critic_cache: dict[str, BaseChatModel] = {}

    def _get_critic() -> BaseChatModel:
        nonlocal critic_model
        if critic_model is not None:
            return critic_model
        # Lazy import to keep the graph module's import time fast.
        from app.agent.graph import _create_chat_model

        name = settings.reflexion_model or settings.cheap_model
        if name not in _critic_cache:
            # T=0 for the critic so its grade is reproducible.
            # The actor's temperature is independent.
            _critic_cache[name] = _create_chat_model(name, temperature=0.0)
        return _critic_cache[name]

    async def maybe_critique(state) -> dict[str, Any]:
        if not settings.reflexion_enabled:
            return {}

        attempts = getattr(state, "critique_attempts", 0)
        if attempts >= settings.reflexion_max_attempts:
            logger.debug(
                "🪞 critique skipped: at attempt cap (%d/%d)",
                attempts, settings.reflexion_max_attempts,
            )
            return {}

        messages = list(state.messages)
        last_answer = _find_last_assistant_answer(messages)
        if last_answer is None:
            # No final-answer turn yet — agent is still mid-tool-use.
            # Critique is a no-op until the model says it's done.
            return {}

        user_question = _find_last_user_question(messages)
        if user_question is None:
            # Defensive: no user message anywhere? Pass through.
            return {}

        answer_text = (
            last_answer.content if isinstance(last_answer.content, str)
            else str(last_answer.content)
        )

        # Build the critic input — system prompt + a single user turn
        # carrying both the question and the draft answer.
        critic_input = [
            SystemMessage(content=CRITIC_SYSTEM_PROMPT),
            HumanMessage(content=(
                "QUESTION:\n"
                f"{user_question}\n\n"
                "DRAFT ANSWER:\n"
                f"{answer_text}"
            )),
        ]

        try:
            critic = _get_critic()
            response = await critic.ainvoke(critic_input)
        except Exception as e:  # nosec — fail-open on any critic error
            # Critic failures must NEVER fail the request. We log and
            # pass through. The whole point of the critic is to IMPROVE
            # the answer; if it can't run, the unimproved answer is
            # still better than no answer.
            logger.warning("🪞 critic invocation failed, passing through: %s", e)
            return {}

        raw = response.content if isinstance(response.content, str) else str(response.content)
        parsed = _parse_critic_response(raw)
        if parsed is None:
            logger.warning(
                "🪞 critic produced unparseable output, passing through. raw=%r",
                raw[:200],
            )
            return {}

        grade, reasoning = parsed
        logger.info(
            "🪞 critique grade=%d/5 (attempt %d/%d): %s",
            grade, attempts + 1, settings.reflexion_max_attempts, reasoning,
        )

        if grade >= settings.reflexion_min_grade:
            # Passing grade — let the agent's answer through unchanged.
            return {}

        # Below threshold — inject a revision request and bump the
        # attempt counter. The next iteration of call_llm will see the
        # original answer + this hint and produce a revised answer.
        revision_hint = (
            f"🪞 SELF-CRITIQUE (grade {grade}/5): {reasoning}. "
            "Please revise your previous answer to address the issues above."
        )
        return {
            "messages": [HumanMessage(content=revision_hint)],
            "critique_attempts": attempts + 1,
        }

    return maybe_critique
