#!/usr/bin/env bash
# One-time setup for zotero-word-cite: create the virtualenv and install deps.
set -euo pipefail

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"

echo "==> Creating virtualenv (.venv) with: $($PYTHON --version 2>&1)"
"$PYTHON" -m venv .venv

echo "==> Installing dependencies"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo "==> Verifying the package imports"
./.venv/bin/python -c "import zoterocite; print('    zoterocite OK')"

cat <<'EOF'

Setup complete. Next steps:

  1. Copy the example config and add your Zotero credentials:
        cp .env.example .env
        # then edit .env: set ZOTERO_API_KEY and ZOTERO_GROUP_ID

  2. Check your environment:
        ./scripts/zwc init

  3. List available citation styles (offline):
        ./scripts/zwc csl --list

See README.md for how to get a Zotero API key and find your group ID.
EOF
