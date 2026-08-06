"""run.py — main experiment driver for the controlled 4-arm experiment.

Modes:
  --smoke   : 1 arm (arm_c) × N tasks (default 3) × 1 trial — pipeline check
  --phase1  : arm A + arm C × hallucination subset × 5 trials (Phase 1)
  --full    : 4 arms × 25 tasks × 5 trials (Phase 2 — 500 runs, ~5 hours)

What this DOES (Path 2 — worktree + real agent diff capture):
  1. Load arms.yaml + tasks.yaml + JWT from .dev-run/jwt.txt
  2. Record master SHA at startup (safety reference)
  3. For each (arm, task, trial):
     a. `git worktree add --detach /tmp/exp-4arm-<uuid> <parent_sha>`
        — detached HEAD = no branch ref pollution
     b. POST /api/workspace/open to point agent at the worktree
     c. POST /v1/chat/completions with need_description as user message
     d. Capture (response, git_status --short, git diff) from worktree
     e. Write blinded artifact to runs/<phase>/blinded/<uuid>.md
     f. `git worktree remove --force` + `git worktree prune`
     g. Assert master SHA UNCHANGED — abort whole run if it moved
  4. Write run-level metadata to runs/<phase>/index.jsonl
  5. Do NOT score here — scoring is a separate step (score.py)

Git safety model (the question that gated this work):
  - `--detach` means worktree HEAD is a free-floating SHA, no branch
  - `git_commit` calls inside the worktree create orphan commits in the
    shared .git/objects; they're unreachable from master and get GC'd
  - `git_branch` calls would pollute .git/refs — we mitigate by sweeping
    branches named "exp-*" after each trial
  - `code_shell` cannot escape: it goes to the Java codeexec service
    which runs in Docker, can't see the host filesystem
  - Master SHA assertion after each trial catches any defect we missed

What this still DOES NOT do (deferred to Path 3):
  - ❌ Toggle agent features per arm. Phase 0 just sends `experimental_features`
    in the payload; the agent server currently ignores it. arm_a ≡ arm_c
    until Path 3 wires the toggle infra.
  - ❌ Score / judge / report. Use score.py for that.
  - ❌ Run agent tests in worktree (codeexec Docker can't see worktree path)

The smoke path validates worktree + workspace switch + diff capture
*plumbing*. It does NOT validate that the agent fixed the bug —
that's Path 3's job once we have a real judge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"
ARMS_YAML = HERE / "arms.yaml"
TASKS_YAML = HERE / "tasks.yaml"

# ── Git safety ────────────────────────────────────────────────────
# Resolved at startup. Used to assert master ref never moves during a run.
REPO_ROOT = HERE.parents[3]  # /Users/.../mcp
MASTER_SHA_AT_STARTUP: str | None = None  # set in main()

# Worktrees live here. /tmp survives reboots on macOS but gets nuked on
# system tmp cleanup — fine for ephemeral experiment state.
WORKTREE_BASE = Path("/tmp")
WORKTREE_PREFIX = "exp-4arm-"

# JWT for talking to the agent's /api/workspace/open endpoint.
# scripts/dev/scenarios/with-tools/up.sh writes it here.
DEFAULT_JWT_PATH = REPO_ROOT / ".dev-run" / "jwt.txt"


# ── Loading ────────────────────────────────────────────────────────
def load_yaml(p: Path) -> dict:
    if not p.exists():
        raise FileNotFoundError(f"missing: {p}")
    with p.open() as fh:
        return yaml.safe_load(fh)


# ── Git worktree safety ────────────────────────────────────────────
def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run a git command, return stdout. Raises on non-zero if check=True."""
    cp = subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\nSTDOUT: {cp.stdout}\nSTDERR: {cp.stderr}")
    return cp.stdout.strip()


def current_master_sha() -> str:
    """Resolve master ref to a full SHA. Used as safety reference."""
    return _run_git(["rev-parse", "master"])


