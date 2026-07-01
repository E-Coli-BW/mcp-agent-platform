"""prepare_tasks.py — populate tasks.yaml with parent_sha + need_description.

For each task in tasks.yaml:
  1. Look up parent_sha via `git rev-parse <sha>^`.
  2. Ask an LLM to rewrite the commit_msg into a "user's original request"
     — WITHOUT showing the diff. Only commit_msg + file paths are visible to
     the LLM. This is the anti-leak trick: commit message is already a
     developer's natural-language summary, reversing to "user need" is a safe
     semantic transform.
  3. Heuristically flag commits where the message itself smuggles
     implementation details (e.g. "change Map to ConcurrentHashMap" leaks
     the data structure choice).
  4. Write the enriched tasks.yaml back in place.

Usage:
    python prepare_tasks.py            # process every task
    python prepare_tasks.py --limit 3  # just the first 3 (for smoke)
    python prepare_tasks.py --dry-run  # show what would change, don't write

Anti-leak rules baked into the LLM prompt:
  - Do NOT mention file names, function names, or specific identifiers
    from the commit message.
  - Describe the PROBLEM the user saw (symptom), not the FIX.
  - Output one sentence in Chinese, < 80 characters.

If the LLM backend isn't reachable, falls back to a heuristic rule
(strip the "fix(scope):" / "feat(scope):" prefix and rephrase).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]  # mcp root
TASKS_YAML = HERE / "tasks.yaml"

# Heuristic markers that suggest a commit message LEAKS implementation
# detail — flagged for human review. Conservative; better to flag too
# many than too few.
LEAK_MARKERS = [
    r"\bchange\s+\w+\s+to\s+\w+",     # "change Map to ConcurrentHashMap"
    r"\brename\b",                     # "rename foo to bar"
    r"\bextract\s+\w+\b",              # "extract method"
    r"\binline\s+\w+\b",               # "inline variable"
    r"\bswitch\s+from\s+\w+\s+to\b",  # "switch from X to Y"
    r"=\s*\w+",                        # "rate_limit_max = 20"
]


def get_parent_sha(sha: str) -> str:
    """Run `git rev-parse <sha>^` to get parent. Returns "" if missing."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", f"{sha}^"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()[:7]  # short SHA matches our convention
    except subprocess.CalledProcessError:
        return ""


def has_leak_marker(commit_msg: str) -> bool:
    """Heuristic: does this commit message leak implementation choice?"""
    return any(re.search(pat, commit_msg, re.IGNORECASE) for pat in LEAK_MARKERS)


def heuristic_rewrite(commit_msg: str) -> str:
    """Fallback when no LLM available: strip prefix + reword.

    Not great prose, but deterministic + leak-free. Better than nothing
    for smoke / CI.
    """
    # Strip "fix(scope): " or "feat(scope): "
    m = re.match(r"^(fix|feat|refactor|test|docs|chore)(\([^)]+\))?:\s*(.+)$", commit_msg)
    body = m.group(3) if m else commit_msg
    return f"We hit a problem: {body}. Please help me locate and fix it."


def llm_rewrite(commit_msg: str, files: list[str]) -> str:
    """Call LLM (Ollama by default) to rewrite as user-need.

    Returns "" if LLM unavailable — caller falls back to heuristic.
    """
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.environ.get("PREPARE_TASKS_MODEL", "qwen2.5:7b")

    # Show file PATHS but no file content. Paths reveal "this is in
    # auth-service" which is fine — the user would also know this; they
    # just wouldn't know "edit JwtConfig.java line 47".
    files_summary = "\n".join(f"  - {f}" for f in files[:5])

    prompt = f"""You are a user-support engineer. Below is a commit message submitted by a developer, describing a fix or a new feature.

**Back-translate** it into "what the user would have said when first making the request".

Strict rules:
1. **Describe only the symptoms of the problem**; don't say how to change it, which file, or which line
2. **Don't mention** any specific class name, function name, variable name, or config key
3. A single sentence, within 80 characters
4. Start with "I" or "we", mimicking the user's tone
5. Give the back-translation directly, without any explanation or prefix

Commit message: {commit_msg}
Files involved (for your context only — don't mention them directly): 
{files_summary}

Back-translation:"""

    try:
        import httpx

        r = httpx.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.3}},
            timeout=60,
        )
        r.raise_for_status()
        text = r.json().get("response", "").strip()
        # Defensive: strip leading quotes / "Back-translation:" if model echoed
        text = re.sub(r'^["\']*Back-translation:?\s*', "", text)
        text = re.sub(r'^["「『]+|["」』]+$', "", text)
        return text[:200]  # safety cap
    except Exception as e:
        print(f"  ⚠️  LLM call failed ({e.__class__.__name__}: {e}), falling back to heuristic", file=sys.stderr)
        return ""


