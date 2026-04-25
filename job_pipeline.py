#!/usr/bin/env python3
"""
Ashlee Thomas — Daily Job Search Pipeline
----------------------------------------
Runs every morning via cron/launchd.
1. Scrapes jobs via OpenCLI (LinkedIn, Twitter, HackerNews, Reddit)
2. Scores each job with Gemini 1.5 Flash (free tier)
3. Writes tailored cover letters for 7+ matches with Gemini 1.5 Pro (free tier)
4. Uploads cover letters to a dated Google Drive folder
5. Emails Ashlee a summary with the Drive link
"""

import csv
import datetime
import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ─── Config ────────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
RESUME_FILE   = BASE_DIR / "ashlee_resume.txt"
SEEN_FILE     = BASE_DIR / "data" / "seen_jobs.csv"
LOG_FILE      = BASE_DIR / "data" / "pipeline.log"
CREDS_FILE    = BASE_DIR / "credentials.json"
TOKEN_FILE    = BASE_DIR / "token.json"

RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ashleerthomas@gmail.com")
SENDER_EMAIL    = os.environ.get("SENDER_EMAIL", "")          # your Gmail
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD", "")    # Gmail app password
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")       # Google Drive folder ID

SCORE_THRESHOLD = 7   # minimum score to include
MAX_COVER_LETTERS_PER_DAY = 10  # safety cap on Gemini Pro calls

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
]

# ─── Search queries ─────────────────────────────────────────────────────────────
# Each entry: (opencli_adapter, subcommand, query_string)
SEARCHES = [
    # LinkedIn (requires opencli linkedin adapter — see README)
    ("linkedin", "jobs", "Head of Sustainability Scope 3 remote"),
    ("linkedin", "jobs", "Climate Finance Director remote"),
    ("linkedin", "jobs", "ESG Strategy Director supply chain"),
    ("linkedin", "jobs", "Decarbonization Program Manager remote"),
    ("linkedin", "jobs", "SBTi supplier engagement lead"),
    ("linkedin", "jobs", "Supply chain sustainability VP"),
    # Twitter
    ("twitter", "search", "hiring climate sustainability director"),
    ("twitter", "search", "hiring ESG Scope 3 remote"),
    ("twitter", "search", "sustainability VP opening hiring"),
    # HackerNews
    ("hackernews", "search", "sustainability ESG climate hiring"),
    ("hackernews", "search", "Scope 3 decarbonization"),
    # Reddit
    ("reddit", "search", "climate sustainability hiring remote --subreddit r/ClimateJobs"),
    ("reddit", "search", "ESG job opening --subreddit r/sustainability"),
]

# ─── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Resume ─────────────────────────────────────────────────────────────────────

def load_resume() -> str:
    return RESUME_FILE.read_text()

# ─── Seen jobs (deduplication) ───────────────────────────────────────────────────

def load_seen() -> set:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE) as f:
        return {row["url"] for row in csv.DictReader(f) if row.get("url")}

def mark_seen(jobs: list):
    SEEN_FILE.parent.mkdir(exist_ok=True)
    exists = SEEN_FILE.exists()
    with open(SEEN_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "title", "company", "date_seen"])
        if not exists:
            writer.writeheader()
        for job in jobs:
            writer.writerow({
                "url": job.get("url", ""),
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "date_seen": datetime.date.today().isoformat(),
            })

# ─── Scraping ────────────────────────────────────────────────────────────────────

def run_opencli(adapter: str, subcommand: str, query: str) -> list[dict]:
    """Run an opencli command and return parsed JSON results."""
    # Split query in case it has flags like --subreddit
    query_parts = query.split()
    cmd = ["opencli", adapter, subcommand] + query_parts + ["-f", "json"]
    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning("opencli returned %d: %s", result.returncode, result.stderr[:200])
            return []
        raw = result.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("items", data.get("results", []))
    except subprocess.TimeoutExpired:
        log.warning("opencli timed out for: %s %s", adapter, query)
        return []
    except json.JSONDecodeError:
        log.warning("Could not parse JSON from opencli output")
        return []

def scrape_jobs(seen: set) -> list[dict]:
    """Scrape all sources, deduplicate against seen set."""
    all_jobs = []
    for adapter, subcommand, query in SEARCHES:
        jobs = run_opencli(adapter, subcommand, query)
        for job in jobs:
            url = job.get("url", "")
            if url and url not in seen:
                # Normalize fields — different adapters use different key names
                all_jobs.append({
                    "title":       job.get("title") or job.get("name", "Unknown Role"),
                    "company":     job.get("company") or job.get("org") or job.get("source", "Unknown"),
                    "url":         url,
                    "description": job.get("description") or job.get("body") or job.get("text", ""),
                    "location":    job.get("location", ""),
                    "source":      adapter,
                })
                seen.add(url)
        time.sleep(1)  # be gentle with the browser extension
    log.info("Found %d new jobs across all sources", len(all_jobs))
    return all_jobs