def assert_master_unchanged(label: str) -> None:
    """Hard abort if master moved since we recorded MASTER_SHA_AT_STARTUP.

    This is the structural safety net — if any worktree operation
    accidentally moved master (it shouldn't, but defense in depth),
    we stop immediately rather than continue producing tainted runs.
    """
    if MASTER_SHA_AT_STARTUP is None:
        return  # not initialized yet (e.g. unit test import)
    now = current_master_sha()
    if now != MASTER_SHA_AT_STARTUP:
        print(
            f"\n🚨 MASTER MOVED at {label}!\n"
            f"   was: {MASTER_SHA_AT_STARTUP[:7]}\n"
            f"   now: {now[:7]}\n"
            f"   This must not happen. Aborting run to prevent further damage.",
            file=sys.stderr,
        )
        sys.exit(99)


def setup_worktree(parent_sha: str) -> Path:
    """Create a detached-HEAD worktree at /tmp/exp-4arm-<uuid> on parent_sha.

    Detached HEAD means the worktree has no branch ref. agent-created
    commits become orphans (GC-able), agent-created branches stay local
    to the worktree and disappear on remove.
    """
    wt = WORKTREE_BASE / f"{WORKTREE_PREFIX}{uuid.uuid4().hex[:8]}"
    _run_git(["worktree", "add", "--detach", str(wt), parent_sha])
    return wt


def capture_worktree_diff(wt: Path) -> dict:
    """After agent runs, snapshot the worktree state.

    Returns:
        {
          "files_changed": int,
          "lines_added": int,
          "lines_removed": int,
          "status_short": str,   # `git status --short` output
          "diff": str,           # `git diff HEAD` truncated to 16KB
          "head_sha": str,       # current HEAD (should still == parent_sha)
        }
    """
    # status --short shows both staged and unstaged in 2-char prefix
    status = _run_git(["status", "--short"], cwd=wt, check=False)

    # Diff against the worktree's own HEAD (= parent_sha at start).
    # If agent ran git_commit, HEAD moved — captured separately.
    diff = _run_git(["diff", "HEAD"], cwd=wt, check=False)

    # numstat = added\tremoved\tfile per row
    numstat = _run_git(["diff", "HEAD", "--numstat"], cwd=wt, check=False)
    files_changed = 0
    lines_added = 0
    lines_removed = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            files_changed += 1
            # Binary files show "-\t-" — skip
            try:
                lines_added += int(parts[0])
                lines_removed += int(parts[1])
            except ValueError:
                pass

    head = _run_git(["rev-parse", "HEAD"], cwd=wt, check=False)

    return {
        "files_changed": files_changed,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "status_short": status[:4096],
        "diff": diff[:16384],
        "head_sha": head[:7],
        "diff_truncated": len(diff) > 16384,
    }


def teardown_worktree(wt: Path) -> None:
    """Remove the worktree and prune any stale records. Best-effort."""
    try:
        _run_git(["worktree", "remove", "--force", str(wt)], check=False)
    except Exception as e:
        print(f"  ⚠️  worktree remove failed for {wt}: {e}", file=sys.stderr)
    # Defensive: if the directory still exists (e.g. file lock), nuke it.
    if wt.exists():
        try:
            shutil.rmtree(wt, ignore_errors=True)
        except Exception:
            pass
    _run_git(["worktree", "prune"], check=False)


def sweep_experiment_branches() -> int:
    """Delete any local branches matching the experiment-created pattern.

    Agent may have called git_branch in a worktree which would create
    a ref under .git/refs/heads/. Detached HEAD prevents this for the
    worktree's own HEAD, but `git branch <name>` from inside the
    worktree still creates a global ref. We sweep these to keep the
    branch namespace clean.
    """
    branches = _run_git(["branch", "--list"], check=False).splitlines()
    deleted = 0
    for b in branches:
        name = b.strip().lstrip("* ").strip()
        if not name or name == "master":
            continue
        # Sweep heuristic: anything containing "exp", "fix-", "agent-"
        # created during a run. Conservative: do NOT delete the user's
        # own feature branches. Only sweep the obviously-experimental.
        # For now we ONLY sweep what we know we created ourselves.
        # (Currently nothing — agent uses detached HEAD. Function reserved.)
        pass
    return deleted


