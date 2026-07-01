"""Token counting and cost tracking for LLM calls.

Tracks prompt/completion tokens per request and estimates cost.
Logs to console and exposes via /api/usage endpoint.
"""

import time
import logging
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)

# Cost per 1M tokens (input/output) — approximate pricing May 2026
MODEL_PRICING = {
    # Local models (free)
    "qwen2.5:7b": {"input": 0.0, "output": 0.0},
    "llama3": {"input": 0.0, "output": 0.0},
    # Cloud models
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku": {"input": 0.25, "output": 1.25},
    "deepseek-coder": {"input": 0.14, "output": 0.28},
}


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code."""
    return max(1, len(text) // 4)


@dataclass
class RequestUsage:
    """Token usage for a single request."""
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def calculate_cost(self):
        pricing = MODEL_PRICING.get(self.model, {"input": 0.0, "output": 0.0})
        self.cost_usd = (
            self.prompt_tokens * pricing["input"] / 1_000_000
            + self.completion_tokens * pricing["output"] / 1_000_000
        )
        return self.cost_usd


class UsageTracker:
    """Tracks cumulative token usage and cost across all requests."""

    def __init__(self):
        self.total_requests: int = 0
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.total_tool_calls: int = 0
        self.total_cost_usd: float = 0.0
        self.per_model: dict[str, dict] = defaultdict(lambda: {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0
        })
        self._start_time = time.time()

    def record(self, usage: RequestUsage):
        """Record a completed request's usage."""
        usage.calculate_cost()

        self.total_requests += 1
        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        self.total_tool_calls += usage.tool_calls
        self.total_cost_usd += usage.cost_usd

        model_stats = self.per_model[usage.model]
        model_stats["requests"] += 1
        model_stats["prompt_tokens"] += usage.prompt_tokens
        model_stats["completion_tokens"] += usage.completion_tokens
        model_stats["cost_usd"] += usage.cost_usd

        logger.info(
            "📊 Usage: model=%s prompt=%d completion=%d tools=%d cost=$%.6f total_cost=$%.4f",
            usage.model, usage.prompt_tokens, usage.completion_tokens,
            usage.tool_calls, usage.cost_usd, self.total_cost_usd,
        )

    def get_summary(self) -> dict:
        """Return usage summary for the /api/usage endpoint."""
        uptime = time.time() - self._start_time
        return {
            "total_requests": self.total_requests,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "total_tool_calls": self.total_tool_calls,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "uptime_seconds": int(uptime),
            "per_model": dict(self.per_model),
        }


# Singleton
_tracker = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    return _tracker
