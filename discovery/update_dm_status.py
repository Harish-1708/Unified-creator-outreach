"""
update_dm_status.py — writes a human's DM outreach outcome (status + free-
text notes) onto ONE Shortlist row, identified by (dedup_key, Campaign).

Deliberately targets the SHORTLIST tab, not Master: dm_status, dm_notes,
and dm_draft only exist as Shortlist columns (SHORTLIST_EXTRA_HEADERS in
shortlist.py) — they were never part of MASTER_HEADERS, since a DM outcome
only makes sense for a row that's already been approved and synced there
in the first place.

Run only via GitHub Actions — the "Update DM Status" workflow, dispatched
from the DM Queue Streamlit page. This never sends a DM or drafts one —
there is no code path anywhere in this repo that sends a DM automatically,
and this script isn't the exception: it only ever records what a human
already did manually on the platform itself.
"""
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

DM_STATUS_OPTIONS = [
    "Not Contacted", "Draft Ready", "Sent", "Follow-up Needed",
    "Replied", "Interested", "Not Interested", "No Response", "Closed",
]


def find_target_row(shortlist_records: list, dedup_key: str, campaign: str):
    """Identical shape to update_review_decision.py's function of the same
    name, deliberately kept as a separate copy rather than a shared import
    — this repo's own established philosophy is self-contained files, and
    the two scripts target different tabs (Shortlist here, Master there),
    which makes a shared abstraction less clean than it first looks."""
    for i, r in enumerate(shortlist_records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def main():
    dedup_key = os.environ.get("CREATOR_KEY", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()
    dm_status = os.environ.get("DM_STATUS", "").strip()
    dm_notes = os.environ.get("DM_NOTES", "")

    missing = [name for name, val in {
        "CREATOR_KEY": dedup_key, "CAMPAIGN": campaign, "DM_STATUS": dm_status,
    }.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    if dm_status not in DM_STATUS_OPTIONS:
        raise ValueError(f"DM_STATUS must be one of {DM_STATUS_OPTIONS}, got '{dm_status}'.")

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    shortlist_ws = sheet.worksheet("Shortlist")

    records = shortlist_ws.get_all_records()
    row_num = find_target_row(records, dedup_key, campaign)
    if row_num is None:
        raise ValueError(
            f"No Shortlist row found for dedup_key='{dedup_key}', Campaign='{campaign}' — "
            f"this creator may not have been synced to Shortlist yet (run 'Sync Shortlist' "
            f"first), or the values don't match exactly (Campaign is case-sensitive)."
        )

    header = shortlist_ws.row_values(1)
    now = datetime.now(timezone.utc).isoformat()
    updates = {"dm_status": dm_status, "dm_notes": dm_notes, "dm_last_action_at": now}
    for col_name, value in updates.items():
        if col_name not in header:
            raise ValueError(
                f"Shortlist tab has no '{col_name}' column yet — run 'Sync Shortlist' once "
                f"first, which self-heals the header row to include any newly added columns."
            )
        col_index = header.index(col_name) + 1
        shortlist_ws.update_cell(row_num, col_index, value)

    print(f"[dm_status] {dedup_key} (Campaign='{campaign}'): dm_status={dm_status!r} — "
          f"saved to Shortlist row {row_num}.")


if __name__ == "__main__":
    main()
