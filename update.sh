#!/usr/bin/env bash
# Updates this folder to the latest version from GitHub (branch claude/elegant-bell-qeagk3).
# Run by update.bat, or directly in Git Bash:  ./update.sh
# Keeps your .env, kalshi.key and trades.jsonl. Throws away any other edits you made to the code.
# Everything is inside main() so bash reads the whole file before `git reset` rewrites it.
main() {
    set -e
    cd "$(dirname "$0")"
    local branch=claude/elegant-bell-qeagk3
    echo "Getting the latest changes from $branch ..."
    git fetch origin "$branch"
    git checkout -q -f main
    git reset -q --hard FETCH_HEAD
    git log -1 --format="Up to date: %h %s (%cr)"
    echo "Next: run-tests.bat, then start-dashboard.bat."
}
main "$@"
exit
