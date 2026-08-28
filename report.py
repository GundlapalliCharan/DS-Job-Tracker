import json
from datetime import datetime
from config import IST, logger
from database import get_todays_jobs

def get_deadline_status(deadline: str, deadline_iso: str = None) -> str:
    """
    Evaluates whether a job's deadline is ACTIVE, PASSED, or UNKNOWN using IST timezone.
    Returns: "ACTIVE", "PASSED", or "UNKNOWN"
    """
    now = datetime.now(IST)

    # 1. Try deadline_iso ISO 8601 parsing first
    if deadline_iso:
        try:
            parsed_iso = datetime.fromisoformat(deadline_iso.strip())
            if parsed_iso.tzinfo is None:
                parsed_iso = parsed_iso.replace(tzinfo=IST)
            return "PASSED" if parsed_iso < now else "ACTIVE"
        except Exception:
            pass

    # 2. Fallback to parsing original deadline text
    if not deadline:
        return "UNKNOWN"

    deadline_text = deadline.strip().lower()
    unknown_keywords = ["null", "none", "unknown", "not specified", "not mentioned", "n/a", "tbd"]
    if deadline_text in unknown_keywords:
        return "UNKNOWN"

    # Remove common ordinal suffixes and separators for strptime parsing
    cleaned = deadline_text.replace("|", " ")
    for suffix in ["1st", "2nd", "3rd", "th", "st", "nd", "rd"]:
        cleaned = cleaned.replace(suffix, "")

    formats = [
        "%d %B %I %p", "%d %B %I:%M %p",
        "%d %b %I %p", "%d %b %I:%M %p",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned.strip(), fmt)
            if parsed.year == 1900:
                parsed = parsed.replace(year=now.year)
            parsed = parsed.replace(tzinfo=IST)
            return "PASSED" if parsed < now else "ACTIVE"
        except ValueError:
            continue

    return "UNKNOWN"

def format_job(job: dict, index: int, show_details: bool = True) -> str:
    """Formats a single job record dictionary for Discord markdown rendering."""
    lines = []
    lines.append(f"**{index}. {job['title']}**")

    if job.get("company"):
        lines.append(f"🏢 {job['company']}")

    lines.append(f"⭐ Relevance: **{job.get('relevance_score', 0)}/100**")

    # Location handling
    loc_raw = job.get("location")
    if loc_raw:
        try:
            loc_list = json.loads(loc_raw) if isinstance(loc_raw, str) else loc_raw
            lines.append(f"📍 {', '.join(loc_list)}")
        except Exception:
            lines.append(f"📍 {loc_raw}")

    if job.get("work_type"):
        lines.append(f"💼 {job['work_type']}")

    if job.get("salary"):
        lines.append(f"💰 {job['salary']}")

    # Skills handling
    skills_raw = job.get("skills")
    if skills_raw:
        try:
            skills_list = json.loads(skills_raw) if isinstance(skills_raw, str) else skills_raw
            lines.append(f"🛠️ {', '.join(skills_list)}")
        except Exception:
            lines.append(f"🛠️ {skills_raw}")

    # Deadline status checking at report rendering time
    deadline_text = job.get("deadline")
    deadline_iso = job.get("deadline_iso")
    status = get_deadline_status(deadline_text, deadline_iso)

    if deadline_text:
        if status == "PASSED":
            lines.append(f"⏰ Deadline: {deadline_text} **(PASSED / EXPIRED)**")
        else:
            lines.append(f"⏰ Deadline: {deadline_text}")
    elif status == "PASSED":
        lines.append("⏰ Deadline: **PASSED / EXPIRED**")

    if job.get("application_url"):
        lines.append(f"🔗 {job['application_url']}")

    if job.get("relevance_reason"):
        lines.append(f"💡 {job['relevance_reason']}")

    if show_details:
        skill_match = job.get("skill_match_score") or 0
        lines.append(f"🧩 Skill Match: **{skill_match}/100**")

        eligibility = job.get("eligibility") or "Unknown"
        if eligibility == "Eligible":
            lines.append(f"✅ Eligibility: **{eligibility}**")
        elif eligibility == "Not Eligible":
            lines.append(f"❌ Eligibility: **{eligibility}**")
        else:
            lines.append(f"❓ Eligibility: **{eligibility}**")

        if job.get("eligibility_reason"):
            lines.append(f"⚠️ {job['eligibility_reason']}")

        missing_raw = job.get("missing_skills")
        if missing_raw:
            try:
                missing_list = json.loads(missing_raw) if isinstance(missing_raw, str) else missing_raw
                if missing_list:
                    lines.append(f"📚 Missing skills: {', '.join(missing_list)}")
            except Exception:
                if missing_raw:
                    lines.append(f"📚 Missing skills: {missing_raw}")

        rec = job.get("recommendation") or "CONSIDER"
        lines.append(f"👉 Recommendation: **{rec}**")

    lines.append("────────────────────")
    return "\n".join(lines)

