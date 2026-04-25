# Job Search Pipeline

Automated daily job search for Ashlee R. Thomas.

Every morning at 6am this script:
1. Scrapes LinkedIn, Twitter, HackerNews, and Reddit for matching roles
2. Scores each job 1–10 using Gemini AI (free)
3. Writes tailored cover letters for roles that score 7+
4. Uploads the cover letters to a Google Drive folder
5. Emails you a summary with a link to the folder

**Cost: $0/month.** Everything uses free tiers.

---

## What you need before starting

- A Mac or Windows computer
- A Gmail account (to send the morning emails)
- A Google account (for Drive storage)
- Internet connection

You do **not** need to know how to code. Follow the steps below exactly.

---

## Step 1 — Install Claude Code (one-time)

Claude Code is the AI assistant that will help you set this up.

1. Install Node.js from https://nodejs.org (click the LTS version)
2. Open Terminal (Mac: press Cmd+Space, type "Terminal", press Enter)
3. Run this command:
   ```
   npm install -g @anthropic/claude-code
   ```
4. Run `claude` to start it and follow the login prompt

---

## Step 2 — Install OpenCLI (one-time)

OpenCLI scrapes job sites using your logged-in Chrome browser.

In Terminal, run:
```bash
npm install -g @jackwener/opencli
```

Then install the browser extension:
1. Download the latest `opencli-extension-vX.X.X.zip` from:
   https://github.com/jackwener/opencli/releases
2. Unzip it
3. Open Chrome → go to `chrome://extensions`
4. Turn on **Developer mode** (top right toggle)
5. Click **Load unpacked** → select the unzipped folder
6. You should see "OpenCLI Bridge" appear in your extensions

Test it works:
```bash
opencli hackernews top --limit 3
```
If you see results, you're good.

---

## Step 3 — Get a free Gemini API key (one-time)

Gemini is the AI that scores jobs and writes your cover letters.

1. Go to https://aistudio.google.com
2. Sign in with your Google account
3. Click **Get API Key** → **Create API key**
4. Copy the key (it looks like: `AIzaSy...`)
5. Save it somewhere safe — you'll need it in Step 6

---

## Step 4 — Set up Google Drive (one-time)

1. Go to https://drive.google.com
2. Create a new folder called **"Job Search — Cover Letters"**
3. Open the folder — look at the URL in your browser:
   ```
   https://drive.google.com/drive/folders/COPY_THIS_PART
   ```
4. Copy the folder ID (the long string after `/folders/`)

---

## Step 5 — Set up Google API credentials (one-time)

This lets the script upload files to Drive and send emails on your behalf.

1. Go to https://console.cloud.google.com
2. Click the project dropdown at the top → **New Project** → name it "job-pipeline" → Create
3. In the left menu: **APIs & Services** → **Library**
4. Search "Google Drive API" → click it → **Enable**
5. Search "Gmail API" → click it → **Enable**
6. In the left menu: **APIs & Services** → **Credentials**
7. Click **+ Create Credentials** → **OAuth client ID**
8. If prompted to configure consent screen: click **Configure**, choose **External**, fill in your name and email, save
9. Back on Create Credentials: Application type = **Desktop app** → Name: "job-pipeline" → Create
10. Click **Download JSON** → save the file as `credentials.json` in this folder

---

## Step 6 — Set up Gmail App Password (one-time)

This is a special password just for this script (not your regular Gmail password).

1. Go to https://myaccount.google.com/security
2. Make sure **2-Step Verification** is ON (required)
3. Search for "App passwords" in the search bar
4. Click **App passwords** → create one → name it "job-pipeline"
5. Copy the 16-character password shown (example: `abcd efgh ijkl mnop` → enter without spaces)

---

## Step 7 — Clone this repo and configure it

In Terminal:
```bash
git clone https://github.com/smithar106/job-search-tool.git
cd job-search-tool
bash setup.sh
```

