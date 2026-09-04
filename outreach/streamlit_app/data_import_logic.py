"""Pure logic for the Data tab (Phase C). Nothing here touches the
network — CSV parsing and mapping happen entirely in-process. The actual
Sheet write happens via the same pattern as everywhere else in this app:
commit a JSON payload file, trigger a GitHub Actions workflow that reads
it and does the real write with the Editor-scoped credential. Streamlit
itself never gets Sheets write access, here or anywhere else.
"""
import csv
import io
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

KNOWN_FIELDS = ["FirstName", "LastName", "Email", "Company"]


def parse_csv_bytes(raw_bytes: bytes) -> Tuple[List[str], List[Dict[str, str]]]:
    """Returns (column_names, rows). utf-8-sig handles a BOM from Excel
    exports without choking on it."""
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    rows = [dict(row) for row in reader]
    return columns, rows


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_default_mapping(csv_columns: List[str], custom_columns: List[str]) -> Dict[str, str]:
    """Best-effort auto-mapping by normalized name match, so the user
    usually just reviews/adjusts rather than mapping from scratch.
    Returns {csv_column: target_field_or_empty_string} — "" means
    unmapped/skip. Known fields (FirstName/LastName/Email/Company) are
    preferred over a same-named custom column, in case of a clash."""
    known_by_norm = {_normalize(f): f for f in KNOWN_FIELDS}
    custom_by_norm = {_normalize(c): c for c in custom_columns}
    mapping = {}
    for col in csv_columns:
        key = _normalize(col)
        mapping[col] = known_by_norm.get(key) or custom_by_norm.get(key) or ""
    return mapping


def apply_mapping(rows: List[Dict[str, str]], mapping: Dict[str, str]) -> List[Dict[str, str]]:
    """mapping: {csv_column: target_field}. Columns mapped to "" are
    dropped. Every value is stripped of surrounding whitespace."""
    mapped_rows = []
    for row in rows:
        mapped = {}
        for csv_col, target_field in mapping.items():
            if not target_field:
                continue
            mapped[target_field] = (row.get(csv_col) or "").strip()
        mapped_rows.append(mapped)
    return mapped_rows


def validate_mapping(mapping: Dict[str, str]) -> Optional[str]:
    if "Email" not in mapping.values():
        return "Map at least one column to Email — it's the only required field."
    return None


def count_valid_rows(mapped_rows: List[Dict[str, str]]) -> int:
    """How many rows actually have an email — the number that will really
    get imported, before duplicates against the existing Sheet are even
    considered (that check only happens server-side, since only the
    server has the full current lead list at write time)."""
    return sum(1 for r in mapped_rows if (r.get("Email") or "").strip())


def build_import_payload(mapped_rows: List[Dict[str, str]]) -> Dict:
    return {"leads": mapped_rows}


def import_payload_path(campaign_name: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return f"outreach/imports/{campaign_name}/{ts}.json"


def build_removal_payload(lead_ids: List[str]) -> Dict:
    return {"lead_ids": [str(lid) for lid in lead_ids]}


def removal_payload_path(campaign_name: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return f"outreach/removals/{campaign_name}/{ts}.json"


def payload_to_bytes(payload: Dict) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


# ---------- Lead table filtering ----------

FILTER_ALL = "All"
FILTER_PENDING_APPROVAL = "Pending Approval"
FILTER_IN_PROGRESS = "In Progress"
FILTER_REPLIED = "Replied"
FILTER_BOUNCED = "Bounced"
FILTER_REMOVED = "Removed"

FILTER_OPTIONS = [FILTER_ALL, FILTER_PENDING_APPROVAL, FILTER_IN_PROGRESS, FILTER_REPLIED,
                   FILTER_BOUNCED, FILTER_REMOVED]


def filter_leads(leads: List[Dict], status_filter: str) -> List[Dict]:
    if status_filter == FILTER_ALL:
        return leads
    if status_filter == FILTER_PENDING_APPROVAL:
        return [l for l in leads if (l.get("Approval") or "") not in ("Yes",)]
    if status_filter == FILTER_REMOVED:
        return [l for l in leads if l.get("Status") == "Removed"]
    if status_filter == FILTER_REPLIED:
        return [l for l in leads if l.get("Status") == "Stopped - Replied"]
    if status_filter == FILTER_BOUNCED:
        return [l for l in leads if l.get("Status") == "Stopped - Bounced"]
    if status_filter == FILTER_IN_PROGRESS:
        return [l for l in leads
                if (l.get("IntroSentAt") or "").strip() and not (l.get("Status") or "").startswith("Stopped")
                and l.get("Status") != "Removed"]
    return leads


def search_leads(leads: List[Dict], query: str) -> List[Dict]:
    if not query or not query.strip():
        return leads
    q = query.strip().lower()
    return [
        l for l in leads
        if q in (l.get("FirstName") or "").lower()
        or q in (l.get("LastName") or "").lower()
        or q in (l.get("Email") or "").lower()
        or q in (l.get("Company") or "").lower()
    ]