# ─── Gemini ──────────────────────────────────────────────────────────────────────

def init_gemini() -> genai.Client:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set. Run: export GEMINI_API_KEY=your_key")
        sys.exit(1)
    return genai.Client(api_key=GEMINI_API_KEY)

def score_job(job: dict, resume: str, client: genai.Client = None) -> dict:
    """Score job 1-10 with Gemini Flash (fast, free tier)."""
    if client is None:
        client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""You are evaluating job fit for Ashlee R. Thomas, a senior climate and ESG professional.

CANDIDATE PROFILE:
{resume}

JOB:
Title: {job['title']}
Company: {job['company']}
Location: {job.get('location', 'unknown')}
Description: {job['description'][:3000] if job['description'] else 'No description available'}

Score this job 1-10 for candidate fit. Consider: Scope 3/SBTi experience match, seniority alignment, sector fit (climate/ESG/sustainability), language requirements.

Return ONLY valid JSON, no markdown, no explanation:
{{"score": 8, "why": "Strong match — 2 sentences max", "missing": "One gap or 'none'"}}"""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = response.text.strip().strip("```json").strip("```").strip()
        return json.loads(text)
    except Exception as e:
        log.warning("Scoring failed for %s: %s", job['title'], e)
        return {"score": 0, "why": "scoring error", "missing": "n/a"}

def write_cover_letter(job: dict, score_data: dict, resume: str, client: genai.Client = None) -> str:
    """Write tailored cover letter with Gemini Pro."""
    if client is None:
        client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""Write a professional 3-paragraph cover letter for Ashlee R. Thomas applying to this role.

STRICT RULES:
- Do NOT open with "I am excited to apply" or any generic opener
- Para 1: Open with something SPECIFIC about {job['company']}'s sustainability program, ESG commitments, or recent climate initiative. Show you know them.
- Para 2: Connect Ashlee's Oxfam Scope 3 work and/or USAID background directly to 2-3 specific requirements from the JD
- Para 3: Close by noting her CC-P certification, multilingual capability (English/Spanish/French), and availability. One clear call to action.
- Tone: Direct, collegial, no filler phrases
- Length: ~250-300 words

CANDIDATE:
{resume}

ROLE: {job['title']} at {job['company']}
LOCATION: {job.get('location', '')}
URL: {job['url']}
JD: {job['description'][:4000] if job['description'] else 'No description available'}
FIT NOTES: {score_data.get('why', '')}

Write only the letter body. No subject line, no date, no address block."""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        log.warning("Cover letter failed for %s: %s", job['title'], e)
        return f"[Cover letter generation failed: {e}]"

# ─── Google Drive ────────────────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), DRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                log.error("credentials.json not found. See README for Google API setup.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds

def create_drive_folder(service, today: str) -> tuple[str, str]:
    """Create a dated subfolder inside the main Drive folder. Returns (folder_id, folder_url)."""
    folder_name = f"Job Matches — {today}"
    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [DRIVE_FOLDER_ID],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    folder_id = folder.get("id")
    # Make it readable by anyone with the link (so Ashlee can open without extra sharing)
    service.permissions().create(
        fileId=folder_id,
        body={"type": "anyone", "role": "reader"},
    ).execute()
    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    return folder_id, folder_url

def upload_cover_letter(service, filename: str, content: str, folder_id: str) -> str:
    """Upload a text file to Drive. Returns file URL."""
    meta = {"name": filename, "parents": [folder_id]}
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    return f"https://drive.google.com/file/d/{f.get('id')}/view"

# ─── Email ───────────────────────────────────────────────────────────────────────

