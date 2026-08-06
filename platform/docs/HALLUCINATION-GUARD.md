# Hallucination Guard — Tool-Use Verification in LangGraph v2

## Problem

Small LLMs (7B parameters, e.g. `qwen2.5:7b`) frequently **hallucinate tool usage** — they produce text *describing* tool calls rather than issuing structured function calls through the API. Example:

```
User: "Write a file called hello.py"

❌ Bad (hallucinated):
   "I'll use file_write to create hello.py with the following content...
    ```sh
    file_write path="hello.py" content="print('hello')"
    ```
    Done! The file has been created."

✅ Good (actual tool call):
   tool_calls: [{name: "file_write", args: {path: "hello.py", content: "print('hello')"}}]
```

The user sees a confident response claiming actions were taken, but **nothing actually happened**. This is particularly dangerous because:
1. The agent appears functional (no errors shown)
2. Users may not realize edits weren't persisted
3. Follow-up questions compound the hallucination ("commit it" → hallucinated git commit)

## Root Cause Analysis

| Factor | Detail |
|--------|--------|
| Model size | 7B models have weaker instruction-following for structured output |
| Training data | Models trained on text showing tool usage patterns (tutorials, docs) |
| Tool binding | Ollama's tool-calling support is less robust than OpenAI's native API |
| Context pressure | As conversation grows, model "forgets" it must use structured calls |

## Solution: Graph-Level Hallucination Detection

We added a **hallucination guard** as a dedicated node in the LangGraph v2 `StateGraph`:

### Architecture

```
call_llm → maybe_critique → route
                               ├─ tools           (actual tool_calls present)
                               ├─ hallucination_fix → call_llm  ← NEW
                               ├─ call_llm        (reflexion 🪞 revision)
                               └─ END             (pure text response, no tool mentions)
```

### Detection (in route node)

```python
_HALLUCINATED_TOOL_RE = re.compile(
    r"(?:Using |🔧 Using |📝 |🔍 )?(file_write|file_read|file_list|...)"
    r"[\s.:(]",
    re.IGNORECASE,
)

# In the route function:
if isinstance(last, AIMessage) and not last.tool_calls:
    content = last.content
    matches = _HALLUCINATED_TOOL_RE.findall(content)
    if matches:
        return "hallucination_fix"  # → correction node
```

**Key insight**: If the LLM's response **mentions tool names** but has **no `tool_calls`** in the structured output, it hallucinated.

### Correction (hallucination_fix node)

```python
_CORRECTION = (
    "🚨 STOP. You just described using tools in plain text, but you did NOT "
    "actually call any tools. Your response was NOT executed. "
    "You MUST use the function calling interface to invoke tools — "
    "do NOT write tool names or commands in your text response. "
    "Try again: call the appropriate tool NOW using a proper function call."
)
```

This message is injected as a `HumanMessage`, then the graph loops back to `call_llm`. The model sees the correction and (typically) issues a proper tool call on the next attempt.

### Safety Guards

- **Max steps**: The hallucination fix counts toward `loop_counter`, so it can't infinite-loop
- **ToolMessage bypass**: If the previous message is a `ToolMessage` (real tool result), the guard is skipped — the model is legitimately summarizing what a tool returned
- **Doesn't interfere with legitimate text**: Pure text responses that don't mention tool names route to END normally

### Bug Found & Fixed: Infinite Loop After Real Tool Calls

**Symptom**: Agent called `code_run` successfully, got result `4`, then entered an infinite loop — calling the tool 9× before hitting recursion limit (80).

**Root cause**: After a real tool call returns, the model summarizes the result: *"The code_run executed successfully. The output was `4`."* The hallucination guard's regex matched `code_run` in this text and triggered a correction. The correction confused the model into calling the tool again, which produced another summary, which triggered the guard again → infinite loop.

**Timeline**:
```
Loop 1: LLM → tool_calls: [code_run] → ToolMessage("4")
Loop 2: LLM → text "The code_run executed..." → Guard fires! → correction injected
Loop 3: LLM → tool_calls: [code_run] → ToolMessage("4")  (model re-calls!)
Loop 4: LLM → text "The code_run executed..." → Guard fires again!
... repeats until recursion_limit=80
```

