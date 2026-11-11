#!/usr/bin/env bash
# Build a standalone nanocode engine (no Python needed on the target Mac) for the desktop app.
# Output: packaging/dist/nanocode-engine/nanocode-engine  ->  bundled as Resources/engine/ in nanocode.app
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
"$PY" -m pip show pyinstaller >/dev/null 2>&1 || "$PY" -m pip install pyinstaller
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --name nanocode-engine --onedir \
  --distpath packaging/dist --workpath packaging/work --specpath packaging/work \
  --collect-submodules nanocode \
  packaging/engine.py
echo "engine built: packaging/dist/nanocode-engine/nanocode-engine"