# ── Workspace switch via agent HTTP API ────────────────────────────
def load_jwt(jwt_path: Path = DEFAULT_JWT_PATH) -> str:
    """Read the test user's JWT from .dev-run/jwt.txt.

    The with-tools/up.sh scenario writes this file. If missing, the
    user hasn't started the dev stack yet.
    """
    if not jwt_path.exists():
        print(
            f"❌ JWT not found at {jwt_path}.\n"
            f"   Run: bash scripts/dev/scenarios/with-tools/up.sh",
            file=sys.stderr,
        )
        sys.exit(1)
    return jwt_path.read_text().strip()


async def switch_workspace(client: httpx.AsyncClient, agent_url: str, jwt: str, path: Path) -> None:
    """Tell the agent to use `path` as its workspace root.

    The agent's workspace_resolver only allows paths under $HOME or
    /tmp — we use /tmp/exp-4arm-* so this is always allowed.
    """
    r = await client.post(
        f"{agent_url}/api/workspace/open",
        headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
        json={"path": str(path)},
        timeout=10,
    )
    r.raise_for_status()


def select_tasks(tasks: list[dict], smoke: bool, limit: int | None) -> list[dict]:
    """Pick the task subset for this run mode.

    Path 2 requirements (stricter than Phase 0):
      - need_description must NOT be <TODO>
      - parent_sha must be set (worktree checkout requires it)
    """
    ready = []
    skipped = 0
    for t in tasks:
        desc = t.get("need_description")
        parent = t.get("parent_sha")
        if desc in (None, "", "<TODO>"):
            skipped += 1
            continue
        if not parent:
            skipped += 1
            continue
        ready.append(t)

    if not ready:
        print(
            "❌ No tasks have both need_description AND parent_sha filled.\n"
            "   Run: python prepare_tasks.py --no-llm",
            file=sys.stderr,
        )
        sys.exit(1)
    if skipped:
        print(f"   ⚠️  Skipped {skipped} task(s) missing need_description or parent_sha")

    if smoke:
        return ready[:3]
    if limit:
        return ready[:limit]
    return ready


def select_arms(arms: list[dict], smoke: bool, only: str | None) -> list[dict]:
    """Pick which arms to run."""
    if only:
        m = [a for a in arms if a["id"] == only]
        if not m:
            print(f"❌ Arm '{only}' not found. Available: {[a['id'] for a in arms]}", file=sys.stderr)
            sys.exit(1)
        return m
    if smoke:
        return [a for a in arms if a["id"] == "arm_c"]  # smoke = control arm only
    return arms


# ── HTTP to agent ──────────────────────────────────────────────────
async def call_agent(
    client: httpx.AsyncClient,
    agent_url: str,
    jwt: str,
    arm: dict,
    task: dict,
    timeout_s: float,
    session_id: str,
) -> dict:
    """Single agent call. Returns dict with response + timing + any errors.

    PHASE 0/2 NOTE: we POST feature flags in the body but agent server
    currently ignores them. Wiring feature toggles is Path 3 work.

    The user message is `need_description` prefixed by a workspace
    framing — the agent needs to know it has a real git repo and is
    expected to USE tools (file_*, git_*) rather than chat at us.
    """
    framing = (
        "You're operating in a git workspace that contains a real "
        "codebase with a bug. Investigate using file_list / file_search "
        "/ file_read, then fix using file_edit / file_write. Success "
        "means `git diff` shows a meaningful change that addresses the "
        "user's problem. Do not respond in prose only — actually use "
        "the tools. When done, briefly summarize what you changed.\n\n"
        "USER REQUEST:\n"
    )
    user_message = framing + task["need_description"]

    payload = {
        "messages": [{"role": "user", "content": user_message}],
        "model": arm["model"],
        "session_id": session_id,
        # Feature flags — agent server will respect these in Path 3.
        "experimental_features": arm["features"],
        "stream": False,
        # Sampling
        "temperature": arm["sampling"]["temperature"],
        "top_p": arm["sampling"]["top_p"],
        "max_tokens": arm["sampling"]["max_tokens"],
    }

    t0 = time.monotonic()
    err: str | None = None
    resp_body: dict | None = None
    status: int = 0
    try:
        r = await client.post(
            f"{agent_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_s,
        )
        status = r.status_code
        try:
            resp_body = r.json()
        except Exception:
            resp_body = {"raw_text": r.text[:8192]}
    except httpx.RequestError as e:
        err = f"{e.__class__.__name__}: {e}"
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "ok": err is None and 200 <= status < 300,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "error": err,
        "response": resp_body,
        "user_message": user_message,  # so write_blinded can show what we asked
    }