**Fix**: Before checking for hallucinated tool names, inspect the previous message. If it's a `ToolMessage`, the model just received a real tool result — skip the guard.

```python
if isinstance(last, AIMessage) and not last.tool_calls:
    prev = state.messages[-2] if len(state.messages) >= 2 else None
    has_recent_tool_result = isinstance(prev, ToolMessage)
    if not has_recent_tool_result:
        # ... hallucination detection logic
```

**Lesson**: Safety mechanisms that *inject corrections* can themselves create feedback loops if their trigger conditions overlap with normal behavior. Always check: "Can the guard fire on the output it just caused?"

## Interview Talking Points

### 1. Why is this a graph node and not a post-processing filter?

> A post-processor can only reject the response — it can't **retry**. By making it a graph node with an edge back to `call_llm`, we get automatic retry with correction context. The LangGraph state machine handles the loop cleanly, and the `loop_counter` provides natural termination.

### 2. How does this compare to OpenAI's `tool_choice: "required"`?

> `tool_choice: required` forces the model to always produce a tool call, which breaks pure-text responses (e.g., "Here's an explanation of..."). Our approach is **selective** — it only triggers when the model's text *implies* it used tools but didn't. This preserves the model's ability to respond with text when appropriate.

### 3. What's the false positive rate?

> Low in the original design, but we discovered a critical **false positive that caused infinite loops**. When the model successfully calls a tool and then summarizes the result ("The code_run executed successfully..."), the regex matched the tool name in the summary text. This turned a 1-tool-call request into a 9-call infinite loop that hit the recursion limit.
>
> **Fix**: Skip the guard entirely if the previous message is a `ToolMessage` — in that case the model is legitimately referring to a tool it just used. This is a great example of why you need **integration testing under load**, not just unit tests — the loop only manifests during real multi-turn execution.

### 4. Tell me about a time a safety mechanism backfired

> Our hallucination guard was designed to prevent the model from *pretending* to call tools. But after a real tool call, the model's summary ("code_run returned 4") triggered the same guard, which injected a correction ("you didn't actually call tools!"), which confused the model into calling the tool *again*. The benchmark test showed 9 redundant `code_run` calls before hitting recursion_limit=80.
>
> **Debugging approach**: The audit logs showed real Docker executions with realistic latency variance (979ms cold, 300ms warm), proving the tools were actually executing. The `🚨 Hallucinated tool use detected` warnings alternating with `✅ Tool result` confirmed the guard was incorrectly firing post-result.
>
> **Fix**: A one-line check — `isinstance(prev, ToolMessage)` — to distinguish "model summarizing a real result" from "model hallucinating a tool call." Classic case of a safety net creating the very failure mode it was designed to prevent.

### 4. Why not fine-tune the model instead?

> Fine-tuning is the ideal long-term fix but requires:
> - Training data collection (expensive)
> - Model retraining pipeline
> - Version management
>
> The hallucination guard is a **zero-cost runtime mitigation** that works across any model, deployed in minutes. It's defense-in-depth — you'd still want it even with a fine-tuned model as a safety net.

### 5. How does this interact with the reflexion (self-critique) system?

> They're orthogonal. Reflexion checks **answer quality** (did the agent address the user's question?). Hallucination guard checks **execution integrity** (did the agent actually do what it claims?). The graph flow is:
> ```
> call_llm → maybe_critique (reflexion) → route → hallucination_fix (if detected)
> ```
> Reflexion runs first; if it passes but the response hallucinates tools, the guard catches it.

## Metrics

After deploying this guard:
- Hallucination detection rate: observable via `🚨 Hallucinated tool use detected` log line
- Recovery rate: % of hallucination corrections that lead to actual tool calls on retry
- Impact on latency: +1 LLM round-trip (~2-8s for 7B) when triggered

## When to Upgrade the Model Instead

If the hallucination guard fires on >30% of tool-requiring requests, the model is fundamentally too weak for agentic use. Recommended upgrades:
- `qwen2.5:14b` or `qwen2.5:32b` (local, much better tool calling)
- `deepseek-coder-v2:16b` (excellent for code tasks)
- Cloud: `gpt-4o-mini` or `claude-3.5-sonnet` (near-zero hallucination rate)
