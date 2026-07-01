"""Compression — AST-aware code compression for tool output.

Public entry points:
    from app.rag.compression import compress_code_output
"""

from app.rag.compression.ast import compress_code_output

__all__ = ["compress_code_output"]