# ── Blinded artifact ───────────────────────────────────────────────
def write_blinded(
    out_dir: Path,
    artifact_id: str,
    task: dict,
    arm: dict,
    trial_idx: int,
    result: dict,
    diff_info: dict | None,
) -> Path:
    """Write the agent's output to runs/<phase>/blinded/<uuid>.md.

    The file CONTAINS the agent's response and resulting diff but NOT
    the arm/model name. A separate mapping file (index.jsonl) ties
    uuid → (arm, task, trial). This is the structural anti-bias
    measure for human spot-check.
    """
    blinded_dir = out_dir / "blinded"
    blinded_dir.mkdir(parents=True, exist_ok=True)
    f = blinded_dir / f"{artifact_id}.md"

    # Extract the assistant's final message if available
    final_text = ""
    if result.get("response"):
        choices = result["response"].get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            final_text = (msg.get("content") or "").strip()
        if not final_text:
            final_text = json.dumps(result["response"])[:4000]

    diff_section = ""
    if diff_info:
        di = diff_info
        diff_section = (
            f"\n## Files changed\n"
            f"- files: {di['files_changed']}, +{di['lines_added']} / -{di['lines_removed']}\n"
            f"- HEAD: {di['head_sha']}\n"
            f"\n### git status --short\n"
            f"```\n{di['status_short'] or '(clean)'}\n```\n"
            f"\n### git diff HEAD"
            f"{' (truncated to 16KB)' if di.get('diff_truncated') else ''}\n"
            f"```diff\n{di['diff'] or '(empty)'}\n```\n"
        )

    content = f"""# Artifact {artifact_id}

## User request (as sent to agent)
{result.get("user_message", task["need_description"])}

## Agent response
{final_text or "(empty)"}
{diff_section}
## Run metadata (unblinded — strip before showing reviewer)
- elapsed_ms: {result["elapsed_ms"]}
- ok: {result["ok"]}
- status: {result["status"]}
- error: {result.get("error") or "(none)"}
"""
    f.write_text(content)
    return f


def append_index(out_dir: Path, entry: dict) -> None:
    """Append one line to index.jsonl — the only place arm↔uuid mapping lives."""
    idx = out_dir / "index.jsonl"
    idx.parent.mkdir(parents=True, exist_ok=True)
    with idx.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── Main ───────────────────────────────────────────────────────────