def send_summary_email(matches: list[dict], folder_url: str, today: str):
    """Send morning summary email to Ashlee."""
    if not SENDER_EMAIL or not GMAIL_APP_PASS:
        log.error("SENDER_EMAIL or GMAIL_APP_PASSWORD not set — skipping email")
        return

    subject = f"Job matches for {today} — {len(matches)} role{'s' if len(matches)!=1 else ''} ready"

    rows_html = ""
    rows_text = ""
    for i, m in enumerate(matches, 1):
        rows_html += f"""
        <tr>
          <td style="padding:8px;border-bottom:1px solid #eee;">{i}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;"><a href="{m['url']}">{m['title']}</a></td>
          <td style="padding:8px;border-bottom:1px solid #eee;">{m['company']}</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-weight:bold;color:#1a7a1a;">{m['score']}/10</td>
          <td style="padding:8px;border-bottom:1px solid #eee;font-size:13px;">{m.get('why','')}</td>
        </tr>"""
        rows_text += f"\n  {i}. {m['title']} @ {m['company']} — {m['score']}/10\n     {m['url']}\n     Why: {m.get('why','')}\n"

    html_body = f"""
<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
  <h2 style="color:#1a7a1a;">Job matches for {today}</h2>
  <p>{len(matches)} role{'s' if len(matches)!=1 else ''} scored {SCORE_THRESHOLD}+ out of 10 today.</p>
  <p><strong><a href="{folder_url}">Open cover letters in Google Drive →</a></strong></p>
  <table style="width:100%;border-collapse:collapse;margin-top:16px;">
    <tr style="background:#f5f5f5;">
      <th style="padding:8px;text-align:left;">#</th>
      <th style="padding:8px;text-align:left;">Role</th>
      <th style="padding:8px;text-align:left;">Company</th>
      <th style="padding:8px;text-align:left;">Score</th>
      <th style="padding:8px;text-align:left;">Why it fits</th>
    </tr>
    {rows_html}
  </table>
  <p style="margin-top:24px;font-size:12px;color:#888;">
    Each cover letter is in the Drive folder above. Apply directly from the job URL.
  </p>
</body></html>"""

    text_body = f"""Job matches for {today}
{len(matches)} role(s) scored {SCORE_THRESHOLD}+ out of 10.

Cover letters: {folder_url}
{rows_text}
Apply directly from each job URL above.
"""

    msg = MIMEMultipart("alternative")
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SENDER_EMAIL, GMAIL_APP_PASS)
            s.send_message(msg)
        log.info("Email sent to %s", RECIPIENT_EMAIL)
    except Exception as e:
        log.error("Email failed: %s", e)

# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    log.info("=== Job pipeline starting for %s ===", today)

    # Validate required env vars
    missing = [v for v in ["GEMINI_API_KEY", "SENDER_EMAIL", "GMAIL_APP_PASSWORD", "DRIVE_FOLDER_ID"]
               if not os.environ.get(v)]
    if missing:
        log.error("Missing environment variables: %s\nSee README or run: cp .env.example .env", missing)
        sys.exit(1)

    client = init_gemini()
    resume = load_resume()
    seen   = load_seen()

    # 1. Scrape
    jobs = scrape_jobs(seen)
    if not jobs:
        log.info("No new jobs found today.")
        return

    # 2. Score
    log.info("Scoring %d jobs...", len(jobs))
    scored = []
    for job in jobs:
        score_data = score_job(job, resume, client)
        job["score"] = score_data.get("score", 0)
        job["why"]   = score_data.get("why", "")
        job["missing"] = score_data.get("missing", "")
        scored.append(job)
        time.sleep(0.5)  # stay well within free tier rate limits

    matches = [j for j in scored if j["score"] >= SCORE_THRESHOLD]
    log.info("%d of %d jobs scored %d+", len(matches), len(scored), SCORE_THRESHOLD)

    if not matches:
        log.info("No qualifying matches today.")
        mark_seen(scored)
        return

    # 3. Google Drive setup
    creds = get_google_creds()
    drive_service = build("drive", "v3", credentials=creds)
    folder_id, folder_url = create_drive_folder(drive_service, today)
    log.info("Drive folder created: %s", folder_url)

    # 4. Write and upload cover letters
    for i, job in enumerate(matches[:MAX_COVER_LETTERS_PER_DAY]):
        log.info("Writing cover letter %d/%d: %s @ %s",
                 i+1, min(len(matches), MAX_COVER_LETTERS_PER_DAY),
                 job["title"], job["company"])
        letter = write_cover_letter(job, {"why": job["why"]}, resume, client)
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in
                            f"{job['company']} - {job['title']}")[:80]
        filename  = f"{safe_name}.txt"
        file_url  = upload_cover_letter(drive_service, filename, letter, folder_id)
        job["cover_letter_url"] = file_url
        time.sleep(1)

    # 5. Mark all scored jobs as seen (not just matches, so we don't re-score tomorrow)
    mark_seen(scored)

    # 6. Send email
    send_summary_email(matches, folder_url, today)
    log.info("=== Pipeline complete. %d matches sent to %s ===", len(matches), RECIPIENT_EMAIL)

if __name__ == "__main__":
    main()
