"""Pure logic for the Email Accounts page. Never touches SMTP credentials —
those live only in the GitHub Secret EMAIL_ACCOUNTS_JSON, used exclusively
by GitHub Actions. This page shows account NAMES and ADDRESSES (from a
lightweight companion Streamlit secret containing no passwords) plus
real usage counts pulled from each campaign's Send Log.
"""
import os
import sys
from typing import Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402


def aggregate_sent_today_by_account(send_logs_by_campaign: Dict[str, List[Dict]]) -> Dict[str, int]:
    """send_logs_by_campaign: {campaign_name: send_log_rows}. Sums today's
    'sent' counts per account across every campaign — an account's daily
    cap is shared across all campaigns that use it, so a per-campaign-only
    count would understate real usage."""
    totals: Dict[str, int] = {}
    for send_log in send_logs_by_campaign.values():
        campaign_counts = outreach._count_sent_today_by_account(send_log)  # noqa: SLF001 - single source of truth
        for acct, count in campaign_counts.items():
            totals[acct] = totals.get(acct, 0) + count
    return totals


def build_health_lookup(health_records: List[Dict]) -> Dict[str, Dict]:
    """{account_name: {"status": ..., "detail": ..., "checked_at": ...}}
    from the raw Sheet records written by check_account_health.yml —
    keyed by AccountName exactly as EMAIL_ACCOUNTS_JSON's own keys are,
    so this merges directly against account_directory's keys with no
    fuzzy matching needed."""
    lookup: Dict[str, Dict] = {}
    for record in health_records:
        name = record.get("AccountName", "")
        if name:
            lookup[name] = {
                "status": record.get("Status", ""),
                "detail": record.get("Detail", ""),
                "checked_at": record.get("CheckedAt", ""),
            }
    return lookup


def merge_account_directories(streamlit_secret_directory: Dict[str, str],
                               slot_mapping: Dict[str, Dict]) -> Dict[str, str]:
    """Combines the legacy Streamlit-secrets-based directory with the new
    slot-mapping file's directory (name -> {"slot": ..., "address": ...})
    — the slot mapping wins on a name collision, since an account managed
    through this app always has current data there, while the Streamlit
    secret might be stale (Streamlit can't write its own secrets, so
    nothing keeps it in sync after an in-app edit). Same "new source
    wins" principle as outreach.load_email_accounts merging slots over
    EMAIL_ACCOUNTS_JSON."""
    merged = dict(streamlit_secret_directory)
    for name, entry in slot_mapping.items():
        merged[name] = entry["address"]
    return merged


def build_account_rows(account_directory: Dict[str, str], sent_today_by_account: Dict[str, int],
                        default_account: str, health_lookup: Dict[str, Dict] = None) -> List[Dict]:
    """account_directory: {account_name: address} (no passwords — see
    module docstring). Returns rows sorted by account name, each with
    today's send count, whether it's the global default, and connection
    status. health_lookup is optional — a brand new deployment (before
    check_account_health.yml has ever run) shows "Unknown" rather than
    erroring, matching ReadOnlySheetsConnector.get_account_health's own
    "tab doesn't exist yet" -> [] behavior."""
    health_lookup = health_lookup or {}
    rows = []
    for name in sorted(account_directory.keys()):
        health = health_lookup.get(name, {})
        rows.append({
            "name": name,
            "address": account_directory[name],
            "sent_today": sent_today_by_account.get(name, 0),
            "is_default": name == default_account,
            "status": health.get("status") or "Unknown",
            "status_detail": health.get("detail", ""),
            "checked_at": health.get("checked_at", ""),
        })
    return rows
