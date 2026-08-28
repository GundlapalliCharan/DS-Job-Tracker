import os
import unittest
from unittest.mock import MagicMock, patch
import tempfile
import json
from datetime import datetime, timedelta

from config import IST
from database import (
    init_database,
    save_job,
    get_message_status,
    set_message_status,
    get_todays_jobs,
    get_bot_state,
    set_bot_state,
    get_db_connection
)
from gemini_service import Job, JobAnalysis, enforce_safety_and_normalization, analyze_job_with_retry
from report import get_deadline_status, create_daily_report
from bot import send_long_message

class TestDSJobTracker(unittest.TestCase):

    def setUp(self):
        """Set up a temporary database file for isolated testing."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        init_database(self.db_path)

    def tearDown(self):
        """Clean up the temporary database file after each test."""
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except PermissionError:
                pass

    def test_database_initialization_and_migration(self):
        """Verify tables and columns are created properly."""
        with get_db_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(jobs)")
            cols = [row["name"] for row in cursor.fetchall()]
            
            # Check mandatory new/migrated columns
            self.assertIn("discord_message_id", cols)
            self.assertIn("deadline_iso", cols)
            self.assertIn("skill_match_score", cols)
            self.assertIn("eligibility", cols)
            self.assertIn("missing_skills", cols)
            self.assertIn("recommendation", cols)

            cursor.execute("PRAGMA table_info(processed_messages)")
            proc_cols = [row["name"] for row in cursor.fetchall()]
            self.assertIn("discord_message_id", proc_cols)
            self.assertIn("status", proc_cols)

    def test_job_insertion_single_and_multi_role(self):
        """Verify single and multiple roles save correctly under same discord_message_id."""
        job1 = Job(
            title="Data Scientist",
            category="Data Science",
            location=["Bengaluru", "Remote"],
            work_type="Full Time",
            experience="Fresher",
            qualification="B.Tech CSE",
            skills=["Python", "Machine Learning", "SQL"],
            salary="12 LPA",
            deadline="30th Dec | 5pm",
            deadline_iso="2026-12-30T17:00:00",
            application_url="https://example.com/apply1",
            relevance_score=95,
            relevance_reason="Matches profile",
            skill_match_score=90,
            eligibility="Eligible",
            eligibility_reason="Meets degree requirement",
            missing_skills=[],
            recommendation="APPLY"
        )

        job2 = Job(
            title="ML Engineer",
            category="AI / ML",
            location=["Hyderabad"],
            work_type="Full Time",
            skills=["Python", "PyTorch"],
            salary="15 LPA",
            relevance_score=90,
            skill_match_score=85,
            eligibility="Eligible",
            recommendation="APPLY"
        )

        msg_id = "1234567890"
        save_job(job1, "Company A", "HR John", "Original post content", msg_id, db_name=self.db_path)
        save_job(job2, "Company A", "HR John", "Original post content", msg_id, db_name=self.db_path)

        todays_jobs = get_todays_jobs(db_name=self.db_path)
        self.assertEqual(len(todays_jobs), 2)
        self.assertEqual(todays_jobs[0]["discord_message_id"], msg_id)
        self.assertEqual(todays_jobs[1]["discord_message_id"], msg_id)

    def test_message_idempotency(self):
        """Verify processed_messages tracks status correctly."""
        msg_id = "9876543210"
        self.assertIsNone(get_message_status(msg_id, db_name=self.db_path))

        set_message_status(msg_id, "QUEUED", db_name=self.db_path)
        self.assertEqual(get_message_status(msg_id, db_name=self.db_path), "QUEUED")

        set_message_status(msg_id, "PROCESSING", db_name=self.db_path)
        self.assertEqual(get_message_status(msg_id, db_name=self.db_path), "PROCESSING")

        set_message_status(msg_id, "COMPLETED", db_name=self.db_path)
        self.assertEqual(get_message_status(msg_id, db_name=self.db_path), "COMPLETED")

    def test_deadline_status_evaluation(self):
        """Verify ISO and natural language deadline evaluation."""
        now = datetime.now(IST)

        # Future ISO date
        future_iso = (now + timedelta(days=5)).isoformat()
        self.assertEqual(get_deadline_status("In 5 days", future_iso), "ACTIVE")

        # Past ISO date
        past_iso = (now - timedelta(days=2)).isoformat()
        self.assertEqual(get_deadline_status("2 days ago", past_iso), "PASSED")

        # Unknown / Null
        self.assertEqual(get_deadline_status(None, None), "UNKNOWN")
        self.assertEqual(get_deadline_status("Not mentioned", None), "UNKNOWN")

    def test_python_recommendation_safety_rules(self):
        """Verify Not Eligible rule enforces recommendation = SKIP."""
        analysis = JobAnalysis(
            is_job=True,
            company="Tech Corp",
            jobs=[
                Job(
                    title="Senior Data Scientist",
                    relevance_score=100,
                    skill_match_score=90,
                    eligibility="Not Eligible",
                    eligibility_reason="Requires 5+ years experience",
                    recommendation="APPLY", # LLM incorrectly suggested APPLY
                    application_url="[apply](https://example.com/job)"
                )
            ]
        )

        normalized = enforce_safety_and_normalization(analysis)
        job = normalized.jobs[0]
        self.assertEqual(job.recommendation, "SKIP") # Must be overridden to SKIP
        self.assertEqual(job.application_url, "https://example.com/job") # Cleaned markdown link

    def test_daily_report_generation(self):
        """Verify empty and populated daily report formatting."""
        empty_report = create_daily_report(db_name=self.db_path)
        self.assertIn("No jobs were collected today", empty_report)

        job = Job(
            title="Data Analyst",
            location=["Bengaluru"],
            relevance_score=80,
            skill_match_score=75,
            eligibility="Eligible",
            recommendation="CONSIDER"
        )
        save_job(job, "Analytics Inc", None, "post", "111222", db_name=self.db_path)

        report = create_daily_report(db_name=self.db_path)
        self.assertIn("Total job roles: **1**", report)
        self.assertIn("Data Analyst", report)
        self.assertIn("Analytics Inc", report)

    def test_send_long_message_chunking(self):
        """Verify long message splitting into valid chunks <= 1900 chars."""
        mock_destination = MagicMock()
        mock_destination.send = unittest.mock.AsyncMock()

        long_text = ("Line of content for job report\n" * 150) # > 3000 chars
        import asyncio
        asyncio.run(send_long_message(mock_destination, long_text))

        self.assertGreater(mock_destination.send.call_count, 1)
        for call_args in mock_destination.send.call_args_list:
            arg = call_args[0][0]
            self.assertLessEqual(len(arg), 1900)

    @patch("gemini_service.gemini_client.models.generate_content")
    def test_gemini_retry_on_failure(self, mock_generate):
        """Verify retry logic when Gemini raises an API exception."""
        mock_generate.side_effect = Exception("API Quota Error")

        with self.assertRaises(RuntimeError):
            analyze_job_with_retry("Sample job text", max_retries=2)

        self.assertEqual(mock_generate.call_count, 2)

if __name__ == "__main__":
    unittest.main()