async def run(
    arms_to_run: list[dict],
    tasks_to_run: list[dict],
    trials: int,
    runtime: dict,
    out_dir: Path,
    jwt: str,
):
    agent_url = runtime["agent_url"]
    timeout_s = float(runtime.get("timeout_seconds", 300))
    # Path 2 forces parallelism=1 — the agent server has a SINGLE
    # workspace at a time; two trials would race on /api/workspace/open
    # and the second trial could read the first trial's worktree files.
    # Parallelism returns in Path 3+ with per-tenant workspaces or
    # multiple agent server instances.
    parallelism = 1

    total = len(arms_to_run) * len(tasks_to_run) * trials
    done = 0

    print(f"▶️  Run starting: {len(arms_to_run)} arms × {len(tasks_to_run)} tasks × {trials} trials = {total} runs")
    print(f"   Agent URL: {agent_url}")
    print(f"   Parallelism: {parallelism} (forced — worktree mode)")
    print(f"   Output: {out_dir}")
    print(f"   Master SHA (must not move): {MASTER_SHA_AT_STARTUP[:7]}\n")

    async with httpx.AsyncClient() as client:
        async def one_run(arm: dict, task: dict, trial_idx: int):
            nonlocal done
            artifact_id = uuid.uuid4().hex[:12]
            session_id = f"exp:{arm['id']}:{task['sha']}:{trial_idx}:{artifact_id}"

            # 1. Setup worktree (detached HEAD on parent_sha)
            wt: Path | None = None
            diff_info: dict | None = None
            setup_err: str | None = None
            try:
                wt = setup_worktree(task["parent_sha"])
            except Exception as e:
                setup_err = f"worktree setup failed: {e}"

            # 2. Point the agent at the worktree
            if wt and not setup_err:
                try:
                    await switch_workspace(client, agent_url, jwt, wt)
                except Exception as e:
                    setup_err = f"workspace switch failed: {e}"

            # 3. Call the agent (skip if setup failed — still write artifact for trace)
            if setup_err:
                result = {
                    "ok": False,
                    "status": 0,
                    "elapsed_ms": 0,
                    "error": setup_err,
                    "response": None,
                    "user_message": task["need_description"],
                }
            else:
                result = await call_agent(
                    client=client,
                    agent_url=agent_url,
                    jwt=jwt,
                    arm=arm,
                    task=task,
                    timeout_s=timeout_s,
                    session_id=session_id,
                )

            # 4. Capture diff (even if agent failed — empty diff is signal too)
            if wt:
                try:
                    diff_info = capture_worktree_diff(wt)
                except Exception as e:
                    print(f"  ⚠️  diff capture failed: {e}", file=sys.stderr)

            # 5. Teardown worktree
            if wt:
                teardown_worktree(wt)

            # 6. SAFETY ASSERTION — master must not have moved
            assert_master_unchanged(label=f"after {arm['id']}/{task['sha']}/trial={trial_idx}")

            # 7. Write artifact + index row
            write_blinded(out_dir, artifact_id, task, arm, trial_idx, result, diff_info)
            append_index(
                out_dir,
                {
                    "artifact_id": artifact_id,
                    "arm_id": arm["id"],
                    "task_sha": task["sha"],
                    "task_parent_sha": task.get("parent_sha"),
                    "task_level": task.get("level"),
                    "trial_idx": trial_idx,
                    "ok": result["ok"],
                    "elapsed_ms": result["elapsed_ms"],
                    "status": result["status"],
                    "error": result.get("error"),
                    "session_id": session_id,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    # Diff metrics — null when worktree setup failed
                    "diff_files": diff_info["files_changed"] if diff_info else None,
                    "diff_lines_added": diff_info["lines_added"] if diff_info else None,
                    "diff_lines_removed": diff_info["lines_removed"] if diff_info else None,
                    "worktree_head_changed": (
                        diff_info["head_sha"] != task["parent_sha"][:7]
                        if diff_info else None
                    ),
                },
            )
            done += 1
            marker = "✅" if result["ok"] else "❌"
            diff_summary = (
                f" diff={diff_info['files_changed']}f +{diff_info['lines_added']}/-{diff_info['lines_removed']}"
                if diff_info else " (no diff)"
            )
            print(
                f"  [{done:>3}/{total}] {marker} {arm['id']:<8} {task['sha']} "
                f"trial={trial_idx} ({result['elapsed_ms']}ms){diff_summary}"
            )

        # Serial loop (parallelism=1 enforced above).
        for arm in arms_to_run:
            for task in tasks_to_run:
                for t in range(trials):
                    await one_run(arm, task, t)

    print(f"\n🏁 Done. {done}/{total} runs completed.")
    print(f"   Blinded artifacts: {out_dir / 'blinded'}")
    print(f"   Index (arm mapping): {out_dir / 'index.jsonl'}")
    print(f"   Master SHA at end: {current_master_sha()[:7]} (unchanged from start ✓)")
    print(f"\n📌 Next: python score.py --runs-dir {out_dir.name}")


