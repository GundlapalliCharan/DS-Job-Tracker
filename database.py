import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
from config import DB_NAME, IST, logger

@contextmanager
def get_db_connection(db_name=None):
    """Context manager for safe SQLite connections with WAL mode and busy timeout."""
    target_db = db_name if db_name is not None else DB_NAME
    connection = sqlite3.connect(target_db, timeout=30.0)
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
    finally:
        connection.close()

def init_database(db_name=None):
    """Initializes jobs, processed_messages, and bot_state tables with indexes and migrations."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()

        # Create jobs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                discord_message_id TEXT,
                company TEXT,
                organizer TEXT,
                title TEXT,
                category TEXT,
                location TEXT,
                work_type TEXT,
                experience TEXT,
                qualification TEXT,
                passing_year TEXT,
                cgpa TEXT,
                aggregate TEXT,
                skills TEXT,
                salary TEXT,
                bond TEXT,
                deadline TEXT,
                deadline_iso TEXT,
                application_url TEXT,
                relevance_score INTEGER,
                relevance_reason TEXT,
                skill_match_score INTEGER DEFAULT 0,
                eligibility TEXT,
                eligibility_reason TEXT,
                missing_skills TEXT,
                recommendation TEXT,
                original_post TEXT
            )
        """)

        # Migration check for jobs table
        cursor.execute("PRAGMA table_info(jobs)")
        existing_columns = [row["name"] for row in cursor.fetchall()]

        column_migrations = {
            "discord_message_id": "TEXT",
            "deadline_iso": "TEXT",
            "skill_match_score": "INTEGER DEFAULT 0",
            "eligibility": "TEXT",
            "eligibility_reason": "TEXT",
            "missing_skills": "TEXT",
            "recommendation": "TEXT"
        }

        for col_name, col_type in column_migrations.items():
            if col_name not in existing_columns:
                logger.info(f"Migrating database: Adding column '{col_name}' to 'jobs' table.")
                cursor.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_type}")

        # Create processed_messages table for message-level idempotency
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT UNIQUE NOT NULL,
                channel_id TEXT,
                guild_id TEXT,
                received_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            )
        """)

        # Create bot_state table for persistent key-value state (e.g., daily_report_last_sent)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Create strategic indexes for query performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_discord_msg_id ON jobs(discord_message_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_received_at ON jobs(received_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_recommendation ON jobs(recommendation)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_deadline_iso ON jobs(deadline_iso)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_relevance_score ON jobs(relevance_score)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_proc_msg_discord_id ON processed_messages(discord_message_id)")

        conn.commit()
    logger.info("Database initialized with WAL mode, migrations, and indexes!")

def get_message_status(discord_message_id, db_name=None):
    """Retrieves processing status of a Discord message."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status FROM processed_messages WHERE discord_message_id = ?
        """, (str(discord_message_id),))
        row = cursor.fetchone()
        return row["status"] if row else None

def set_message_status(discord_message_id, status, channel_id=None, guild_id=None, error=None, db_name=None):
    """Upserts message processing status in processed_messages table."""
    now_str = datetime.now(IST).isoformat(timespec="seconds")
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO processed_messages (
                discord_message_id, channel_id, guild_id, received_at, status, error
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_message_id) DO UPDATE SET
                status = excluded.status,
                error = excluded.error
        """, (
            str(discord_message_id),
            str(channel_id) if channel_id else None,
            str(guild_id) if guild_id else None,
            now_str,
            status,
            error
        ))
        conn.commit()

def save_job(job, company, organizer, original_post, discord_message_id, db_name=None):
    """Saves a parsed Job object into the SQLite jobs table with exact column/value alignment."""
    now_str = datetime.now(IST).isoformat(timespec="seconds")
    
    # Process and sanitize location, skills, missing_skills lists to JSON
    location_json = json.dumps(job.location if isinstance(job.location, list) else [])
    skills_json = json.dumps(job.skills if isinstance(job.skills, list) else [])
    missing_skills_json = json.dumps(job.missing_skills if isinstance(job.missing_skills, list) else [])
    
    relevance = max(0, min(100, int(job.relevance_score or 0)))
    skill_match = max(0, min(100, int(job.skill_match_score or 0)))

    # Explicit 27-column INSERT query
    columns = [
        "received_at", "discord_message_id", "company", "organizer", "title",
        "category", "location", "work_type", "experience", "qualification",
        "passing_year", "cgpa", "aggregate", "skills", "salary",
        "bond", "deadline", "deadline_iso", "application_url", "relevance_score",
        "relevance_reason", "skill_match_score", "eligibility", "eligibility_reason", "missing_skills",
        "recommendation", "original_post"
    ]
    
    placeholders = ", ".join(["?"] * len(columns))
    sql_query = f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})"
    
    values = (
        now_str,
        str(discord_message_id),
        company,
        organizer,
        job.title,
        job.category,
        location_json,
        job.work_type,
        job.experience,
        job.qualification,
        job.passing_year,
        job.cgpa,
        job.aggregate,
        skills_json,
        job.salary,
        job.bond,
        job.deadline,
        job.deadline_iso,
        job.application_url,
        relevance,
        job.relevance_reason,
        skill_match,
        job.eligibility,
        job.eligibility_reason,
        missing_skills_json,
        job.recommendation,
        original_post
    )

    # Programmatic assertion during runtime to guarantee alignment
    assert len(columns) == len(values) == 27, f"SQL column mismatch: {len(columns)} cols vs {len(values)} vals"

    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute(sql_query, values)
        conn.commit()

def get_todays_jobs(date_str=None, db_name=None):
    """Retrieves all jobs recorded on a specific date (defaults to today in IST)."""
    if not date_str:
        date_str = datetime.now(IST).strftime("%Y-%m-%d")

    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM jobs
            WHERE received_at LIKE ?
            ORDER BY relevance_score DESC, skill_match_score DESC
        """, (f"{date_str}%",))
        return cursor.fetchall()

def get_bot_state(key, default=None, db_name=None):
    """Gets persistent bot state value from SQLite."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def set_bot_state(key, value, db_name=None):
    """Sets persistent bot state value in SQLite."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()

def get_stats(db_name=None):
    """Returns database statistics for status commands."""
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS count FROM jobs")
        total_jobs = cursor.fetchone()["count"]
        
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) AS count FROM jobs WHERE received_at LIKE ?", (f"{today_str}%",))
        todays_jobs = cursor.fetchone()["count"]
        
        cursor.execute("SELECT COUNT(*) AS count FROM processed_messages")
        processed_count = cursor.fetchone()["count"]
        
        last_report = get_bot_state("daily_report_last_sent", default="Never", db_name=db_name)
        
        return {
            "total_jobs": total_jobs,
            "todays_jobs": todays_jobs,
            "processed_messages": processed_count,
            "last_report_date": last_report
        }

