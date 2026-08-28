import json
import re
import time
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL, load_and_validate_profile, logger

# Initialize Gemini Client
profile_data = load_and_validate_profile()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

class Job(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    location: List[str] = Field(default_factory=list)
    work_type: Optional[str] = None
    experience: Optional[str] = None
    qualification: Optional[str] = None
    passing_year: Optional[str] = None
    cgpa: Optional[str] = None
    aggregate: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    salary: Optional[str] = None
    bond: Optional[str] = None
    deadline: Optional[str] = None
    deadline_iso: Optional[str] = None
    application_url: Optional[str] = None
    relevance_score: int = 0
    relevance_reason: Optional[str] = None
    skill_match_score: int = 0
    eligibility: str = "Unknown"
    eligibility_reason: Optional[str] = None
    missing_skills: List[str] = Field(default_factory=list)
    recommendation: str = "CONSIDER"

class JobAnalysis(BaseModel):
    is_job: bool
    company: Optional[str] = None
    organizer: Optional[str] = None
    jobs: List[Job] = Field(default_factory=list)

def clean_url(raw_url: Optional[str]) -> Optional[str]:
    """Extracts raw URL if Gemini returned markdown link syntax [text](url)."""
    if not raw_url:
        return None
    raw_url = raw_url.strip()
    match = re.search(r'\[.*?\]\((https?://[^\s\)]+)\)', raw_url)
    if match:
        return match.group(1)
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    return raw_url

def enforce_safety_and_normalization(analysis: JobAnalysis) -> JobAnalysis:
    """Applies Python business logic and range constraints to Gemini output."""
    if not analysis.is_job:
        analysis.jobs = []
        return analysis

    valid_eligibility = {"Eligible", "Not Eligible", "Unknown"}
    valid_recommendation = {"APPLY", "CONSIDER", "SKIP"}

    for job in analysis.jobs:
        # Enforce range limits [0, 100]
        job.relevance_score = max(0, min(100, int(job.relevance_score or 0)))
        job.skill_match_score = max(0, min(100, int(job.skill_match_score or 0)))

        # Normalize eligibility
        if job.eligibility not in valid_eligibility:
            job.eligibility = "Unknown"

        # Normalize recommendation
        if job.recommendation not in valid_recommendation:
            job.recommendation = "CONSIDER"

        # Clean URL
        job.application_url = clean_url(job.application_url)

        # Deterministic Business Rule:
        # If candidate is NOT ELIGIBLE, recommendation MUST be SKIP regardless of LLM score
        if job.eligibility == "Not Eligible":
            job.recommendation = "SKIP"

    return analysis

def analyze_job_with_retry(content: str, max_retries: int = 3) -> JobAnalysis:
    """Sends job content to Gemini API with retries and exponential backoff."""
    prompt = f"""
You are an intelligent job-post analyzer helping a Data Science student find suitable jobs.

==============================
STUDENT PROFILE
==============================

{json.dumps(profile_data, indent=2)}

==============================
JOB POST
==============================

{content}

==============================
YOUR TASK
==============================

Analyze the job post and return structured JSON conforming to the schema.

1. Determine whether the message represents a genuine hiring opportunity:
   - Job / Full-time role
   - Internship
   - Recruitment drive / Placement drive
   - Off-campus / On-campus hiring

   CRITICAL NON-JOB RULE:
   Messages that are conversational or administrative such as:
   "hi", "hello", "thanks", "thank you", "acknowledged", "okay", "good morning", "noted"
   MUST return:
   "is_job": false,
   "jobs": []

2. If it is NOT a job opportunity:
   - Set is_job to false
   - Return an empty jobs list []

3. If it IS a job opportunity:
   - Extract company
   - Extract organizer if mentioned
   - Identify EVERY distinct role listed in the post.
   - Create a separate Job object in the jobs list for EACH distinct role.

4. Never invent information. If a field is not explicitly mentioned or clearly inferable, use null or empty list.

==============================
RELEVANCE SCORE (0-100)
==============================

Score each role's career alignment with Data Science:
90-100: Data Scientist, Machine Learning Engineer, AI Engineer, NLP / Deep Learning Specialist.
70-89: Data Analyst, Business Intelligence, Python Data Engineer, AI Software Developer.
40-69: Python Developer, Backend Developer, General Software Engineer.
20-39: DevOps, Cloud Engineer, System Admin, General IT.
0-19: Sales, HR, Marketing, or completely unrelated roles.

==============================
SKILL MATCH & ELIGIBILITY
==============================

- Compare required skills with student profile.
- Return skill_match_score (0-100) and missing_skills list.
- Check mandatory eligibility requirements (Degree, Branch, Passing Year, CGPA, Experience).
- Return eligibility ("Eligible", "Not Eligible", "Unknown") and eligibility_reason.

==============================
RECOMMENDATION & DEADLINE
==============================

- Return recommendation ("APPLY", "CONSIDER", "SKIP").
  * APPLY: High relevance AND Eligible.
  * CONSIDER: Relevant but moderate match or unknown eligibility.
  * SKIP: Not Eligible OR low relevance.

- deadline: Preserve original wording (e.g. "25th Aug | 6pm").
- deadline_iso: Convert to ISO 8601 string (e.g. "2026-08-25T18:00:00") assuming current year if missing. If uncertain, set to null.

Return ONLY structured JSON adhering strictly to the response schema.
"""

    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"GEMINI_REQUEST attempt {attempt}/{max_retries}")
            
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": JobAnalysis,
                },
            )

            if not response.text or not response.text.strip():
                raise ValueError("Gemini returned an empty text response.")

            parsed_analysis = JobAnalysis.model_validate_json(response.text)
            logger.info("GEMINI_SUCCESS: Valid response parsed successfully.")
            
            # Post-process with Python safety rules
            return enforce_safety_and_normalization(parsed_analysis)

        except Exception as e:
            last_exception = e
            logger.warning(f"GEMINI_RETRY: Attempt {attempt} failed with error: {e}")
            if attempt < max_retries:
                backoff_time = 2 ** attempt
                logger.info(f"Waiting {backoff_time}s before next Gemini retry...")
                time.sleep(backoff_time)

    logger.error(f"GEMINI_FAILURE: All {max_retries} retries exhausted.")
    raise RuntimeError(f"Gemini API processing failed after {max_retries} attempts: {last_exception}")

