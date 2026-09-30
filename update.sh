#!/usr/bin/env bash
# Updates this folder to the latest version from GitHub (branch claude/elegant-bell-qeagk3).
# Run it in the terminal where `git` works (Git Bash, VS Code terminal):  ./update.sh
# Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
set -e
cd "$(dirname "$0")"
BRANCH=claude/elegant-bell-qeagk3
echo "Getting the latest changes from $BRANCH ..."
git fetch origin "$BRANCH"
git checkout -q -f main
git reset -q --hard FETCH_HEAD
git log -1 --format="Up to date: %h %s (%cr)"
echo "Next: run-tests.bat, then start-dashboard.bat."
