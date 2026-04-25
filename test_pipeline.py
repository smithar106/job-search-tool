#!/usr/bin/env python3
"""
test_pipeline.py — Smoke tests for the job search pipeline.
Run: python3 test_pipeline.py
Tests each component in isolation without making live API calls.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure we can import from this directory
sys.path.insert(0, str(Path(__file__).parent))

import job_pipeline as p


# ─── Fixtures ────────────────────────────────────────────────────────────────

SAMPLE_JOB = {
    "title": "Head of Sustainability",
    "company": "Microsoft",
    "url": "https://example.com/job/123",
    "description": "Lead Scope 3 strategy and supplier engagement for global supply chain.",
    "location": "Remote",
    "source": "linkedin",
}

SAMPLE_SCORE = {"score": 8, "why": "Strong Scope 3 match.", "missing": "none"}


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestDeduplication(unittest.TestCase):

    def test_mark_and_load_seen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p.SEEN_FILE = Path(tmpdir) / "seen_jobs.csv"
            p.mark_seen([SAMPLE_JOB])
            seen = p.load_seen()
            self.assertIn(SAMPLE_JOB["url"], seen)

    def test_load_seen_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p.SEEN_FILE = Path(tmpdir) / "seen_jobs.csv"
            seen = p.load_seen()
            self.assertEqual(seen, set())

    def test_no_duplicate_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p.SEEN_FILE = Path(tmpdir) / "seen_jobs.csv"
            p.mark_seen([SAMPLE_JOB, SAMPLE_JOB])
            seen = p.load_seen()
            self.assertEqual(len(seen), 1)


class TestScraping(unittest.TestCase):

    @patch("job_pipeline.subprocess.run")
    def test_run_opencli_parses_json_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([SAMPLE_JOB]),
            stderr="",
        )
        results = p.run_opencli("linkedin", "jobs", "sustainability")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], SAMPLE_JOB["url"])

    @patch("job_pipeline.subprocess.run")
    def test_run_opencli_handles_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        results = p.run_opencli("linkedin", "jobs", "sustainability")
        self.assertEqual(results, [])

    @patch("job_pipeline.subprocess.run")
    def test_run_opencli_handles_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(returncode=69, stdout="", stderr="Browser not connected")
        results = p.run_opencli("linkedin", "jobs", "sustainability")
        self.assertEqual(results, [])

    @patch("job_pipeline.subprocess.run")
    def test_scrape_deduplicates(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([SAMPLE_JOB]),
            stderr="",
        )
        seen = {SAMPLE_JOB["url"]}
        results = p.scrape_jobs(seen)
        self.assertEqual(results, [])

    @patch("job_pipeline.subprocess.run")
    def test_scrape_normalizes_fields(self, mock_run):
        raw = {"name": "Climate Lead", "org": "WRI", "url": "https://x.com/1", "body": "JD text"}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps([raw]), stderr="")
        results = p.scrape_jobs(set())
        self.assertEqual(results[0]["title"], "Climate Lead")
        self.assertEqual(results[0]["company"], "WRI")


class TestScoring(unittest.TestCase):

    def _make_client(self, response_text):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text=response_text)
        return client

    def test_score_returns_dict(self):
        client = self._make_client(json.dumps(SAMPLE_SCORE))
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 8)
        self.assertIn("why", result)

    def test_score_handles_malformed_json(self):
        client = self._make_client("not json at all")
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 0)

    def test_score_handles_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("API error")
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 0)


class TestCoverLetter(unittest.TestCase):

    def test_cover_letter_returns_string(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="Dear Hiring Manager, ...")
        result = p.write_cover_letter(SAMPLE_JOB, SAMPLE_SCORE, "resume text", client)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 10)

    def test_cover_letter_handles_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("Quota exceeded")
        result = p.write_cover_letter(SAMPLE_JOB, SAMPLE_SCORE, "resume text", client)
        self.assertIn("failed", result.lower())


class TestEmail(unittest.TestCase):

    @patch("job_pipeline.smtplib.SMTP_SSL")
    def test_send_email_calls_smtp(self, mock_smtp_cls):
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        os.environ["SENDER_EMAIL"] = "test@gmail.com"
        os.environ["GMAIL_APP_PASSWORD"] = "testpass"

        p.SENDER_EMAIL    = "test@gmail.com"
        p.GMAIL_APP_PASS  = "testpass"
        p.RECIPIENT_EMAIL = "ashlee@gmail.com"

        matches = [{**SAMPLE_JOB, **SAMPLE_SCORE}]
        p.send_summary_email(matches, "https://drive.google.com/test", "2026-04-25")
        mock_smtp.send_message.assert_called_once()

    def test_send_email_skips_without_creds(self):
        p.SENDER_EMAIL   = ""
        p.GMAIL_APP_PASS = ""
        # Should not raise, just log and return
        p.send_summary_email([], "https://drive.google.com/test", "2026-04-25")


class TestResume(unittest.TestCase):

    def test_resume_file_exists(self):
        self.assertTrue(p.RESUME_FILE.exists(), "ashlee_resume.txt not found")

    def test_resume_not_empty(self):
        content = p.load_resume()
        self.assertGreater(len(content), 100)
        self.assertIn("ASHLEE", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
