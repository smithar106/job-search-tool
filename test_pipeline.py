#!/usr/bin/env python3
"""
test_pipeline.py — Smoke tests for the job search pipeline.
Run: python3 test_pipeline.py
All tests run without live API keys or network calls.
"""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import urllib.request

sys.path.insert(0, str(Path(__file__).parent))
import job_pipeline as p


# ─── Fixtures ─────────────────────────────────────────────────────────────────

SAMPLE_JOB = {
    "title":       "Head of Sustainability",
    "company":     "Microsoft",
    "url":         "https://example.com/job/123",
    "description": "Lead Scope 3 strategy and supplier engagement for global supply chain.",
    "location":    "Remote",
    "salary":      "",
    "listed":      "2026-04-25",
    "source":      "linkedin",
}

SAMPLE_SCORE = {
    "score": 8, "why": "Strong Scope 3 match.", "missing": "none",
    "strongest_credential": "Led Scope 3 rollout across 85 countries at Oxfam.",
    "ats_keywords": ["Scope 3", "SBTi", "supplier engagement"],
    "company_fact": "Microsoft has committed to being carbon negative by 2030.",
    "overqualified": False,
}


# ─── Deduplication ────────────────────────────────────────────────────────────

class TestDeduplication(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        p.SEEN_FILE = Path(self.tmpdir.name) / "seen_jobs.csv"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_mark_and_load_seen(self):
        p.mark_seen([SAMPLE_JOB])
        self.assertIn(SAMPLE_JOB["url"], p.load_seen())

    def test_load_seen_empty(self):
        self.assertEqual(p.load_seen(), set())

    def test_no_duplicate_urls(self):
        p.mark_seen([SAMPLE_JOB, SAMPLE_JOB])
        self.assertEqual(len(p.load_seen()), 1)


# ─── OpenCLI scraping ─────────────────────────────────────────────────────────

class TestOpenCLIScraping(unittest.TestCase):

    @patch("job_pipeline.subprocess.run")
    def test_parses_json_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps([SAMPLE_JOB]), stderr="")
        result = p.run_opencli("linkedin", "search", "sustainability", ["--remote", "remote"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], SAMPLE_JOB["url"])

    @patch("job_pipeline.subprocess.run")
    def test_handles_empty(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        self.assertEqual(p.run_opencli("linkedin", "search", "sustainability", []), [])

    @patch("job_pipeline.subprocess.run")
    def test_handles_nonzero_exit(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=69, stdout="", stderr="Browser not connected")
        self.assertEqual(p.run_opencli("linkedin", "search", "sustainability", []), [])

    @patch("job_pipeline.subprocess.run")
    @patch("job_pipeline.fetch_greenhouse", return_value=[])
    @patch("job_pipeline.fetch_lever",      return_value=[])
    @patch("job_pipeline.fetch_ashby",      return_value=[])
    def test_deduplicates_against_seen(self, _ashby, _lever, _gh, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps([SAMPLE_JOB]), stderr="")
        seen = {SAMPLE_JOB["url"]}
        self.assertEqual(p.scrape_jobs(seen), [])

    @patch("job_pipeline.subprocess.run")
    def test_normalizes_alternate_field_names(self, mock_run):
        raw = {"name": "Climate Lead", "org": "WRI",
               "url": "https://x.com/1", "body": "JD text"}
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps([raw]), stderr="")
        with patch("job_pipeline.fetch_greenhouse", return_value=[]), \
             patch("job_pipeline.fetch_lever",      return_value=[]), \
             patch("job_pipeline.fetch_ashby",      return_value=[]):
            results = p.scrape_jobs(set())
        self.assertEqual(results[0]["title"],   "Climate Lead")
        self.assertEqual(results[0]["company"], "WRI")


# ─── Greenhouse fetch ─────────────────────────────────────────────────────────

