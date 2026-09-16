#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
environment_directory="$project_root/.venv"
environment_python="$environment_directory/bin/python"
lock_file="$project_root/requirements.lock.txt"

for required in finresearch resources web config.json requirements.lock.txt; do
    if [[ ! -e "$project_root/$required" ]]; then
        printf 'A complete source checkout is required; missing %s.\n' "$required" >&2
        exit 1
    fi
done

existing_environment=false
if [[ -e "$environment_directory" || -L "$environment_directory" ]]; then
    existing_environment=true
    if [[ ! -x "$environment_python" ]]; then
        printf '%s\n' 'The existing .venv is not a Linux virtual environment. It has not been changed.' >&2
        exit 1
    fi
else
    if ! command -v python3.12 >/dev/null 2>&1; then
        printf '%s\n' 'Install Python 3.12 and its venv package, then run this script again.' >&2
        exit 1
    fi
    python3.12 -m venv "$environment_directory"
fi

"$environment_python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"; assert sys.prefix != sys.base_prefix, "A virtual environment is required"'

if [[ "$existing_environment" == false ]]; then
    "$environment_python" -m pip install -r "$lock_file"
fi

# Never install, upgrade, or remove packages from an already existing environment.
"$environment_python" - "$lock_file" <<'PY'
import importlib.metadata
import pathlib
import sys

failures = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    package, expected = line.split("==", 1)
    try:
        installed = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        installed = "missing"
    if installed != expected:
        failures.append(f"{package}: expected {expected}, found {installed}")
if failures:
    raise SystemExit("Existing environment does not match requirements.lock.txt; no packages were changed.\n" + "\n".join(failures))
print("All locked runtime dependencies match.")
PY
"$environment_python" -m pip check

printf '%s\n' \
    'Setup complete. No local configuration or credentials were read or written.' \
    'From the project root, start the local workbench with:' \
    '  .venv/bin/python -m finresearch serve --port 8765'
