#!/usr/bin/env bash
#
# rebuild-history.sh — collapse this repo into a single clean "showcase snapshot"
# commit (strategy A2), preserving the working tree exactly as it is on disk.
#
# WHAT THIS DOES
#   1. Safety: refuses to run unless the tracked working tree is clean-staged
#      (you already staged the cleanup diff; this script commits THAT state).
#   2. Backup: creates a local branch + tag pointing at the current HEAD so the
#      old 18-commit history is fully recoverable.
#   3. Rebuild: creates an orphan branch, stages everything, and makes ONE root
#      commit using the message in .git/COMMIT_EDITMSG.
#   4. STOPS. It does NOT push. It prints the exact force-push command for you
#      to run by hand (per project Rule 1 — the human performs the push).
#
# WHAT THIS DOES NOT DO
#   - No `git push` / `--force`. Ever. You do that step yourself.
#   - No network calls at all.
#
# ROLLBACK (if anything looks wrong AFTER running, BEFORE you force-push):
#   git checkout main                       # or: git checkout "$BACKUP_BRANCH"
#   git branch -f main "$BACKUP_BRANCH"     # restore main to the old history
#   (Nothing was pushed, so origin is untouched until you push.)
#
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

TARGET_BRANCH="main"                       # branch we want the snapshot to live on
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_BRANCH="backup-pre-rebuild-${STAMP}"
BACKUP_TAG="pre-rebuild-${STAMP}"
MSG_FILE="${REPO_ROOT}/.git/COMMIT_EDITMSG"
TMP_ORPHAN="__snapshot_orphan_${STAMP}"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Preflight checks ──────────────────────────────────────────────────────────
say "Preflight checks"

[ -f "$MSG_FILE" ] || die "Commit message file not found: $MSG_FILE"
[ -s "$MSG_FILE" ] || die "Commit message file is empty: $MSG_FILE"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$TARGET_BRANCH" ]; then
  die "Expected to be on '$TARGET_BRANCH' but on '$CURRENT_BRANCH'. Aborting."
fi

# There must be NO unstaged changes to tracked files. Staged changes are fine
# (that's the cleanup you already reviewed). Untracked files (like this script)
# are fine too — they'll be included in the snapshot.
if ! git diff --quiet; then
  die "You have UNSTAGED changes to tracked files. Stage or discard them first:
     git add -u    (to include them)   OR   git checkout -- <file>   (to drop them)"
fi

ORIG_HEAD="$(git rev-parse HEAD)"
ORIG_SHORT="$(git rev-parse --short HEAD)"
say "Current HEAD: $ORIG_SHORT  ($(git rev-list --count HEAD) commits total)"

# ── Backup ────────────────────────────────────────────────────────────────────
say "Creating safety backup"
git branch "$BACKUP_BRANCH" "$ORIG_HEAD"
git tag    "$BACKUP_TAG"    "$ORIG_HEAD"
ok "Backup branch: $BACKUP_BRANCH"
ok "Backup tag:    $BACKUP_TAG"

# ── Rebuild as a single orphan commit ─────────────────────────────────────────
say "Building orphan snapshot commit"

# Orphan branch has no parent — this becomes a true root commit.
git checkout --orphan "$TMP_ORPHAN"

# Stage the ENTIRE working tree (respects .gitignore). This captures the exact
# on-disk state, including the already-staged cleanup and any untracked files.
git add -A

# Commit using the reviewed message. --no-verify is deliberately NOT used;
# if a pre-commit hook exists it should run. If you WANT to skip hooks, do it
# yourself, consciously.
git commit -F "$MSG_FILE"

NEW_SHORT="$(git rev-parse --short HEAD)"
ok "Snapshot commit created: $NEW_SHORT"

# ── Swap the orphan branch into place as $TARGET_BRANCH ───────────────────────
say "Repointing '$TARGET_BRANCH' to the snapshot"
# Delete the old branch ref and rename the orphan to take its place. The old
# commits are still reachable via $BACKUP_BRANCH / $BACKUP_TAG.
git branch -D "$TARGET_BRANCH"
git branch -m "$TARGET_BRANCH"
ok "'$TARGET_BRANCH' now points at $NEW_SHORT (single root commit)"

# ── Summary + next step (NOT executed) ────────────────────────────────────────
echo
ok "Local rebuild complete. Nothing has been pushed."
echo
say "Verify the result:"
echo "    git log --oneline                 # should show exactly ONE commit"
echo "    git show --stat HEAD | head -40    # inspect the snapshot"
echo "    git status                         # should be clean"
echo
warn "Origin still has the OLD history until YOU force-push."
echo
say "When you're satisfied, push it yourself (this script will NOT):"
echo
echo "    git push --force-with-lease origin ${TARGET_BRANCH}"
echo
warn "Prefer --force-with-lease over --force: it refuses to overwrite if origin"
warn "moved unexpectedly. Since this is a solo repo, either works, but lease is safer."
echo
say "If something looks wrong BEFORE pushing, roll back with:"
echo "    git branch -f ${TARGET_BRANCH} ${BACKUP_BRANCH} && git checkout ${TARGET_BRANCH}"
echo
say "After a successful push, clean up backups whenever you're ready:"
echo "    git branch -D ${BACKUP_BRANCH}"
echo "    git tag -d ${BACKUP_TAG}"
echo