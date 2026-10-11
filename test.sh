#!/usr/bin/env bash
# Run the complete GDI test suite from any working directory.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$project_dir"
export PYTHONDONTWRITEBYTECODE=1
if [[ -z "${GDI_TEST_ACT:-}" ]] && command -v act >/dev/null; then
    export GDI_TEST_ACT="$(command -v act)"
fi
exec python3 -m unittest discover -s tests -v "$@"
