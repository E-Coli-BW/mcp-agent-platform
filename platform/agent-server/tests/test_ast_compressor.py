"""Tests for AST-aware context compressor."""

import pytest
from app.rag.compression.ast import compress_code_output, _detect_language


class TestLanguageDetection:
    def test_detects_python_from_file_header(self):
        content = "File: src/main.py (50 lines total, showing 1-50)\n   1 | def hello():\n"
        assert _detect_language(content) == ".py"

    def test_detects_java_from_file_header(self):
        content = "File: src/Main.java (30 lines)\n   1 | public class Main {\n"
        assert _detect_language(content) == ".java"

    def test_detects_python_from_content(self):
        content = "import os\n\ndef hello(self):\n    pass\n"
        assert _detect_language(content) == ".py"

    def test_detects_java_from_content(self):
        content = "public class Foo {\n    private int x;\n}\n"
        assert _detect_language(content) == ".java"

    def test_returns_none_for_unknown(self):
        content = "just some random text with no code markers"
        assert _detect_language(content) is None


class TestCompressCodeOutput:
    def test_short_content_unchanged(self):
        content = "def hello(): pass"
        result = compress_code_output(content, max_chars=1500)
        assert result == content

    def test_long_content_compressed(self):
        # Create a long Python function
        content = "File: test.py (200 lines)\n"
        content += "   1 | def calculate_total(items, tax_rate=0.1):\n"
        content += "   2 |     '''Calculate total price with tax.'''\n"
        for i in range(3, 150):
            content += f"  {i:2d} |     x = x + {i}\n"
        content += " 150 |     return total\n"

        result = compress_code_output(content, max_chars=1500)
        assert len(result) <= 1500
        # Should preserve key structural elements
        assert "calculate_total" in result or "compressed" in result.lower()

    def test_fallback_on_non_code(self):
        # Non-code content should use head+tail fallback
        content = "Error: " + "x" * 3000
        result = compress_code_output(content, max_chars=1500)
        assert len(result) <= 1500
        assert "compressed" in result.lower() or "..." in result

    def test_preserves_function_name(self):
        content = "   1 | def my_special_function(arg1, arg2):\n"
        content += "   2 |     '''Does something special.'''\n"
        content += "\n".join(f"  {i:2d} |     line = {i}" for i in range(3, 100))
        content += "\n 100 |     return result\n"

        result = compress_code_output("File: test.py (100 lines)\n" + content, max_chars=800)
        # Either AST extracts the name, or fallback keeps head (which has the name)
        assert "my_special_function" in result
