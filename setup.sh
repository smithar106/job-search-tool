#!/usr/bin/env bash
# setup.sh — One-time setup script for Ashlee's job search pipeline
# Run once: bash setup.sh

set -e

echo ""
echo "=== Ashlee Thomas — Job Search Pipeline Setup ==="
echo ""

# ─── 1. Python check ──────────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install from https://python.org"
    exit 1
fi
echo "✅ Python 3 found: $(python3 --version)"

# ─── 2. pip install ───────────────────────────────────────────────────────────
echo ""
echo "Installing Python dependencies..."
pip3 install -r requirements.txt --quiet
echo "✅ Dependencies installed"

# ─── 3. OpenCLI check ─────────────────────────────────────────────────────────
echo ""
if ! command -v opencli &> /dev/null; then
    echo "Installing OpenCLI..."
    npm install -g @jackwener/opencli
    echo "✅ OpenCLI installed"
else
    echo "✅ OpenCLI found: $(opencli --version 2>/dev/null || echo 'installed')"
fi

# ─── 4. .env check ────────────────────────────────────────────────────────────
echo ""
if [ ! -f .env ]; then
    cp .env.example .env
    echo "📋 Created .env from template."
    echo ""
    echo "   ⚠️  BEFORE RUNNING THE PIPELINE, edit .env and fill in:"
    echo "      GEMINI_API_KEY   — from https://aistudio.google.com"
    echo "      SENDER_EMAIL     — the Gmail that sends the summary"
    echo "      GMAIL_APP_PASSWORD — from myaccount.google.com/security → App Passwords"
    echo "      DRIVE_FOLDER_ID  — from your Google Drive folder URL"
    echo ""
    echo "   Then run: nano .env  (or open it in any text editor)"
else
    echo "✅ .env already exists"
fi

# ─── 5. credentials.json check ───────────────────────────────────────────────
echo ""
if [ ! -f credentials.json ]; then
    echo "⚠️  credentials.json not found."
    echo ""
    echo "   To set up Google Drive + Gmail access:"
    echo "   1. Go to https://console.cloud.google.com"
    echo "   2. Create a new project (or use an existing one)"
    echo "   3. Enable these APIs:"
    echo "      - Google Drive API"
    echo "      - Gmail API"
    echo "   4. Go to APIs & Services → Credentials"
    echo "   5. Create OAuth 2.0 Client ID → Desktop App"
    echo "   6. Download the JSON → save it here as credentials.json"
    echo ""
else
    echo "✅ credentials.json found"
fi

# ─── 6. data directory ───────────────────────────────────────────────────────
mkdir -p data
echo "✅ data/ directory ready"

# ─── 7. Google auth test ─────────────────────────────────────────────────────
echo ""
if [ -f credentials.json ] && [ -f .env ]; then
    echo "Running Google auth (a browser window will open to authorize)..."
    python3 -c "
from dotenv import load_dotenv; load_dotenv()
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/gmail.send']
TOKEN  = Path('token.json')
CREDS  = Path('credentials.json')

creds = None
if TOKEN.exists():
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
print('Google auth complete — token.json saved')
"
fi

# ─── 8. cron setup ───────────────────────────────────────────────────────────
echo ""
PIPELINE_PATH="$(pwd)/job_pipeline.py"
CRON_LINE="0 6 * * * cd $(pwd) && python3 $PIPELINE_PATH >> data/pipeline.log 2>&1"

echo "To run the pipeline every morning at 6am, add this cron job:"
echo ""
echo "   $CRON_LINE"
echo ""
echo "To add it automatically, run:"
echo "   (crontab -l 2>/dev/null; echo \"$CRON_LINE\") | crontab -"
echo ""
echo "To run it manually right now:"
echo "   python3 job_pipeline.py"
echo ""
echo "=== Setup complete ==="
