"""
promote_excluded_creator.py — moves ONE creator from the Excluded tab into
Master, for cases where a human disagrees with the pipeline's automatic
rejection and wants it back in the review queue.

A genuine MOVE, not a copy: the row is removed from Excluded once it's
been added to Master, since "excluded" and "in Master for review" are
mutually exclusive states — leaving it in both would make it show up in
two contradictory places at once.

Self-contained (reads the real header rows directly) rather than
importing discover.py's EXCLUDED_HEADERS/MASTER_HEADERS, matching every
other script in this build (update_review_decision.py, add_manual_creator.py)
for the same reason: no need to load discover.py's heavier dependencies
just to copy one row between two tabs it already knows the shape of.

Run only via GitHub Actions — the "Promote Excluded Creator" workflow.
"""
import os

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def find_row(records: list, dedup_key: str, campaign: str):
    """Same compound-key shape as every other lookup in this pipeline."""
    for i, r in enumerate(records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def build_row_for_header(source_record: dict, header: list) -> list:
    """Builds a row matching the TARGET tab's real header — Master and
    Excluded don't share an identical column set (Master has Niche
    columns Excluded doesn't; Excluded has rejection_reason Master
    doesn't), so any column not present on the source record is left
    blank, not omitted."""
    return [str(source_record.get(h, "")) for h in header]


def main():
    dedup_key = os.environ.get("CREATOR_KEY", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()

    missing = [name for name, val in {"CREATOR_KEY": dedup_key, "CAMPAIGN": campaign}.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    excluded_ws = sheet.worksheet("Excluded")
    master_ws = sheet.worksheet("Master")

    excluded_records = excluded_ws.get_all_records()
    excluded_row_num = find_row(excluded_records, dedup_key, campaign)
    if excluded_row_num is None:
        raise ValueError(
            f"No Excluded row found for dedup_key='{dedup_key}', Campaign='{campaign}' — check both "
            f"values match exactly (Campaign is case-sensitive)."
        )

    master_records = master_ws.get_all_records()
    if find_row(master_records, dedup_key, campaign) is not None:
        raise ValueError(
            f"'{dedup_key}' already exists in Master under Campaign '{campaign}' — nothing to promote, "
            f"it's already there."
        )

    source_record = excluded_records[excluded_row_num - 2]
    master_header = master_ws.row_values(1)
    new_row = build_row_for_header(source_record, master_header)
    master_ws.append_row(new_row, value_input_option="RAW")
    excluded_ws.delete_rows(excluded_row_num)

    print(f"[promote_excluded_creator] Moved '{dedup_key}' from Excluded to Master under "
          f"Campaign '{campaign}'.")


if __name__ == "__main__":
    main()
