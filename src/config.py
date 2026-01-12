"""Configuration constants and environment variables."""
import os
import re
from typing import List, Optional
from datetime import timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

# API Configuration
BASE_API = os.getenv("SALTALA_BASE", "https://saltala.apisaltala.com/api/v1")
PUBLIC_URL = os.getenv("PUBLIC_URL", "lobarnechea")

# Target Line Configuration
TARGET_LINE_NAMES_RAW = os.getenv("TARGET_LINE_NAMES", "Renovación")
TARGET_LINE_NAMES = [s.strip() for s in TARGET_LINE_NAMES_RAW.split(",") if s.strip()]
FALLBACK_LINE_ID = int(os.getenv("FALLBACK_LINE_ID", "1768"))

# Unit Hint
UNIT_HINT = os.getenv("UNIT_HINT", "277")
UNIT_HINT = int(UNIT_HINT) if UNIT_HINT and UNIT_HINT.isdigit() else None

# Time Range Configuration
NUMBER_OF_MONTH = int(os.getenv("NUMBER_OF_MONTH", "2"))
CORPORATION_ID = int(os.getenv("CORPORATION_ID", "0"))

# HTTP Configuration
TIMEOUT = (10, 20)  # (connect, read)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"

# Timezone Configuration
TZ_NAME = os.getenv("TZ_NAME", "America/Santiago")  # used to compute correct offset per date (DST-safe)
TZ_OFFSET = os.getenv("TZ_OFFSET", "")  # optional override like "-03:00" (takes precedence over TZ_NAME)

# Debug Configuration
DEBUG_LOG_PAYLOADS = os.getenv("DEBUG_LOG_PAYLOADS", "0") not in ("", "0", "false", "False")

# Exit Code
EXIT_AVAILABILITY_HANDLED = 42


def _env_list(key: str) -> List[str]:
    """Parse comma or whitespace-separated environment variable into list."""
    raw = os.getenv(key, "")
    return [s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()]


# Mock Configuration (for testing)
MOCK_LINE_ID_RAW = os.getenv("MOCK_LINE_ID", "")
MOCK_LINE_ID = int(MOCK_LINE_ID_RAW) if MOCK_LINE_ID_RAW.isdigit() else None
MOCK_LINE_NAME = os.getenv("MOCK_LINE_NAME", "")
MOCK_DAYS = _env_list("MOCK_DAYS")
MOCK_TIMES = _env_list("MOCK_TIMES")


def format_offset(td: Optional[timedelta]) -> str:
    """Format a timedelta as ISO offset string (e.g., '-03:00')."""
    if td is None:
        return "-00:00"
    total = int(td.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh = total // 3600
    mm = (total % 3600) // 60
    return f"{sign}{hh:02d}:{mm:02d}"


def offset_for_date(date_str: str) -> str:
    """
    Returns an ISO offset like -03:00 for the given YYYY-MM-DD.
    - If TZ_OFFSET is set, uses it.
    - Else computes from TZ_NAME (DST-safe).
    """
    if TZ_OFFSET:
        return TZ_OFFSET
    try:
        tz = ZoneInfo(TZ_NAME)
        from datetime import datetime
        d = datetime.strptime(date_str, "%Y-%m-%d")
        # local midnight; offset at that local time
        local = d.replace(tzinfo=tz)
        return format_offset(local.utcoffset())
    except Exception:
        # safe fallback
        return "-03:00"
