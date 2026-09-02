"""
edit_master_row.py — updates a small set of human-editable fields on ONE
Master row: contact_email, username, profile_link, content_angle. Every
other column (scores, evidence text, discovery metadata) is pipeline-
computed and deliberately NOT editable through this script — hand-editing
a score would make it indistinguishable from a real one.

Deliberately does NOT allow editing dedup_key. dedup_key is the stable
identifier every other stage (Shortlist sync, the bridge, DM drafting)
matches on — changing it retroactively would silently break every
existing cross-reference to this creator. If the underlying username is
wrong, this fixes the DISPLAY value; the creator's identity (dedup_key)
stays exactly what it was.

Always sets all four fields to whatever was passed, even blank — this is
called only from the Master tab's inline-edit "Save Edits" flow, which
already knows the full current value of every editable column, so a
blank value here is a deliberate clear, not "leave alone."

Run only via GitHub Actions — the "Edit Master Row" workflow.
"""
import os

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

EDITABLE_FIELDS = ["contact_email", "username", "profile_link", "content_angle"]


def find_row(records: list, dedup_key: str, campaign: str):
    for i, r in enumerate(records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def main():
    dedup_key = os.environ.get("CREATOR_KEY", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()

    missing = [name for name, val in {"CREATOR_KEY": dedup_key, "CAMPAIGN": campaign}.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    updates = {field: os.environ.get(field.upper(), "") for field in EDITABLE_FIELDS}

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    master_ws = sheet.worksheet("Master")

    records = master_ws.get_all_records()
    row_num = find_row(records, dedup_key, campaign)
    if row_num is None:
        raise ValueError(f"No Master row found for dedup_key='{dedup_key}', Campaign='{campaign}'.")

    header = master_ws.row_values(1)
    for field, value in updates.items():
        if field not in header:
            raise ValueError(f"Master tab has no '{field}' column.")
        col_index = header.index(field) + 1
        master_ws.update_cell(row_num, col_index, value)

    print(f"[edit_master_row] Updated {list(updates.keys())} for '{dedup_key}' "
          f"(Campaign='{campaign}'), Master row {row_num}.")


if __name__ == "__main__":
    main()