def create_daily_report(date_str: str = None, db_name: str = None) -> str:
    """Generates the formatted daily report string for all jobs on the specified date."""
    jobs_rows = get_todays_jobs(date_str, db_name)
    jobs = [dict(row) for row in jobs_rows]

    now_ist = datetime.now(IST)
    display_date = date_str if date_str else now_ist.strftime("%d %B %Y")

    if not jobs:
        return (
            f"📊 **DAILY JOB REPORT**\n"
            f"📅 {display_date}\n\n"
            f"No jobs were collected today."
        )

    # Sort jobs:
    # 1. Recommendation priority: APPLY -> CONSIDER -> SKIP
    # 2. Relevance Score DESC
    # 3. Skill Match Score DESC
    # 4. Deadline ISO ASC
    rec_order = {"APPLY": 0, "CONSIDER": 1, "SKIP": 2}
    
    def job_sort_key(j):
        rec_val = rec_order.get((j.get("recommendation") or "CONSIDER").upper(), 1)
        rel_val = -(j.get("relevance_score") or 0)
        skill_val = -(j.get("skill_match_score") or 0)
        d_iso = j.get("deadline_iso") or "9999-99-99"
        return (rec_val, rel_val, skill_val, d_iso)

    sorted_jobs = sorted(jobs, key=job_sort_key)

    apply_jobs = [j for j in sorted_jobs if (j.get("recommendation") or "CONSIDER").upper() == "APPLY"]
    consider_jobs = [j for j in sorted_jobs if (j.get("recommendation") or "CONSIDER").upper() == "CONSIDER"]
    skip_jobs = [j for j in sorted_jobs if (j.get("recommendation") or "CONSIDER").upper() == "SKIP"]

    report_lines = [
        "📊 **DAILY JOB REPORT**",
        f"📅 {display_date}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📈 **SUMMARY**",
        f"Total job roles: **{len(jobs)}**",
        f"🔥 Apply: **{len(apply_jobs)}**",
        f"🟡 Consider: **{len(consider_jobs)}**",
        f"⚪ Skip: **{len(skip_jobs)}**"
    ]

    if apply_jobs:
        report_lines.extend(["", "🔥 **HIGH PRIORITY — APPLY**", "━━━━━━━━━━━━━━━━━━━━"])
        for idx, job in enumerate(apply_jobs, 1):
            report_lines.append(format_job(job, idx, show_details=True))

    if consider_jobs:
        report_lines.extend(["", "🟡 **MEDIUM PRIORITY — CONSIDER**", "━━━━━━━━━━━━━━━━━━━━"])
        for idx, job in enumerate(consider_jobs, 1):
            report_lines.append(format_job(job, idx, show_details=True))

    if skip_jobs:
        report_lines.extend(["", "⚪ **LOW PRIORITY — SKIP**", "━━━━━━━━━━━━━━━━━━━━"])
        for idx, job in enumerate(skip_jobs, 1):
            report_lines.append(format_job(job, idx, show_details=True))

    return "\n".join(report_lines)