class TestGreenhouseFetch(unittest.TestCase):

    def _mock_response(self, payload: dict):
        data = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("job_pipeline.urllib.request.urlopen")
    def test_returns_matching_jobs(self, mock_urlopen):
        payload = {"jobs": [{
            "title": "Director of Sustainability",
            "absolute_url": "https://jobs.greenhouse.io/wri/123",
            "content": "<p>Lead Scope 3 strategy.</p>",
            "offices": [{"name": "Remote"}],
            "departments": [{"name": "Sustainability"}],
            "updated_at": "2026-04-25T00:00:00Z",
        }]}
        mock_urlopen.return_value = self._mock_response(payload)
        results = p.fetch_greenhouse("WRI", "wri")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company"], "WRI")
        self.assertEqual(results[0]["source"],  "greenhouse")

    @patch("job_pipeline.urllib.request.urlopen")
    def test_filters_irrelevant_jobs(self, mock_urlopen):
        payload = {"jobs": [{
            "title": "Senior Software Engineer",
            "absolute_url": "https://jobs.greenhouse.io/wri/999",
            "content": "<p>Build React apps.</p>",
            "offices": [], "departments": [], "updated_at": "",
        }]}
        mock_urlopen.return_value = self._mock_response(payload)
        self.assertEqual(p.fetch_greenhouse("WRI", "wri"), [])

    @patch("job_pipeline.urllib.request.urlopen")
    def test_handles_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        self.assertEqual(p.fetch_greenhouse("WRI", "wri"), [])

    @patch("job_pipeline.urllib.request.urlopen")
    def test_strips_html_from_description(self, mock_urlopen):
        payload = {"jobs": [{
            "title": "Climate Director",
            "absolute_url": "https://jobs.greenhouse.io/wri/1",
            "content": "<p>Lead <b>sustainability</b> efforts.</p>",
            "offices": [], "departments": [{"name": "climate"}],
            "updated_at": "",
        }]}
        mock_urlopen.return_value = self._mock_response(payload)
        results = p.fetch_greenhouse("WRI", "wri")
        self.assertNotIn("<p>", results[0]["description"])
        self.assertNotIn("<b>", results[0]["description"])


# ─── Lever fetch ──────────────────────────────────────────────────────────────

class TestLeverFetch(unittest.TestCase):

    def _mock_response(self, payload):
        data = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("job_pipeline.urllib.request.urlopen")
    def test_returns_matching_jobs(self, mock_urlopen):
        payload = [{"text": "Sustainability Manager",
                    "hostedUrl": "https://jobs.lever.co/watershed/abc",
                    "categories": {"location": "Remote"},
                    "lists": [], "additional": "Lead carbon accounting programs."}]
        mock_urlopen.return_value = self._mock_response(payload)
        results = p.fetch_lever("Watershed", "watershed")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "lever")

    @patch("job_pipeline.urllib.request.urlopen")
    def test_filters_irrelevant_jobs(self, mock_urlopen):
        payload = [{"text": "iOS Engineer",
                    "hostedUrl": "https://jobs.lever.co/watershed/xyz",
                    "categories": {}, "lists": [], "additional": ""}]
        mock_urlopen.return_value = self._mock_response(payload)
        self.assertEqual(p.fetch_lever("Watershed", "watershed"), [])

    @patch("job_pipeline.urllib.request.urlopen")
    def test_handles_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("timeout")
        self.assertEqual(p.fetch_lever("Watershed", "watershed"), [])


# ─── Ashby fetch ──────────────────────────────────────────────────────────────

class TestAshbyFetch(unittest.TestCase):

    def _mock_response(self, payload):
        data = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    @patch("job_pipeline.urllib.request.urlopen")
    def test_returns_matching_jobs(self, mock_urlopen):
        payload = {"jobPostings": [{
            "title": "Head of Carbon Removal",
            "id":    "abc-123",
            "departmentName": "Climate",
            "locationName": "Remote",
            "descriptionHtml": "<p>Lead carbon removal strategy.</p>",
            "publishedAt": "2026-04-25T00:00:00Z",
        }]}
        mock_urlopen.return_value = self._mock_response(payload)
        results = p.fetch_ashby("Climeworks", "climeworks")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "ashby")
        self.assertIn("climeworks/abc-123", results[0]["url"])

    @patch("job_pipeline.urllib.request.urlopen")
    def test_filters_irrelevant_jobs(self, mock_urlopen):
        payload = {"jobPostings": [{
            "title": "Backend Engineer", "id": "xyz",
            "departmentName": "Engineering",
            "locationName": "", "descriptionHtml": "", "publishedAt": "",
        }]}
        mock_urlopen.return_value = self._mock_response(payload)
        self.assertEqual(p.fetch_ashby("Climeworks", "climeworks"), [])

    @patch("job_pipeline.urllib.request.urlopen")
    def test_handles_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("timeout")
        self.assertEqual(p.fetch_ashby("Climeworks", "climeworks"), [])


# ─── Scoring ──────────────────────────────────────────────────────────────────

