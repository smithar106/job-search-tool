#!/usr/bin/env python3
"""
Ashlee Thomas — Daily Job Search Pipeline
-----------------------------------------
Runs every morning via cron/launchd.

1.  Scrapes jobs via OpenCLI (LinkedIn, Twitter, HackerNews, Reddit)
    + Greenhouse public API (WRI, RMI, NRDC, EDF, C40, and more)
2.  Scores each job 1-10 with Gemini 2.0 Flash (free tier)
3.  Keeps top 25 matches
4.  Extracts hiring manager name from JD with Gemini (best-effort)
5.  Writes tailored cover letters, renders as PDF (fpdf2)
6.  Uploads PDFs to a dated Google Drive subfolder
7.  Creates a Google Sheet for the day:
      title | company | hiring manager | location | score |
      why it fits | job URL | cover letter URL
8.  Emails Ashlee a summary with the Sheet link + Drive folder link
"""

import csv
import datetime
import io
import json
import logging
import os
import html
import re
import smtplib
import subprocess
import sys
import time
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fpdf import FPDF
from google import genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─── Config ────────────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
RESUME_FILE   = BASE_DIR / "ashlee_resume.txt"
SEEN_FILE     = BASE_DIR / "data" / "seen_jobs.csv"
LOG_FILE      = BASE_DIR / "data" / "pipeline.log"
CREDS_FILE    = BASE_DIR / "credentials.json"
TOKEN_FILE    = BASE_DIR / "token.json"

RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "ashleerthomas@gmail.com")
SENDER_EMAIL    = os.environ.get("SENDER_EMAIL", "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

DAILY_TARGET    = 30   # aim for this many cover letters per day
SCORE_THRESHOLD = 6    # minimum score; we keep top DAILY_TARGET above this

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ─── OpenCLI searches ─────────────────────────────────────────────────────────
# Each entry: (adapter, subcommand, query, extra_flags)

SEARCHES = [
    # LinkedIn — uses Voyager API via logged-in Chrome; --details fetches full JD
    # Remote roles
    ("linkedin", "search", "Head of Sustainability Scope 3",       ["--remote", "remote", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "Climate Finance Director",             ["--remote", "remote", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "ESG Strategy Director supply chain",   ["--remote", "remote", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "Decarbonization Program Manager",      ["--remote", "remote", "--details", "--limit", "20", "--date-posted", "week"]),
    # Hybrid roles (same queries, different remote filter)
    ("linkedin", "search", "Head of Sustainability Scope 3",       ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "Climate Finance Director",             ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "ESG Strategy Director supply chain",   ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "SBTi supplier engagement",            ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "sustainability VP supply chain",       ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "carbon accounting ESG director",       ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "climate policy program director",      ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    # Renewable energy / packaging / supply chain — hybrid
    ("linkedin", "search", "renewable energy sustainability director",    ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "sustainable supply chain ESG manager",        ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    ("linkedin", "search", "sustainable packaging circular economy lead", ["--remote", "hybrid", "--details", "--limit", "20", "--date-posted", "week"]),
    # Twitter
    ("twitter", "search", "hiring climate sustainability director", []),
    ("twitter", "search", "hiring ESG Scope 3 remote",             []),
    ("twitter", "search", "sustainability VP opening hiring",       []),
    # HackerNews
    ("hackernews", "search", "sustainability ESG climate hiring",   []),
    ("hackernews", "search", "Scope 3 decarbonization",            []),
    # Reddit
    ("reddit", "search", "climate sustainability hiring",          ["--subreddit", "r/ClimateJobs"]),
    ("reddit", "search", "ESG job opening",                        ["--subreddit", "r/sustainability"]),
]

# ─── Greenhouse ATS orgs ──────────────────────────────────────────────────────
# Public API, no auth required: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
# slug = the company's Greenhouse board slug (found in their jobs page URL)

GREENHOUSE_ORGS = [
    # Climate / ESG NGOs
    ("World Resources Institute",       "wri"),
    ("Rocky Mountain Institute",        "rockymountaininstitute"),
    ("NRDC",                            "nrdc"),
    ("Environmental Defense Fund",      "edf"),
    ("C40 Cities",                      "c40cities"),
    ("Ceres",                           "ceres"),
    ("ClimateWorks Foundation",         "climateworks"),
    ("Conservation International",      "conservation"),
    ("Oxfam America",                   "oxfamamerica"),
    ("BSR",                             "bsr"),
    ("South Pole",                      "southpole"),
    ("Clean Air Task Force",            "catf"),
    # Tech with large sustainability teams
    ("Salesforce",                      "salesforce"),
    ("Stripe",                          "stripe"),
    ("Airbnb",                          "airbnb"),
    # Renewable energy
    ("Sunrun",                          "sunrun"),
    ("Sunnova Energy",                  "sunnova"),
    ("Nextracker",                      "nextracker"),
    ("Pattern Energy",                  "patternenergy"),
    ("Intersect Power",                 "intersectpower"),
    ("Invenergy",                       "invenergy"),
    # Sustainable packaging
    ("Novamont",                        "novamont"),
    ("Footprint",                       "footprinttech"),
    ("Sealed Air",                      "sealedair"),
    ("Smurfit Westrock",                "smurfitkappa"),
    # Sustainable supply chain / logistics
    ("Flexport",                        "flexport"),
    ("Sourcemap",                       "sourcemap"),
    ("Resilinc",                        "resilinc"),
]

# ─── Lever ATS orgs ───────────────────────────────────────────────────────────
# Public API: https://api.lever.co/v0/postings/{slug}?mode=json
# No auth required.

LEVER_ORGS = [
    # Climate tech / carbon
    ("Watershed",                   "watershed"),
    ("Pachama",                     "pachama"),
    ("Rubicon Carbon",              "rubiconcarbonteam"),
    ("CarbonCure Technologies",     "carboncure"),
    ("Ørsted",                      "orsted"),
    # Consumer sustainability
    ("Patagonia",                   "patagonia"),
    ("Allbirds",                    "allbirds"),
    ("Impossible Foods",            "impossiblefoods"),
    # Renewable energy
    ("Sunlight Financial",          "sunlightfinancial"),
    ("Plus Power",                  "pluspower"),
    ("Arcadia",                     "arcadia"),
    ("Ampere Energy",               "ampere"),
    # Sustainable supply chain
    ("Sourcery",                    "sourcery"),
    ("EcoVadis",                    "ecovadis"),
    ("Ulula",                       "ulula"),
]

# ─── Ashby ATS orgs ───────────────────────────────────────────────────────────
# Public API: https://jobs.ashbyhq.com/api/non-user-facing/job-board/for-organization?organizationHostedJobsPageName={slug}

ASHBY_ORGS = [
    # Carbon removal
    ("Climeworks",                  "climeworks"),
    ("Carbon Direct",               "carbondirect"),
    ("Heirloom Carbon",             "heirloomcarbon"),
    ("Running Tide",                "runningtide"),
    ("Charm Industrial",            "charmindustrial"),
    ("Terraformation",              "terraformation"),
    ("Planetary Technologies",      "planetary"),
    ("Perennial",                   "perennial"),
    ("Remora",                      "remora"),
    ("Terawatt Infrastructure",     "terawatt"),
    # Renewable energy
    ("Omnidian",                    "omnidian"),
    ("Anza Renewables",             "anzarenewables"),
    ("Aeva",                        "aeva"),
    # Sustainable packaging / circular economy
    ("Noissue",                     "noissue"),
    ("Roba Metals",                 "robametals"),
    # Sustainable supply chain
    ("Canopy",                      "canopylabs"),
    ("Pledge",                      "pledge"),
]

# Keywords to filter Greenhouse/Lever/Ashby results to relevant roles
GREENHOUSE_KEYWORDS = {
    "sustainability", "climate", "esg", "carbon", "scope 3", "decarbonization",
    "net zero", "emissions", "supply chain", "environment", "energy", "sbti",
    "procurement", "circular", "impact", "regenerative", "green",
    # Renewable energy
    "renewable", "solar", "wind", "clean energy", "grid", "storage", "battery",
    "clean power", "energy transition", "electrification",
    # Sustainable packaging / circular
    "packaging", "circular economy", "biodegradable", "compostable", "recycled",
    "waste reduction", "end of life", "material", "lifecycle",
    # Sustainable supply chain
    "supplier", "sourcing", "traceability", "responsible sourcing",
    "ethical supply", "vendor", "raw material",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

Path(LOG_FILE).parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Resume ───────────────────────────────────────────────────────────────────

def load_resume() -> str:
    return RESUME_FILE.read_text()

# ─── Seen-jobs deduplication ──────────────────────────────────────────────────

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
                "url":        job.get("url", ""),
                "title":      job.get("title", ""),
                "company":    job.get("company", ""),
                "date_seen":  datetime.date.today().isoformat(),
            })

# ─── Scraping — OpenCLI ───────────────────────────────────────────────────────

def run_opencli(adapter: str, subcommand: str, query: str,
                extra_flags: list[str] = None) -> list[dict]:
    cmd = ["opencli", adapter, subcommand, query] + (extra_flags or []) + ["-f", "json"]
    log.info("Running: %s", " ".join(cmd))
    timeout = 300 if adapter == "linkedin" else 60
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.warning("opencli %d: %s", r.returncode, r.stderr[:200])
            return []
        raw = r.stdout.strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("items", data.get("results", []))
    except subprocess.TimeoutExpired:
        log.warning("opencli timed out: %s %s", adapter, query)
        return []
    except json.JSONDecodeError:
        log.warning("opencli bad JSON: %s %s", adapter, query)
        return []

# ─── Scraping — Greenhouse public API ─────────────────────────────────────────

def fetch_greenhouse(org_name: str, slug: str) -> list[dict]:
    """Pull open roles from Greenhouse public board. No auth needed."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        jobs = data.get("jobs", [])
        results = []
        for job in jobs:
            title = job.get("title", "").lower()
            dept  = " ".join(
                d.get("name", "") for d in job.get("departments", [])
            ).lower()
            blob  = f"{title} {dept}"
            if any(kw in blob for kw in GREENHOUSE_KEYWORDS):
                content = job.get("content", "")
                # Decode HTML entities then strip tags
                content = html.unescape(content)
                description = re.sub(r"<[^>]+>", " ", content)
                description = re.sub(r"\s+", " ", description).strip()
                results.append({
                    "title":       job.get("title", ""),
                    "company":     org_name,
                    "url":         job.get("absolute_url", ""),
                    "description": description[:5000],
                    "location":    ", ".join(
                        o.get("name", "") for o in job.get("offices", [])
                    ),
                    "salary":      "",
                    "listed":      job.get("updated_at", "")[:10],
                    "source":      "greenhouse",
                })
        log.info("Greenhouse %s: %d relevant roles", org_name, len(results))
        return results
    except Exception as e:
        log.warning("Greenhouse fetch failed for %s: %s", org_name, e)
        return []

# ─── Scraping — Lever public API ─────────────────────────────────────────────

def fetch_lever(org_name: str, slug: str) -> list[dict]:
    """Pull open roles from Lever public job board. No auth needed."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            jobs = json.loads(resp.read().decode())
        results = []
        for job in (jobs if isinstance(jobs, list) else []):
            title = job.get("text", "").lower()
            categories = json.dumps(job.get("categories", {})).lower()
            blob = f"{title} {categories}"
            if any(kw in blob for kw in GREENHOUSE_KEYWORDS):
                # Lever stores description in lists array
                desc_parts = []
                for item in job.get("lists", []):
                    desc_parts.append(item.get("text", ""))
                    for c in item.get("content", "").split("<li>"):
                        clean = re.sub(r"<[^>]+>", " ", c).strip()
                        if clean:
                            desc_parts.append(clean)
                additional = re.sub(r"<[^>]+>", " ",
                                    html.unescape(job.get("additional", ""))).strip()
                if additional:
                    desc_parts.append(additional)
                description = " ".join(desc_parts)[:5000]

                results.append({
                    "title":       job.get("text", ""),
                    "company":     org_name,
                    "url":         job.get("hostedUrl", job.get("applyUrl", "")),
                    "description": description,
                    "location":    job.get("categories", {}).get("location", ""),
                    "salary":      "",
                    "listed":      "",
                    "source":      "lever",
                })
        log.info("Lever %s: %d relevant roles", org_name, len(results))
        return results
    except Exception as e:
        log.warning("Lever fetch failed for %s: %s", org_name, e)
        return []

# ─── Scraping — Ashby public API ──────────────────────────────────────────────

def fetch_ashby(org_name: str, slug: str) -> list[dict]:
    """Pull open roles from Ashby public job board. No auth needed."""
    url = (
        "https://jobs.ashbyhq.com/api/non-user-facing/job-board/for-organization"
        f"?organizationHostedJobsPageName={slug}"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "application/json"},
            data=b"{}",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        jobs = data.get("jobPostings", [])
        results = []
        for job in jobs:
            title = job.get("title", "").lower()
            dept  = job.get("departmentName", "").lower()
            blob  = f"{title} {dept}"
            if any(kw in blob for kw in GREENHOUSE_KEYWORDS):
                desc_html = html.unescape(job.get("descriptionHtml", ""))
                description = re.sub(r"<[^>]+>", " ", desc_html)
                description = re.sub(r"\s+", " ", description).strip()[:5000]
                results.append({
                    "title":       job.get("title", ""),
                    "company":     org_name,
                    "url":         f"https://jobs.ashbyhq.com/{slug}/{job.get('id', '')}",
                    "description": description,
                    "location":    job.get("locationName", ""),
                    "salary":      "",
                    "listed":      job.get("publishedAt", "")[:10],
                    "source":      "ashby",
                })
        log.info("Ashby %s: %d relevant roles", org_name, len(results))
        return results
    except Exception as e:
        log.warning("Ashby fetch failed for %s: %s", org_name, e)
        return []

# ─── Combined scrape ──────────────────────────────────────────────────────────

def scrape_jobs(seen: set) -> list[dict]:
    all_jobs = []

    # OpenCLI sources
    for adapter, subcommand, query, extra_flags in SEARCHES:
        for job in run_opencli(adapter, subcommand, query, extra_flags):
            url = job.get("url", "")
            if url and url not in seen:
                all_jobs.append({
                    "title":       job.get("title") or job.get("name", "Unknown Role"),
                    "company":     job.get("company") or job.get("org") or job.get("author") or "Unknown",
                    "url":         url,
                    "description": job.get("description") or job.get("body") or job.get("text", ""),
                    "location":    job.get("location", ""),
                    "salary":      job.get("salary", ""),
                    "listed":      job.get("listed", ""),
                    "source":      adapter,
                })
                seen.add(url)
        time.sleep(2)

    # Greenhouse sources
    for org_name, slug in GREENHOUSE_ORGS:
        for job in fetch_greenhouse(org_name, slug):
            url = job.get("url", "")
            if url and url not in seen:
                all_jobs.append(job)
                seen.add(url)
        time.sleep(0.5)

    # Lever sources
    for org_name, slug in LEVER_ORGS:
        for job in fetch_lever(org_name, slug):
            url = job.get("url", "")
            if url and url not in seen:
                all_jobs.append(job)
                seen.add(url)
        time.sleep(0.5)

    # Ashby sources
    for org_name, slug in ASHBY_ORGS:
        for job in fetch_ashby(org_name, slug):
            url = job.get("url", "")
            if url and url not in seen:
                all_jobs.append(job)
                seen.add(url)
        time.sleep(0.5)

    log.info("Total new jobs found: %d", len(all_jobs))
    return all_jobs

# ─── Gemini ───────────────────────────────────────────────────────────────────

def init_gemini() -> genai.Client:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY not set")
        sys.exit(1)
    return genai.Client(api_key=GEMINI_API_KEY)

def score_job(job: dict, resume: str, client: genai.Client) -> dict:
    prompt = f"""You are evaluating job fit for Ashlee R. Thomas, a senior climate and ESG professional.

CANDIDATE PROFILE:
{resume}

JOB:
Title: {job['title']}
Company: {job['company']}
Location: {job.get('location', 'unknown')}
Description: {job['description'][:3000] if job['description'] else 'No description available'}

Score this job 1-10 for candidate fit. Consider: Scope 3/SBTi experience match, \
seniority alignment, sector fit (climate/ESG/sustainability), language requirements.

Return ONLY valid JSON, no markdown:
{{"score": 8, "why": "Strong match — 2 sentences max", "missing": "One gap or none"}}"""
    try:
        r = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        text = r.text.strip().strip("```json").strip("```").strip()
        return json.loads(text)
    except Exception as e:
        log.warning("Scoring failed for %s: %s", job['title'], e)
        return {"score": 0, "why": "scoring error", "missing": "n/a"}

def extract_hiring_manager(job: dict, client: genai.Client) -> str:
    """Best-effort: find a hiring manager or key contact name in the JD."""
    if not job.get("description"):
        return "—"
    prompt = f"""Read this job description and return the name of the hiring manager, \
recruiter, or main contact person if one is mentioned. If no name is found, return exactly: —

Job description:
{job['description'][:2000]}

Return only the name or — . No explanation."""
    try:
        r = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        result = r.text.strip().strip('"').strip("'")
        return result if result and len(result) < 80 else "—"
    except Exception:
        return "—"

def write_cover_letter(job: dict, score_data: dict, resume: str,
                       client: genai.Client) -> str:
    prompt = f"""Write a professional 3-paragraph cover letter for Ashlee R. Thomas.

STRICT RULES:
- Do NOT open with "I am excited to apply" or any generic opener
- Para 1: Open with something SPECIFIC about {job['company']}'s sustainability program, \
ESG commitments, or recent climate initiative. Show you know them.
- Para 2: Connect Ashlee's Oxfam Scope 3 work and/or USAID background directly to \
2-3 specific requirements from the JD
- Para 3: Close noting her CC-P certification, multilingual capability \
(English/Spanish/French), and a clear call to action
- Tone: Direct, collegial, no filler phrases
- Length: 250-300 words

CANDIDATE:
{resume}

ROLE: {job['title']} at {job['company']}
LOCATION: {job.get('location', '')}
URL: {job['url']}
JD: {job['description'][:4000] if job['description'] else 'No description available'}
FIT NOTES: {score_data.get('why', '')}

Write only the letter body. No subject line, no date, no address block."""
    try:
        r = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return r.text.strip()
    except Exception as e:
        log.warning("Cover letter failed for %s: %s", job['title'], e)
        return f"[Cover letter generation failed: {e}]"

# ─── LinkedIn outreach blurbs ─────────────────────────────────────────────────

def write_linkedin_outreach(job: dict, resume: str, client: genai.Client) -> dict:
    """
    Generate two LinkedIn blurbs per job:
      - 'message'  : ~150-word InMail / open-message for when free messaging is enabled
      - 'note'     : ≤120-char connection request note for "Send with note" button
    Returns {"message": "...", "note": "..."}
    """
    hm   = job.get("hiring_manager", "—")
    hm_line = f"Hiring manager: {hm}" if hm != "—" else "Hiring manager: unknown"

    prompt = f"""Write two LinkedIn outreach messages for Ashlee R. Thomas applying to:

Role: {job['title']} at {job['company']}
{hm_line}
Why she fits: {job.get('why', '')}

MESSAGE (free InMail / open message — hiring manager has messaging enabled):
- 100-150 words maximum
- Address by first name if hiring manager name is known, otherwise "Hi" + no name
- Open with one specific observation about {job['company']}'s climate/ESG work
- One sentence connecting her Oxfam Scope 3 or USAID background to this role
- Mention she has applied and a tailored cover letter is attached
- End with a low-friction ask: "Happy to connect if you have questions."
- No emojis, no filler phrases, no "I hope this message finds you well"

CONNECTION NOTE (for the "Connect / Add a note" button — hard limit 120 characters including spaces):
- Must be UNDER 120 characters — count carefully
- First name if known, otherwise no name
- One punchy reason she's a fit
- No hashtags, no emojis

Return ONLY valid JSON, no markdown:
{{"message": "...", "note": "..."}}"""

    try:
        r    = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        text = r.text.strip().strip("```json").strip("```").strip()
        data = json.loads(text)
        # Enforce the 120-char hard limit on the note
        note = str(data.get("note", "")).strip()
        if len(note) > 120:
            note = note[:117].rsplit(" ", 1)[0] + "..."
        return {
            "message": str(data.get("message", "")).strip(),
            "note":    note,
        }
    except Exception as e:
        log.warning("LinkedIn outreach failed for %s: %s", job["title"], e)
        hm_first = hm.split()[0] if hm != "—" else ""
        greeting  = f"Hi {hm_first}," if hm_first else "Hi,"
        fallback_msg = (
            f"{greeting} I've applied for the {job['title']} role at {job['company']}. "
            f"My Oxfam Scope 3 and USAID background align closely with what you're building. "
            f"Happy to connect if you have questions."
        )
        fallback_note = f"Applied for {job['title']} — Scope 3 + USAID background, CC-P certified."
        return {
            "message": fallback_msg,
            "note":    fallback_note[:120],
        }

# ─── PDF rendering ────────────────────────────────────────────────────────────

def render_pdf(job: dict, letter_text: str, today: str) -> bytes:
    """Render cover letter as a clean PDF. Returns raw bytes."""
    pdf = FPDF()
    pdf.set_margins(25, 25, 25)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "Ashlee R. Thomas", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 5, "ashleerthomas@gmail.com  |  linkedin.com/in/ashleerthomas", ln=True)
    pdf.ln(2)

    # Divider
    pdf.set_draw_color(180, 180, 180)
    pdf.line(25, pdf.get_y(), 185, pdf.get_y())
    pdf.ln(5)

    # Date + recipient block
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, today, ln=True)
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, f"{job['title']}", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, f"{job['company']}", ln=True)
    if job.get("location"):
        pdf.cell(0, 6, job["location"], ln=True)
    pdf.ln(5)

    # Letter body
    pdf.set_font("Helvetica", "", 11)
    # fpdf2 multi_cell handles line wrapping; split paragraphs on blank lines
    for para in letter_text.split("\n\n"):
        para = para.strip()
        if para:
            pdf.multi_cell(0, 6, para)
            pdf.ln(3)

    # Footer
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, f"Job URL: {job['url']}", ln=True)

    return bytes(pdf.output())

# ─── Google credentials ───────────────────────────────────────────────────────

def get_google_creds() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                log.error("credentials.json not found — see README Step 5")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return creds

# ─── Google Drive ─────────────────────────────────────────────────────────────

def create_drive_folder(drive_svc, today: str) -> tuple[str, str]:
    """Create a dated subfolder. Returns (folder_id, shareable_url)."""
    folder = drive_svc.files().create(
        body={
            "name": f"Job Matches — {today}",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [DRIVE_FOLDER_ID],
        },
        fields="id",
    ).execute()
    fid = folder["id"]
    drive_svc.permissions().create(
        fileId=fid, body={"type": "anyone", "role": "reader"}
    ).execute()
    return fid, f"https://drive.google.com/drive/folders/{fid}"

def upload_pdf(drive_svc, filename: str, pdf_bytes: bytes, folder_id: str) -> str:
    """Upload PDF to Drive folder. Returns shareable view URL."""
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False
    )
    f = drive_svc.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id",
    ).execute()
    fid = f["id"]
    drive_svc.permissions().create(
        fileId=fid, body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{fid}/view"

# ─── Google Sheets ────────────────────────────────────────────────────────────

SHEET_HEADERS = [
    "#", "Date", "Title", "Company", "Hiring Manager",
    "Location", "Score", "Why it fits", "What's missing",
    "Job URL", "Cover Letter PDF", "LinkedIn Message", "Connection Note (≤120 chars)",
]

def create_daily_sheet(sheets_svc, drive_svc, matches: list[dict], today: str) -> str:
    """Create a Google Sheet for today's matches. Returns shareable URL."""
    # Create spreadsheet
    ss = sheets_svc.spreadsheets().create(
        body={
            "properties": {"title": f"Job Matches — {today}"},
            "sheets": [{"properties": {"title": "Matches"}}],
        }
    ).execute()
    ss_id  = ss["spreadsheetId"]
    ss_url = f"https://docs.google.com/spreadsheets/d/{ss_id}/edit"

    # Move into the daily Drive folder
    drive_svc.files().update(
        fileId=ss_id,
        addParents=DRIVE_FOLDER_ID,
        removeParents="root",
        fields="id, parents",
    ).execute()

    # Make it shareable (anyone with link can view)
    drive_svc.permissions().create(
        fileId=ss_id, body={"type": "anyone", "role": "reader"}
    ).execute()

    # Build rows
    rows = [SHEET_HEADERS]
    for i, m in enumerate(matches, 1):
        rows.append([
            i,
            today,
            m.get("title", ""),
            m.get("company", ""),
            m.get("hiring_manager", "—"),
            m.get("location", ""),
            m.get("score", ""),
            m.get("why", ""),
            m.get("missing", ""),
            m.get("url", ""),
            m.get("cover_letter_url", ""),
            m.get("linkedin_message", ""),
            m.get("linkedin_note", ""),
        ])

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=ss_id,
        range="Matches!A1",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()

    # Bold header row, freeze it, auto-resize columns
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=ss_id,
        body={"requests": [
            # Bold header
            {"repeatCell": {
                "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }},
            # Freeze header row
            {"updateSheetProperties": {
                "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Auto-resize all columns
            {"autoResizeDimensions": {
                "dimensions": {"sheetId": 0, "dimension": "COLUMNS",
                               "startIndex": 0, "endIndex": len(SHEET_HEADERS)},
            }},
        ]},
    ).execute()

    log.info("Sheet created: %s", ss_url)
    return ss_url

# ─── Email ────────────────────────────────────────────────────────────────────

def send_summary_email(matches: list[dict], folder_url: str,
                       sheet_url: str, today: str):
    if not SENDER_EMAIL or not GMAIL_APP_PASS:
        log.error("SENDER_EMAIL or GMAIL_APP_PASSWORD not set — skipping email")
        return

    count   = len(matches)
    subject = f"Job pipeline — {today} — {count} match{'es' if count != 1 else ''} ready"

    rows_html = ""
    rows_text = ""
    for i, m in enumerate(matches, 1):
        rows_html += f"""
        <tr>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;color:#888;">{i}</td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;">
            <a href="{m['url']}" style="color:#1a5c1a;font-weight:bold;">{m['title']}</a>
          </td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;">{m['company']}</td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;color:#555;">{m.get('hiring_manager','—')}</td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;font-weight:bold;color:#1a7a1a;">{m['score']}/10</td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;font-size:12px;">{m.get('why','')}</td>
          <td style="padding:8px 6px;border-bottom:1px solid #eee;">
            <a href="{m.get('cover_letter_url','#')}">PDF</a>
          </td>
        </tr>"""
        rows_text += (
            f"\n  {i}. {m['title']} @ {m['company']} — {m['score']}/10\n"
            f"     HM: {m.get('hiring_manager','—')}\n"
            f"     {m['url']}\n"
            f"     Why: {m.get('why','')}\n"
        )

    html_body = f"""
<html><body style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto;padding:20px;color:#222;">
  <h2 style="color:#1a5c1a;margin-bottom:4px;">Job matches — {today}</h2>
  <p style="margin-top:0;color:#555;">{count} role{'s' if count!=1 else ''} ready for you.</p>
  <p>
    <a href="{sheet_url}" style="background:#1a5c1a;color:#fff;padding:8px 16px;
       border-radius:4px;text-decoration:none;font-weight:bold;margin-right:8px;">
      Open Google Sheet →
    </a>
    <a href="{folder_url}" style="background:#f0f0f0;color:#333;padding:8px 16px;
       border-radius:4px;text-decoration:none;">
      Cover Letter PDFs →
    </a>
  </p>
  <table style="width:100%;border-collapse:collapse;margin-top:20px;font-size:13px;">
    <tr style="background:#f5f5f5;font-size:12px;text-transform:uppercase;letter-spacing:.05em;">
      <th style="padding:8px 6px;text-align:left;">#</th>
      <th style="padding:8px 6px;text-align:left;">Role</th>
      <th style="padding:8px 6px;text-align:left;">Company</th>
      <th style="padding:8px 6px;text-align:left;">Hiring Manager</th>
      <th style="padding:8px 6px;text-align:left;">Score</th>
      <th style="padding:8px 6px;text-align:left;">Why it fits</th>
      <th style="padding:8px 6px;text-align:left;">Cover Letter</th>
    </tr>
    {rows_html}
  </table>
  <p style="margin-top:24px;font-size:11px;color:#aaa;">
    Powered by OpenCLI + Gemini + Google Drive. Apply directly from each job URL.
  </p>
</body></html>"""

    text_body = (
        f"Job matches — {today}\n"
        f"{count} role(s) ready.\n\n"
        f"Google Sheet: {sheet_url}\n"
        f"Cover Letter PDFs: {folder_url}\n"
        f"{rows_text}"
    )

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

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    log.info("=== Job pipeline starting — %s ===", today)

    missing_vars = [v for v in [
        "GEMINI_API_KEY", "SENDER_EMAIL", "GMAIL_APP_PASSWORD", "DRIVE_FOLDER_ID"
    ] if not os.environ.get(v)]
    if missing_vars:
        log.error("Missing env vars: %s  — see README or run: cp .env.example .env", missing_vars)
        sys.exit(1)

    client = init_gemini()
    resume = load_resume()
    seen   = load_seen()

    # 1. Scrape
    jobs = scrape_jobs(seen)
    if not jobs:
        log.info("No new jobs found today.")
        return

    # 2. Score all jobs
    log.info("Scoring %d jobs...", len(jobs))
    scored = []
    for job in jobs:
        result = score_job(job, resume, client)
        job.update({
            "score":   result.get("score", 0),
            "why":     result.get("why", ""),
            "missing": result.get("missing", ""),
        })
        scored.append(job)
        time.sleep(0.3)

    # 3. Select top DAILY_TARGET above threshold, sorted by score desc
    qualified = sorted(
        [j for j in scored if j["score"] >= SCORE_THRESHOLD],
        key=lambda x: x["score"],
        reverse=True,
    )[:DAILY_TARGET]

    log.info("%d qualified matches (target %d)", len(qualified), DAILY_TARGET)

    if not qualified:
        log.info("No qualifying matches today — try lowering SCORE_THRESHOLD.")
        mark_seen(scored)
        return

    # 4. Extract hiring managers
    log.info("Extracting hiring manager names...")
    for job in qualified:
        job["hiring_manager"] = extract_hiring_manager(job, client)
        time.sleep(0.2)

    # 5. Set up Google services
    creds       = get_google_creds()
    drive_svc   = build("drive",        "v3",  credentials=creds)
    sheets_svc  = build("sheets",       "v4",  credentials=creds)

    folder_id, folder_url = create_drive_folder(drive_svc, today)
    log.info("Drive folder: %s", folder_url)

    # 6. Write cover letters, LinkedIn blurbs, render PDFs, upload
    log.info("Writing %d cover letters + LinkedIn blurbs...", len(qualified))
    for i, job in enumerate(qualified):
        log.info("  %d/%d  %s @ %s", i + 1, len(qualified), job["title"], job["company"])

        letter_text = write_cover_letter(job, {"why": job["why"]}, resume, client)
        pdf_bytes   = render_pdf(job, letter_text, today)

        safe = re.sub(r"[^\w\s\-]", "", f"{job['company']} - {job['title']}")[:80].strip()
        job["cover_letter_url"] = upload_pdf(drive_svc, f"{safe}.pdf", pdf_bytes, folder_id)

        outreach = write_linkedin_outreach(job, resume, client)
        job["linkedin_message"] = outreach["message"]
        job["linkedin_note"]    = outreach["note"]

        time.sleep(0.5)

    # 7. Create Google Sheet
    sheet_url = create_daily_sheet(sheets_svc, drive_svc, qualified, today)

    # 8. Mark all scraped jobs as seen
    mark_seen(scored)

    # 9. Send email
    send_summary_email(qualified, folder_url, sheet_url, today)

    log.info("=== Done — %d matches, sheet: %s ===", len(qualified), sheet_url)


if __name__ == "__main__":
    main()
