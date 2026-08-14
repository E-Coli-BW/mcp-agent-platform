"""Stable context-compression API."""

from app.agent.compressor.investigation import (
    InvestigationState,
    update_investigation_from_messages,
)
from app.agent.compressor.messages import (
    CompressionResult,
    compress_messages,
    estimate_chars,
    estimate_tokens,
)

__all__ = [
    "CompressionResult",
    "InvestigationState",
    "compress_messages",
    "estimate_chars",
    "estimate_tokens",
    "update_investigation_from_messages",
]
