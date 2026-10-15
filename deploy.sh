#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
    echo 'deploy: commit all changes before deploying' >&2
    exit 1
fi

git push
python3 -m gdi push drive --full
exec python3 scripts/deploy.py
