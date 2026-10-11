#!/usr/bin/env bash
# Publish the committed GDI sources and the matching cloud-agent runtime.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
output_dir="$project_dir/dist/runtime"
runtime_dir="gdrive:gdi/gdi/runtime"
build_only=false
while (($#)); do
    case "$1" in
        --build-only) build_only=true; shift ;;
        --output|--runtime-dir)
            if (($# < 2)) || [[ -z "$2" ]]; then
                echo "deploy: $1 requires a value" >&2
                exit 2
            fi
            if [[ "$1" == --output ]]; then output_dir="$2"; else runtime_dir="$2"; fi
            shift 2 ;;
        --help)
            echo 'Usage: scripts/deploy.sh [--build-only] [--output DIR] [--runtime-dir RCLONE_PATH]'
            exit 0 ;;
        *) echo "deploy: unknown argument: $1" >&2; exit 2 ;;
    esac
done
for executable in git python3 cmp; do command -v "$executable" >/dev/null; done
if ! "$build_only"; then command -v rclone >/dev/null; fi
unset PYTHONPATH PYTHONHOME
export PYTHONDONTWRITEBYTECODE=1
output_dir="$(python3 -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$output_dir")"
stage='build'
trap 'echo "deploy: failed during $stage; runtime files remain in $output_dir" >&2' ERR
echo 'deploy: building the committed runtime'
python3 -I "$project_dir/scripts/build_runtime.py" --repo "$project_dir" --output "$output_dir"

verification_dir="$(mktemp -d -t gdi-deploy-XXXXXXXX)"
trap 'rm -rf -- "$verification_dir"' EXIT
stage='runtime smoke checks'
# -I and a separate working directory ensure imports come from the zipapp.
(
    cd "$verification_dir"
    export XDG_CONFIG_HOME="$verification_dir/config"
    python3 -I "$output_dir/gdi.pyz" --help >/dev/null
    python3 -I "$output_dir/gdi.pyz" --version >/dev/null
    python3 -I "$output_dir/gdi.pyz" agent snapshot --help >/dev/null
    python3 -I "$output_dir/gdi.pyz" agent clone --help >/dev/null
)
if "$build_only"; then
    echo "deploy: build verified: $output_dir/gdi.pyz (no remote changes)"
    exit 0
fi

cd "$project_dir"
stage='source check'
python3 -I - "$output_dir/runtime.json" <<'PY'
import json, os, subprocess, sys
metadata = json.load(open(sys.argv[1]))
environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
def git(*args):
    return subprocess.check_output(['git', *args], env=environment).decode().strip()
if (git('status', '--porcelain=v1', '--untracked-files=all') or git('rev-parse', 'HEAD') != metadata['head']
        or git('symbolic-ref', 'HEAD') != metadata['ref']):
    raise SystemExit('source changed since runtime build; no publication attempted')
PY
stage='full GDI publication'
echo 'deploy: publishing a full checkpoint'
python3 -m gdi push drive --full --json > "$verification_dir/publication.json"
stage='publication check'
python3 -I - "$output_dir/runtime.json" "$verification_dir/publication.json" <<'PY'
import json, sys
metadata, publication = (json.load(open(path)) for path in sys.argv[1:])
if (publication['head'] != metadata['head'] or 'refs/heads/' + publication['branch'] != metadata['ref']
        or publication['bundle_kind'] != 'full'):
    raise SystemExit('full publication does not match the built runtime; runtime was not uploaded')
print('deploy: published ' + publication['head'] + ' (' + publication['publication_id'] + ')')
PY
stage='runtime upload'
echo "deploy: uploading runtime to $runtime_dir"
for filename in gdi.pyz runtime.json gdi.pyz.sha256; do
    rclone copyto "$output_dir/$filename" "${runtime_dir%/}/$filename"
done
stage='runtime download verification'
for filename in gdi.pyz runtime.json gdi.pyz.sha256; do
    rclone copyto "${runtime_dir%/}/$filename" "$verification_dir/$filename"
    cmp -- "$output_dir/$filename" "$verification_dir/$filename"
done
echo "deploy: complete; uploaded files verified at $runtime_dir"
