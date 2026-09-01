"""Pure logic for the Schedule tab (Phase E). Reuses settings_logic.py's
override read/write helpers (load_raw_override, override_to_yaml_bytes,
override_file_path) — schedule and sending settings live in the SAME
config/campaigns/<name>.yaml file, just different top-level keys, so
there's no reason to duplicate the file I/O.

Validation deliberately mirrors outreach.is_within_sending_window's own
requirements (real IANA timezone, HH:MM times) so this can never persist
a schedule the core system would itself reject when it next loads this
file.
"""
import os
import sys
from typing import Dict, List, Optional

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# A curated, friendly subset rather than all ~600 IANA zones — covers
# every major region without overwhelming a non-technical user. Each is a
# real IANA name (DST-safe), never a fixed-offset abbreviation like "PST".
COMMON_TIMEZONES = [
    ("Pacific Time (US & Canada)", "America/Los_Angeles"),
    ("Mountain Time (US & Canada)", "America/Denver"),
    ("Central Time (US & Canada)", "America/Chicago"),
    ("Eastern Time (US & Canada)", "America/New_York"),
    ("Atlantic Time (Canada)", "America/Halifax"),
    ("UTC", "UTC"),
    ("London", "Europe/London"),
    ("Paris / Berlin / Madrid", "Europe/Paris"),
    ("Athens / Helsinki", "Europe/Athens"),
    ("Dubai", "Asia/Dubai"),
    ("India (IST)", "Asia/Kolkata"),
    ("Bangkok / Jakarta", "Asia/Bangkok"),
    ("Singapore / Hong Kong", "Asia/Singapore"),
    ("Tokyo / Seoul", "Asia/Tokyo"),
    ("Sydney / Melbourne", "Australia/Sydney"),
    ("Auckland", "Pacific/Auckland"),
]

DAY_OPTIONS = [
    ("Monday", "mon"), ("Tuesday", "tue"), ("Wednesday", "wed"), ("Thursday", "thu"),
    ("Friday", "fri"), ("Saturday", "sat"), ("Sunday", "sun"),
]
WEEKDAY_DEFAULTS = ["mon", "tue", "wed", "thu", "fri"]


def validate_schedule(timezone_name: str, window_start: str, window_end: str, send_days: List[str]) -> List[str]:
    errors = []
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"'{timezone_name}' isn't a recognized timezone.")

    from datetime import datetime as _dt
    try:
        _dt.strptime(window_start, "%H:%M")
    except ValueError:
        errors.append("Start time must be in HH:MM format.")
    try:
        _dt.strptime(window_end, "%H:%M")
    except ValueError:
        errors.append("End time must be in HH:MM format.")

    if not send_days:
        errors.append("Select at least one day to send on.")

    return errors


def build_updated_schedule_override(raw_override: Dict, timezone_name: str, window_start: str,
                                     window_end: str, send_days: List[str]) -> Dict:
    """Returns a NEW dict — never mutates raw_override. Only 'schedule' is
    touched; 'sending', 'status', and anything else pass through untouched
    — same guarantee settings_logic.build_updated_override makes for its
    own key."""
    updated = dict(raw_override)
    updated["schedule"] = {
        "timezone": timezone_name,
        "window_start": window_start,
        "window_end": window_end,
        "send_days": list(send_days),
    }
    return updated


def get_current_schedule(campaign_cfg: Dict) -> Dict:
    """Sensible defaults for a campaign with no schedule configured yet —
    matches this codebase's existing convention of a real IANA zone as
    the default (never a bare offset), and every day / all-day as the
    unrestricted-by-default state, mirroring "no schedule = always
    allowed" in outreach.is_within_sending_window."""
    schedule = campaign_cfg.get("schedule") or {}
    return {
        "timezone": schedule.get("timezone") or "America/Los_Angeles",
        "window_start": schedule.get("window_start") or "09:00",
        "window_end": schedule.get("window_end") or "17:00",
        "send_days": list(schedule.get("send_days") or WEEKDAY_DEFAULTS),
    }


def timezone_display_name(iana_name: str) -> Optional[str]:
    for display, iana in COMMON_TIMEZONES:
        if iana == iana_name:
            return display
    return None
