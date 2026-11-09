#!/usr/bin/env bash
# publish.sh - Publishes the HTML report to a GitHub Pages repository (e.g. zeuz.github.io).
#
# Copies the report to <REPO_DIR>/<SUBDIR>/index.html, commits it and pushes it.
# Settings are read from publish.conf next to this script (another file can be given
# with the PUBLISH_CONF environment variable). gen-rep.sh calls it after each run.
#
# Usage: ./publish.sh [report.html]     (default: report.html in the project folder)
# Exit codes: 0 = published, nothing to publish, or publishing disabled; 1 = error.

set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${PUBLISH_CONF:-$DIR/publish.conf}"
REPORT="${1:-$DIR/report.html}"

say() { printf '[publish] %s\n' "$*"; }
fail() { say "ERROR: $*"; exit 1; }

# Defaults (publish.conf overrides them).
ENABLED=false
REPO_DIR=output              # relative paths are relative to this script's folder
SUBDIR=traffic
BRANCH=main
REMOTE=origin

if [[ -f "$CONF" ]]; then
    # shellcheck source=/dev/null
    source "$CONF" || fail "could not read $CONF"
fi

case "${ENABLED,,}" in
    true|yes|1) ;;
    *) say "disabled (ENABLED=$ENABLED in $CONF); nothing to do"; exit 0 ;;
esac

REPO_DIR="${REPO_DIR/#\~/$HOME}"
[[ -z "$REPO_DIR" || "$REPO_DIR" == /* ]] || REPO_DIR="$DIR/$REPO_DIR"
SUBDIR="${SUBDIR#/}"; SUBDIR="${SUBDIR%/}"
[[ -n "$REPO_DIR" ]] || fail "REPO_DIR is empty"
[[ -d "$REPO_DIR/.git" ]] || fail "$REPO_DIR is not a git repository (clone it first)"
[[ -f "$REPORT" ]] || fail "report not found: $REPORT"

# Never wait for a password or a host-key prompt: cron has no terminal.
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=20}"
export GIT_TERMINAL_PROMPT=0
git_() { git -C "$REPO_DIR" "$@"; }

TARGET_REL="${SUBDIR:+$SUBDIR/}index.html"
TARGET="$REPO_DIR/$TARGET_REL"

# Brings in changes made to the site from elsewhere (e.g. edits on github.com).
if git_ remote get-url "$REMOTE" >/dev/null 2>&1; then
    git_ pull --quiet --rebase --autostash "$REMOTE" "$BRANCH" || fail "git pull failed"
fi

mkdir -p "$(dirname "$TARGET")"
cp "$REPORT" "$TARGET" || fail "could not copy the report to $TARGET"

git_ add -- "$TARGET_REL"
if git_ diff --cached --quiet -- "$TARGET_REL"; then
    say "no changes in $TARGET_REL; nothing to publish"
    exit 0
fi

git_ commit --quiet -m "traffic: update report $(date '+%Y-%m-%d %H:%M')" -- "$TARGET_REL" \
    || fail "git commit failed"
git_ push --quiet "$REMOTE" "HEAD:$BRANCH" || fail "git push failed"
say "published $TARGET_REL to $REMOTE/$BRANCH"
