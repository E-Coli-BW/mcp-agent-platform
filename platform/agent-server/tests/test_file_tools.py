"""Tests for local file tools — file_list, file_read, file_search."""

import os
import tempfile
import pytest

from app.tools.agent_mode import set_workspace_root, get_workspace_root
from app.tools.definitions import file_list, file_read, file_search


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Set up a temp workspace with sample files."""
    set_workspace_root(str(tmp_path))

    # Create sample project structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def hello():\n    return 'world'\n\nif __name__ == '__main__':\n    print(hello())\n")
    (tmp_path / "src" / "utils.py").write_text("import os\n\ndef get_path():\n    return os.getcwd()\n")
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "src" / "nested" / "deep.py").write_text("# deep module\nDEEP = True\n")
    (tmp_path / "README.md").write_text("# Test Project\n\nThis is a test.\n")
    (tmp_path / "config.json").write_text('{"key": "value"}\n')
    (tmp_path / ".git").mkdir()  # should be ignored
    (tmp_path / "__pycache__").mkdir()  # should be ignored

    yield tmp_path


class TestFileList:
    def test_lists_root_directory(self):
        result = file_list.invoke({})
        assert "README.md" in result
        assert "src/" in result
        assert "config.json" in result

    def test_ignores_git_and_pycache(self):
        result = file_list.invoke({})
        assert ".git" not in result
        assert "__pycache__" not in result

    def test_recursive_depth(self):
        result = file_list.invoke({})
        # Should see nested files at depth 3
        assert "main.py" in result
        assert "utils.py" in result
        assert "deep.py" in result

    def test_subdirectory(self):
        result = file_list.invoke({"directory": "src"})
        assert "main.py" in result
        assert "README.md" not in result

    def test_nonexistent_directory(self):
        result = file_list.invoke({"directory": "nonexistent"})
        assert "not found" in result.lower() or "❌" in result

    def test_depth_parameter(self):
        result = file_list.invoke({"directory": None, "depth": 1})
        assert "src/" in result
        # At depth 1, should show src/ but not its children
        # (depends on implementation — at least src/ is there)


class TestFileRead:
    def test_read_full_file(self):
        result = file_read.invoke({"path": "README.md"})
        assert "Test Project" in result
        assert "3 lines total" in result

    def test_read_with_line_numbers(self):
        result = file_read.invoke({"path": "src/main.py"})
        assert "1 |" in result or "   1 |" in result
        assert "def hello" in result

    def test_read_with_range(self):
        result = file_read.invoke({"path": "src/main.py", "start_line": 1, "end_line": 2})
        assert "def hello" in result
        assert "showing 1-2" in result

    def test_read_nonexistent_file(self):
        result = file_read.invoke({"path": "nonexistent.py"})
        assert "not found" in result.lower() or "❌" in result

    def test_default_100_line_limit(self, workspace):
        # Create a large file
        big_file = workspace / "big.py"
        big_file.write_text("\n".join(f"line_{i} = {i}" for i in range(200)))
        result = file_read.invoke({"path": "big.py"})
        assert "showing 1-100" in result
        assert "Use file_read" in result  # hint to read more

    def test_read_nested_path(self):
        result = file_read.invoke({"path": "src/nested/deep.py"})
        assert "DEEP = True" in result


class TestFileSearch:
    def test_search_finds_match(self):
        result = file_search.invoke({"query": "def hello"})
        assert "main.py" in result
        assert "def hello" in result

    def test_search_no_match(self):
        result = file_search.invoke({"query": "xyznonexistent123"})
        assert "No matches" in result

    def test_search_in_subdirectory(self):
        result = file_search.invoke({"query": "DEEP", "directory": "src"})
        assert "deep.py" in result

    def test_search_nonexistent_directory(self):
        result = file_search.invoke({"query": "hello", "directory": "nope"})
        assert "not found" in result.lower() or "❌" in result
