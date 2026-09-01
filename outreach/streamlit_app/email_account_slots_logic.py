"""Pure logic for tracking which EMAIL_ACCOUNT_SLOT_N secrets are in use.

GitHub Secrets can never be read back — not by this app, not by any
token — so Streamlit has no way to ask GitHub "which slots are free?"
directly. Instead, a small, non-secret YAML file is committed to the
repo (config/email_account_slots.yaml) mapping each account's name to
its slot number AND its address. Neither of those is sensitive — the
address is the visible "From" on every email that account sends anyway —
so this file is safe to commit in plain text, unlike the app_password,
which only ever exists inside an encrypted GitHub Secret.

This file becomes the one authoritative "account directory" once an
account is managed through this app — see accounts_logic.py, which
merges it with the legacy Streamlit-secrets-based directory during the
transition (same merge pattern as outreach.load_email_accounts).
"""
import os
import sys
from typing import Dict, List, Optional, Tuple

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402

SLOT_MAPPING_PATH = "config/email_account_slots.yaml"


def parse_slot_mapping(raw_yaml: str) -> Dict[str, Dict]:
    """{account_name: {"slot": int, "address": str}}. Returns {} for an
    empty or not-yet-existing file — every account slot management
    feature starts from zero accounts tracked, not an error."""
    if not raw_yaml or not raw_yaml.strip():
        return {}
    data = yaml.safe_load(raw_yaml) or {}
    mapping = {}
    for name, entry in data.items():
        mapping[name] = {"slot": int(entry["slot"]), "address": entry.get("address", "")}
    return mapping


def serialize_slot_mapping(mapping: Dict[str, Dict]) -> bytes:
    # sort_keys so the committed file's diff is stable/reviewable rather
    # than reordering on every edit based on dict insertion order.
    return yaml.safe_dump(mapping, sort_keys=True, default_flow_style=False).encode("utf-8")


def find_next_free_slot(mapping: Dict[str, Dict], slot_count: int = outreach.EMAIL_ACCOUNT_SLOT_COUNT) -> Optional[int]:
    used_slots = {entry["slot"] for entry in mapping.values()}
    for i in range(1, slot_count + 1):
        if i not in used_slots:
            return i
    return None


def add_account_to_mapping(mapping: Dict[str, Dict], account_name: str, address: str,
                            slot_count: int = outreach.EMAIL_ACCOUNT_SLOT_COUNT) -> Dict[str, Dict]:
    """Returns a NEW mapping — never mutates the input. Raises ValueError
    if the name already exists (use edit, not add) or every slot is full."""
    if account_name in mapping:
        raise ValueError(f"Account '{account_name}' already exists (slot {mapping[account_name]['slot']}).")
    next_slot = find_next_free_slot(mapping, slot_count)
    if next_slot is None:
        raise ValueError(f"All {slot_count} account slots are full.")
    updated = dict(mapping)
    updated[account_name] = {"slot": next_slot, "address": address}
    return updated


def remove_account_from_mapping(mapping: Dict[str, Dict], account_name: str) -> Dict[str, Dict]:
    """Returns a NEW mapping with account_name removed — never mutates
    the input, and never raises if the name wasn't present, since the
    caller's goal ("this shouldn't be tracked") is already satisfied
    either way."""
    updated = dict(mapping)
    updated.pop(account_name, None)
    return updated


def update_account_address_in_mapping(mapping: Dict[str, Dict], account_name: str, new_address: str) -> Dict[str, Dict]:
    """Returns a NEW mapping with account_name's address updated — the
    slot number never changes on an edit, only add/remove touch it.
    Raises ValueError if the account isn't tracked."""
    if account_name not in mapping:
        raise ValueError(f"Account '{account_name}' isn't tracked in the slot mapping.")
    updated = dict(mapping)
    updated[account_name] = {"slot": mapping[account_name]["slot"], "address": new_address}
    return updated


def get_account_names(mapping: Dict[str, Dict]) -> List[str]:
    return sorted(mapping.keys())


