import os
import json
import logging
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configuration settings with safe defaults
DISCORD_USER_ID = int(os.getenv("DISCORD_USER_ID", "1480447144287539313"))
DB_NAME = os.getenv("DB_NAME", "jobs.db")
PROFILE_FILE = os.getenv("PROFILE_FILE", "profile.json")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

try:
    GEMINI_REQUEST_DELAY = float(os.getenv("GEMINI_REQUEST_DELAY", "1.0"))
except ValueError:
    GEMINI_REQUEST_DELAY = 1.0

try:
    MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "20000"))
except ValueError:
    MAX_MESSAGE_CHARS = 20000

try:
    REPORT_HOUR = int(os.getenv("REPORT_HOUR", "21"))
except ValueError:
    REPORT_HOUR = 21

try:
    REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))
except ValueError:
    REPORT_MINUTE = 0

from datetime import timezone, timedelta

TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")
try:
    IST = ZoneInfo(TIMEZONE_NAME)
except Exception:
    # Fallback for Windows systems missing the tzdata package
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DSJobTracker")

def validate_environment():
    """Validates that critical environment variables exist."""
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is missing from environment or .env file.")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is missing from environment or .env file.")

def load_and_validate_profile(profile_path=PROFILE_FILE):
    """Validates profile.json existence and JSON structure."""
    if not os.path.exists(profile_path):
        raise FileNotFoundError(f"Profile file '{profile_path}' not found.")
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile_data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in profile file '{profile_path}': {e}")
    
    if not isinstance(profile_data, dict):
        raise ValueError(f"Profile content in '{profile_path}' must be a JSON object.")
        
    logger.info("👤 User profile loaded!")
    return profile_data