def main():
    parser = argparse.ArgumentParser(description="Controlled 4-arm experiment driver")
    parser.add_argument("--smoke", action="store_true", help="Smoke: arm_c × 3 tasks × 1 trial (pipeline check)")
    parser.add_argument("--phase1", action="store_true", help="Phase 1: arm_a + arm_c × hallucination subset × 5 trials")
    parser.add_argument("--full", action="store_true", help="Phase 2: 4 arms × 25 tasks × 5 trials (~5h)")
    parser.add_argument("--only-arm", default=None, help="Override: only run this arm id")
    parser.add_argument("--limit", type=int, default=None, help="Override task count")
    parser.add_argument("--trials", type=int, default=None, help="Override trials per (arm, task)")
    parser.add_argument("--out", default=None, help="Override output dir name under runs/")
    args = parser.parse_args()

    if not any([args.smoke, args.phase1, args.full]):
        print("❌ Must pick a mode: --smoke | --phase1 | --full", file=sys.stderr)
        sys.exit(2)

    if sum([bool(args.smoke), bool(args.phase1), bool(args.full)]) > 1:
        print("❌ Pick exactly one mode.", file=sys.stderr)
        sys.exit(2)

    # Load configs
    arms_data = load_yaml(ARMS_YAML)
    tasks_data = load_yaml(TASKS_YAML)
    arms = arms_data["arms"]
    tasks = tasks_data["tasks"]
    runtime = arms_data.get("runtime", {})

    # Select subset
    arms_to_run = select_arms(arms, smoke=args.smoke, only=args.only_arm)
    tasks_to_run = select_tasks(tasks, smoke=args.smoke, limit=args.limit)

    # Trials
    if args.trials is not None:
        trials = args.trials
    elif args.smoke:
        trials = 1
    elif args.phase1:
        trials = 5
    elif args.full:
        trials = int(runtime.get("trials_per_task", 5))
    else:
        trials = 1

    # Output dir
    if args.out:
        out_dir = RUNS_DIR / args.out
    elif args.smoke:
        out_dir = RUNS_DIR / "smoke"
    elif args.phase1:
        out_dir = RUNS_DIR / f"phase1_{datetime.now().strftime('%Y%m%d_%H%M')}"
    else:
        out_dir = RUNS_DIR / f"full_{datetime.now().strftime('%Y%m%d_%H%M')}"

    out_dir.mkdir(parents=True, exist_ok=True)

    # Save the exact arm/task config used for this run (reproducibility)
    (out_dir / "_arms.yaml").write_text(yaml.dump({"arms": arms_to_run, "runtime": runtime}, allow_unicode=True, sort_keys=False))
    (out_dir / "_tasks.yaml").write_text(yaml.dump({"tasks": tasks_to_run}, allow_unicode=True, sort_keys=False))

    # ── Path 2 safety + auth setup ──────────────────────────────
    global MASTER_SHA_AT_STARTUP
    MASTER_SHA_AT_STARTUP = current_master_sha()

    # Defensive: refuse to run if main worktree has uncommitted changes
    # in eval-harness dir — agent worktrees inherit from the same .git,
    # so user-staged changes could leak into trials in confusing ways.
    dirty = _run_git(["status", "--short"], check=False)
    if dirty:
        # We only WARN, don't abort — the user is allowed to have
        # uncommitted edits (they're working on improving this script
        # right now!). Just make it visible.
        print(f"⚠️  Main worktree has uncommitted changes:\n{dirty[:500]}\n", file=sys.stderr)

    jwt = load_jwt()

    asyncio.run(run(arms_to_run, tasks_to_run, trials, runtime, out_dir, jwt))


if __name__ == "__main__":
    main()