def process_task(task: dict, use_llm: bool = True) -> dict:
    """Mutate a single task dict in place; return updated copy."""
    sha = task["sha"]
    print(f"  • {sha} — {task['commit_msg'][:60]}")

    # 1. Resolve parent
    if not task.get("parent_sha"):
        parent = get_parent_sha(sha)
        if not parent:
            print(f"    ⚠️  parent_sha unresolvable — skipping (commit may have been rebased away)")
            task["description_quality"] = "needs_review"
            task["need_description"] = task.get("need_description", "<TODO>")
            return task
        task["parent_sha"] = parent

    # 2. Rewrite description
    if task.get("need_description") in (None, "", "<TODO>"):
        rewritten = ""
        if use_llm:
            rewritten = llm_rewrite(task["commit_msg"], task.get("files", []))
        if not rewritten:
            rewritten = heuristic_rewrite(task["commit_msg"])
        task["need_description"] = rewritten

    # 3. Flag leak risk
    if has_leak_marker(task["commit_msg"]):
        task["description_quality"] = "needs_review"
        print(f"    🚩 leak marker detected in commit_msg — flagged needs_review")
    else:
        task["description_quality"] = "ok"

    return task


def main():
    parser = argparse.ArgumentParser(description="Populate tasks.yaml with parent_sha + need_description")
    parser.add_argument("--limit", type=int, default=None, help="Process first N tasks only (for smoke)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes, don't write")
    parser.add_argument("--no-llm", action="store_true", help="Use heuristic rewrite only (no Ollama)")
    parser.add_argument("--force", action="store_true", help="Re-process tasks even if already filled")
    args = parser.parse_args()

    if not TASKS_YAML.exists():
        print(f"❌ tasks.yaml not found at {TASKS_YAML}", file=sys.stderr)
        sys.exit(1)

    with TASKS_YAML.open() as fh:
        data = yaml.safe_load(fh)

    tasks = data.get("tasks", [])
    print(f"📋 Loaded {len(tasks)} tasks from {TASKS_YAML.name}")

    if args.limit:
        tasks_to_process = tasks[: args.limit]
        print(f"   Processing first {args.limit} (--limit)")
    else:
        tasks_to_process = tasks

    use_llm = not args.no_llm
    if use_llm:
        print(f"   Using LLM ({os.environ.get('PREPARE_TASKS_MODEL', 'qwen2.5:7b')}) for rewriting")
    else:
        print(f"   Using heuristic rewrite only (--no-llm)")

    changed = 0
    flagged = 0
    for task in tasks_to_process:
        if not args.force and task.get("need_description", "<TODO>") not in ("", "<TODO>"):
            print(f"  ⏭  {task['sha']} — already filled, skipping (use --force to redo)")
            continue
        before = task.get("need_description"), task.get("parent_sha"), task.get("description_quality")
        process_task(task, use_llm=use_llm)
        after = task.get("need_description"), task.get("parent_sha"), task.get("description_quality")
        if before != after:
            changed += 1
        if task.get("description_quality") == "needs_review":
            flagged += 1

    print(f"\n✅ Processed: {len(tasks_to_process)} | Changed: {changed} | Flagged for review: {flagged}")

    if args.dry_run:
        print("\n🔍 --dry-run: showing first 3 results, NOT writing:\n")
        print(yaml.dump({"tasks": tasks_to_process[:3]}, allow_unicode=True, sort_keys=False))
        return

    # Write back
    with TASKS_YAML.open("w") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False, width=120)
    print(f"\n💾 Wrote {TASKS_YAML}")
    print(f"\n📌 Next: review tasks where description_quality == needs_review,")
    print(f"   then run: python run.py --smoke")


if __name__ == "__main__":
    main()
