"""Pure logic for the Check Replies tab's "recent replies" list. The
Response Sheet is already updated the moment check_replies.yml finishes —
this module just sorts and trims what to show, so the page doesn't need to
wait for or parse GitHub Actions run output to display results.
"""
from datetime import datetime
from typing import Dict, List

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def _parse_received_at(value: str) -> datetime:
    try:
        return datetime.strptime(value, DATETIME_FMT)
    except (ValueError, TypeError):
        return datetime.min  # unparsable/blank timestamps sort last, never crash the page


def most_recent_responses(responses: List[Dict], limit: int = 20) -> List[Dict]:
    """Newest first. Doesn't mutate the input list."""
    return sorted(responses, key=lambda r: _parse_received_at(r.get("ReceivedAt", "")), reverse=True)[:limit]