def read_local_slot_mapping(abs_path: str) -> Dict[str, Dict]:
    """Local file read (the repo's own checkout), NOT a GitHub API call —
    same pattern as reading templates/settings.yaml elsewhere in this app.
    Safe to read directly since this file only ever contains names, slot
    numbers, and addresses — never a password. Shared by every page that
    needs to know which accounts exist (Email Accounts, and any
    campaign's own Settings sender-account picker) so they never
    silently disagree about which accounts exist."""
    if not os.path.exists(abs_path):
        return {}
    with open(abs_path, "r", encoding="utf-8") as f:
        return parse_slot_mapping(f.read())


def build_account_secret_payload(name: str, address: str, password: str,
                                  imap_password: Optional[str] = None,
                                  smtp_host: Optional[str] = None, smtp_port: Optional[str] = None,
                                  smtp_username: Optional[str] = None,
                                  imap_host: Optional[str] = None, imap_port: Optional[str] = None,
                                  imap_username: Optional[str] = None) -> str:
    """Builds the JSON string for one EMAIL_ACCOUNT_SLOT_N secret. Only
    includes an optional field when it's actually set — a plain Gmail
    account's secret stays exactly {"name", "address", "app_password"},
    the format that's always worked, rather than padding every account
    with empty custom-provider fields it doesn't need. A third-party
    provider (Hostinger, etc.) can set some or all of the rest —
    smtp_connection_settings/imap_connection_settings fall back to
    Gmail's own host/port and the account's address for anything left
    unset here."""
    import json
    payload: Dict = {"name": name, "address": address, "app_password": password}
    if imap_password:
        payload["imap_password"] = imap_password
    if smtp_host:
        payload["smtp_host"] = smtp_host
    if smtp_port:
        payload["smtp_port"] = int(smtp_port)
    if smtp_username:
        payload["smtp_username"] = smtp_username
    if imap_host:
        payload["imap_host"] = imap_host
    if imap_port:
        payload["imap_port"] = int(imap_port)
    if imap_username:
        payload["imap_username"] = imap_username
    return json.dumps(payload)


BULK_ACCOUNT_CSV_COLUMNS = [
    "Name", "Email", "Password", "IMAP Password", "SMTP Host", "SMTP Port", "SMTP Username",
    "IMAP Host", "IMAP Port", "IMAP Username",
]


def parse_bulk_accounts_csv(columns: List[str], rows: List[Dict[str, str]]) -> Tuple[List[Dict], List[str]]:
    """One universal CSV format covers both a plain Gmail account (only
    Name/Email/Password filled in — every other column blank) and a
    custom-provider account (Hostinger, etc. — the rest filled in too).
    Returns (parsed_rows, errors); parsed_rows is a list of dicts with
    keys matching build_account_secret_payload's parameters, one per
    valid row, in order. errors names which row (1-indexed, matching
    what a spreadsheet shows, header excluded) couldn't be parsed and
    why — a bad row is skipped, not a reason to abandon every other
    valid row in the same file."""
    required = {"Email", "Password"}
    missing_columns = required - set(columns)
    if missing_columns:
        return [], [f"CSV is missing required column(s): {', '.join(sorted(missing_columns))}"]

    parsed = []
    errors = []
    for i, row in enumerate(rows, start=1):
        email_addr = (row.get("Email") or "").strip()
        password = (row.get("Password") or "").strip()
        if not email_addr:
            errors.append(f"Row {i}: Email is required.")
            continue
        if not password:
            errors.append(f"Row {i}: Password is required.")
            continue
        name = (row.get("Name") or "").strip() or email_addr.split("@")[0]
        parsed.append({
            "name": name,
            "address": email_addr,
            "password": password,
            "imap_password": (row.get("IMAP Password") or "").strip() or None,
            "smtp_host": (row.get("SMTP Host") or "").strip() or None,
            "smtp_port": (row.get("SMTP Port") or "").strip() or None,
            "smtp_username": (row.get("SMTP Username") or "").strip() or None,
            "imap_host": (row.get("IMAP Host") or "").strip() or None,
            "imap_port": (row.get("IMAP Port") or "").strip() or None,
            "imap_username": (row.get("IMAP Username") or "").strip() or None,
        })
    return parsed, errors
