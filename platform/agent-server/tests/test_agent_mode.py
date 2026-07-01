"""Tests for agent_mode tools — file_write, file_edit, git operations."""

import os
import subprocess
import pytest

from app.tools.agent_mode import (
    set_workspace_root, get_workspace_root,
    file_write, file_edit, git_status, git_diff, git_commit,
)


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    set_workspace_root(str(tmp_path))
    yield tmp_path


class TestFileWrite:
    def test_write_creates_file(self, workspace):
        result = file_write.invoke({"path": "hello.py", "content": "print('hello')\n"})
        assert "✅" in result
        assert (workspace / "hello.py").read_text() == "print('hello')\n"

    def test_write_creates_nested_dirs(self, workspace):
        result = file_write.invoke({"path": "a/b/c/deep.py", "content": "x = 1\n"})
        assert "✅" in result
        assert (workspace / "a" / "b" / "c" / "deep.py").exists()

    def test_write_overwrites_existing(self, workspace):
        (workspace / "exist.py").write_text("old content")
        result = file_write.invoke({"path": "exist.py", "content": "new content"})
        assert "✅" in result
        assert (workspace / "exist.py").read_text() == "new content"

    def test_write_outside_workspace_blocked(self):
        result = file_write.invoke({"path": "/tmp/evil.py", "content": "hack"})
        assert "❌" in result or "outside" in result.lower()

    def test_write_reports_line_count(self, workspace):
        result = file_write.invoke({"path": "multi.py", "content": "a\nb\nc\n"})
        assert "4 lines" in result  # 3 newlines + 1


class TestFileEdit:
    def test_edit_replaces_text(self, workspace):
        (workspace / "app.py").write_text("name = 'old'\nprint(name)\n")
        result = file_edit.invoke({"path": "app.py", "old_text": "'old'", "new_text": "'new'"})
        assert "✅" in result
        assert (workspace / "app.py").read_text() == "name = 'new'\nprint(name)\n"

    def test_edit_first_occurrence_only(self, workspace):
        (workspace / "dup.py").write_text("x = 1\nx = 1\n")
        file_edit.invoke({"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"})
        assert (workspace / "dup.py").read_text() == "x = 2\nx = 1\n"

    def test_edit_text_not_found(self, workspace):
        (workspace / "app.py").write_text("hello world\n")
        result = file_edit.invoke({"path": "app.py", "old_text": "goodbye", "new_text": "hi"})
        assert "❌" in result
        assert "not found" in result.lower() or "Text not found" in result

    def test_edit_nonexistent_file(self):
        result = file_edit.invoke({"path": "nope.py", "old_text": "a", "new_text": "b"})
        assert "❌" in result


class TestGitOps:
    def test_git_status_initializes_repo(self, workspace):
        result = git_status.invoke({})
        assert "Initialized" in result or "clean" in result.lower()

    def test_git_status_shows_changes(self, workspace):
        subprocess.run(["git", "init"], cwd=str(workspace), capture_output=True)
        (workspace / "new.py").write_text("x = 1\n")
        result = git_status.invoke({})
        assert "new.py" in result or "Changes" in result

    def test_git_commit_with_message(self, workspace):
        subprocess.run(["git", "init"], cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(workspace), capture_output=True)
        (workspace / "init.py").write_text("# init\n")
        result = git_commit.invoke({"message": "initial commit"})
        assert "✅" in result or "Committed" in result

    def test_git_commit_nothing_to_commit(self, workspace):
        subprocess.run(["git", "init"], cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(workspace), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(workspace), capture_output=True)
        result = git_commit.invoke({"message": "empty"})
        assert "Nothing" in result or "clean" in result.lower()
