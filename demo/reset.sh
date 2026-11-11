#!/usr/bin/env bash
# Restore the demo project to its original (buggy) state. Run before every recording take.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf shop habits
cp -R .pristine/shop shop
echo "demo reset: demo/shop has 4 failing tests again"