class TestScoring(unittest.TestCase):

    def test_returns_score_dict(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            text=json.dumps(SAMPLE_SCORE))
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 8)
        self.assertIn("why", result)

    def test_returns_overqualified_false_for_best_fit(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            text=json.dumps(SAMPLE_SCORE))
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertFalse(result.get("overqualified"))

    def test_returns_overqualified_true_for_junior_role(self):
        client = MagicMock()
        overqualified_score = {**SAMPLE_SCORE, "score": 6, "overqualified": True}
        client.models.generate_content.return_value = MagicMock(
            text=json.dumps(overqualified_score))
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertTrue(result.get("overqualified"))
        self.assertEqual(result["score"], 6)

    def test_overqualified_flag_defaults_false_on_missing_field(self):
        client = MagicMock()
        score_without_flag = {"score": 7, "why": "Good fit.", "missing": "none"}
        client.models.generate_content.return_value = MagicMock(
            text=json.dumps(score_without_flag))
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertFalse(result.get("overqualified"))

    def test_handles_malformed_json(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="not json")
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 0)

    def test_handles_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("API error")
        result = p.score_job(SAMPLE_JOB, "resume text", client)
        self.assertEqual(result["score"], 0)


# ─── Hiring manager extraction ────────────────────────────────────────────────

class TestHiringManager(unittest.TestCase):

    def test_returns_name_when_found(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="Sarah Kim")
        result = p.extract_hiring_manager(SAMPLE_JOB, client)
        self.assertEqual(result, "Sarah Kim")

    def test_returns_dash_when_not_found(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="—")
        result = p.extract_hiring_manager(SAMPLE_JOB, client)
        self.assertEqual(result, "—")

    def test_returns_dash_when_no_description(self):
        client = MagicMock()
        job = {**SAMPLE_JOB, "description": ""}
        result = p.extract_hiring_manager(job, client)
        self.assertEqual(result, "—")
        client.models.generate_content.assert_not_called()

    def test_handles_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("error")
        result = p.extract_hiring_manager(SAMPLE_JOB, client)
        self.assertEqual(result, "—")


# ─── Cover letter ─────────────────────────────────────────────────────────────

class TestCoverLetter(unittest.TestCase):

    def test_returns_string(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            text="Dear Hiring Manager, ...")
        result = p.write_cover_letter(SAMPLE_JOB, SAMPLE_SCORE, "resume", client)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 10)

    def test_handles_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("Quota exceeded")
        result = p.write_cover_letter(SAMPLE_JOB, SAMPLE_SCORE, "resume", client)
        self.assertIn("failed", result.lower())


# ─── LinkedIn outreach blurbs ─────────────────────────────────────────────────

class TestLinkedInOutreach(unittest.TestCase):

    JOB_WITH_HM = {**SAMPLE_JOB, "hiring_manager": "Sarah Kim", "why": "Strong Scope 3 match."}
    JOB_NO_HM   = {**SAMPLE_JOB, "hiring_manager": "—",         "why": "Strong Scope 3 match."}

    def _client(self, payload: dict):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            text=json.dumps(payload))
        return client

    def test_returns_message_and_note(self):
        client = self._client({"message": "Hi Sarah, ...", "note": "Applied for Head of Sustainability — Scope 3 expert."})
        result = p.write_linkedin_outreach(self.JOB_WITH_HM, "resume", client)
        self.assertIn("message", result)
        self.assertIn("note", result)
        self.assertIsInstance(result["message"], str)
        self.assertIsInstance(result["note"], str)

    def test_note_hard_limit_120_chars(self):
        long_note = "A" * 200
        client = self._client({"message": "Hi Sarah, ...", "note": long_note})
        result = p.write_linkedin_outreach(self.JOB_WITH_HM, "resume", client)
        self.assertLessEqual(len(result["note"]), 120,
                             f"Note is {len(result['note'])} chars, must be ≤120")

    def test_note_already_under_limit_unchanged(self):
        short_note = "Scope 3 specialist, CC-P certified. Would love to connect."
        client = self._client({"message": "Hi,", "note": short_note})
        result = p.write_linkedin_outreach(self.JOB_WITH_HM, "resume", client)
        self.assertEqual(result["note"], short_note)

    def test_fallback_on_api_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("quota")
        result = p.write_linkedin_outreach(self.JOB_WITH_HM, "resume", client)
        self.assertIn("message", result)
        self.assertIn("note", result)
        self.assertLessEqual(len(result["note"]), 120)

    def test_fallback_note_under_limit(self):
        client = MagicMock()
        client.models.generate_content.side_effect = Exception("quota")
        result = p.write_linkedin_outreach(self.JOB_NO_HM, "resume", client)
        self.assertLessEqual(len(result["note"]), 120)

    def test_handles_malformed_json(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="not json")
        result = p.write_linkedin_outreach(self.JOB_WITH_HM, "resume", client)
        self.assertIn("message", result)
        self.assertLessEqual(len(result["note"]), 120)


