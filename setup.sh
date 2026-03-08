#!/bin/bash
# DeerFlow OAuth Bridge — One-time setup

set -euo pipefail

echo "🦌 DeerFlow OAuth Bridge Setup"
echo "================================"

# Check Python
python3 --version >/dev/null 2>&1 || { echo "❌ Python 3 is required"; exit 1; }

# Create venv and install deps
echo "📦 Installing dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet fastapi uvicorn httpx

# First run — triggers OAuth login
echo ""
echo "🔐 Starting OAuth login..."
echo "A browser window will open. Sign in with your ChatGPT account."
echo ""
python - <<'PY'
from oauth import login, load_credentials

login()
creds = load_credentials() or {}
expires = creds.get("expires", "unknown")
print(f"✅ Authenticated! Token expires: {expires}")
PY

echo ""
echo "✅ Setup complete!"
echo ""
echo "To start the bridge:"
echo " cd $(pwd)"
echo " source venv/bin/activate"
echo " python server.py"
echo ""
echo "Then configure DeerFlow's config.yaml with:"
echo " base_url: http://host.docker.internal:8462/v1"
echo " api_key: \"not-needed\""
echo ""