`setup.sh` will:
- Install Python dependencies
- Create your `.env` file from the template
- Walk you through Google authorization (a browser window will open — click Allow)

Then open the `.env` file and fill in your values:
```bash
nano .env
```

Fill in each line:
```
GEMINI_API_KEY=paste your key from Step 3
SENDER_EMAIL=your_gmail@gmail.com
GMAIL_APP_PASSWORD=paste your app password from Step 6 (no spaces)
DRIVE_FOLDER_ID=paste your folder ID from Step 4
RECIPIENT_EMAIL=ashleerthomas@gmail.com
```

Save with Ctrl+O, then Ctrl+X.

---

## Step 8 — Run the pipeline manually to test

```bash
python3 job_pipeline.py
```

Watch the output. After a minute or two you should see:
- Jobs being scraped from each source
- Scoring happening
- "Drive folder created" with a link
- "Email sent"

Check your inbox — you should have a morning summary email.

---

## Step 9 — Set it to run automatically every morning

Run this once to add a cron job (runs at 6am every day):
```bash
(crontab -l 2>/dev/null; echo "0 6 * * * cd $(pwd) && python3 job_pipeline.py >> data/pipeline.log 2>&1") | crontab -
```

To verify it was added:
```bash
crontab -l
```

---

## Step 10 — Build the LinkedIn adapter (highest value, ask Arthur)

LinkedIn has the most climate/ESG roles but requires a custom adapter.
Arthur's `~/linkedin-bot` has the Playwright code already — it just needs to be
packaged as an OpenCLI adapter using the `opencli-adapter-author` skill.

Once built, the pipeline automatically picks it up via the `linkedin` searches in `SEARCHES`.

Until then, the pipeline runs on Twitter, HackerNews, and Reddit — still useful.

---

## Daily workflow (once running)

Each morning you'll receive an email like this:

> **Job matches for 2026-04-26 — 3 roles ready**
>
> 1. Head of Sustainability @ Microsoft — 9/10  
>    Strong Scope 3 match, SBTi commitment aligns with your Oxfam work
>
> 2. Climate Finance Advisor @ World Bank — 8/10  
>    Direct USAID/MDB background match
>
> 3. ESG Supply Chain Lead @ Apple — 7/10  
>    Supplier engagement experience is a fit
>
> **Open cover letters in Google Drive →**

Click the Drive link → open the cover letter for each role → apply directly using the job URL.

---

## Troubleshooting

**"opencli: command not found"**
```bash
npm install -g @jackwener/opencli
```

**"Extension not connected"**
Make sure Chrome is open and the OpenCLI Bridge extension is enabled at `chrome://extensions`.

**"GEMINI_API_KEY not set"**
Run `nano .env` and make sure the key is filled in with no extra spaces.

**"credentials.json not found"**
Follow Step 5 again — the file needs to be in the same folder as `job_pipeline.py`.

**No jobs found**
- Make sure Chrome is open and you're logged into LinkedIn/Twitter
- Run `opencli hackernews top --limit 3` to verify OpenCLI is working
- Check `data/pipeline.log` for error messages

**Email not sending**
- Confirm the app password has no spaces
- Confirm 2-Step Verification is on for your Gmail account
- Try: `python3 -c "import smtplib; s=smtplib.SMTP_SSL('smtp.gmail.com',465); s.login('YOUR_EMAIL','YOUR_APP_PASS'); print('works')"`

---

## Files in this repo

```
job_pipeline.py      — main script
ashlee_resume.txt    — Ashlee's resume (used for scoring + cover letters)
test_pipeline.py     — automated tests
setup.sh             — one-time setup script
requirements.txt     — Python dependencies
.env.example         — environment variable template
.gitignore           — keeps secrets out of git
data/                — created automatically; stores seen jobs + logs
```

---

## Updating the resume

If Ashlee updates her resume, open `ashlee_resume.txt` and replace the content.
The pipeline always reads from that file at runtime.

---

## Running tests

```bash
python3 test_pipeline.py
```

All tests should pass without needing live API keys.