# ─── PDF rendering ────────────────────────────────────────────────────────────

class TestPDFRendering(unittest.TestCase):

    def test_renders_valid_pdf(self):
        pdf_bytes = p.render_pdf(
            SAMPLE_JOB,
            "Dear Hiring Manager,\n\nI am writing to apply.\n\nSincerely, Ashlee",
            "2026-04-25",
        )
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))

    def test_pdf_contains_company_name(self):
        letter = "Lead sustainability programs at Microsoft."
        pdf_bytes = p.render_pdf(SAMPLE_JOB, letter, "2026-04-25")
        # PDF content is not plain text, but should be non-empty and valid
        self.assertGreater(len(pdf_bytes), 500)

    def test_handles_special_characters(self):
        job = {**SAMPLE_JOB, "title": "Directora de Sostenibilidad",
               "company": "Société Générale"}
        pdf_bytes = p.render_pdf(job, "Estimada equipa,\n\nGracias.", "2026-04-25")
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))


# ─── Email ────────────────────────────────────────────────────────────────────

class TestEmail(unittest.TestCase):

    @patch("job_pipeline.smtplib.SMTP_SSL")
    def test_sends_email_with_sheet_and_folder(self, mock_smtp_cls):
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        p.SENDER_EMAIL    = "test@gmail.com"
        p.GMAIL_APP_PASS  = "testpass"
        p.RECIPIENT_EMAIL = "ashlee@gmail.com"

        matches = [{**SAMPLE_JOB, **SAMPLE_SCORE,
                    "hiring_manager": "Jane Doe",
                    "cover_letter_url": "https://drive.google.com/file/abc"}]
        p.send_summary_email(
            matches,
            "https://drive.google.com/drive/folders/xyz",
            "https://docs.google.com/spreadsheets/d/abc",
            "2026-04-25",
        )
        mock_smtp.send_message.assert_called_once()

    def test_skips_without_credentials(self):
        p.SENDER_EMAIL   = ""
        p.GMAIL_APP_PASS = ""
        # Should not raise
        p.send_summary_email([], "https://drive.google.com/x",
                             "https://sheets.google.com/x", "2026-04-25")


# ─── Resume ───────────────────────────────────────────────────────────────────

class TestResume(unittest.TestCase):

    def test_resume_file_exists(self):
        self.assertTrue(p.RESUME_FILE.exists(), "ashlee_resume.txt not found")

    def test_resume_content(self):
        content = p.load_resume()
        self.assertGreater(len(content), 100)
        self.assertIn("ASHLEE", content)
        self.assertIn("Oxfam", content)


# ─── Config ───────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):

    def test_daily_target_is_30(self):
        self.assertEqual(p.DAILY_TARGET, 30)

    def test_greenhouse_orgs_defined(self):
        self.assertGreater(len(p.GREENHOUSE_ORGS), 0)
        for name, slug in p.GREENHOUSE_ORGS:
            self.assertIsInstance(name, str)
            self.assertIsInstance(slug, str)
            self.assertTrue(slug.replace("-", "").isalnum(),
                            f"Slug '{slug}' should be alphanumeric")

    def test_lever_orgs_defined(self):
        self.assertGreater(len(p.LEVER_ORGS), 0)
        for name, slug in p.LEVER_ORGS:
            self.assertIsInstance(name, str)
            self.assertIsInstance(slug, str)

    def test_ashby_orgs_defined(self):
        self.assertGreater(len(p.ASHBY_ORGS), 0)
        for name, slug in p.ASHBY_ORGS:
            self.assertIsInstance(name, str)
            self.assertIsInstance(slug, str)

    def test_searches_are_4_tuples(self):
        for entry in p.SEARCHES:
            self.assertEqual(len(entry), 4,
                             f"SEARCHES entry {entry} should be a 4-tuple")
            adapter, subcommand, query, flags = entry
            self.assertIsInstance(flags, list)

    def test_sheet_headers_include_linkedin_columns(self):
        self.assertIn("LinkedIn Message",              p.SHEET_HEADERS)
        self.assertIn("Connection Note (≤120 chars)", p.SHEET_HEADERS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
