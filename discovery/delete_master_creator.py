"""
delete_master_creator.py — permanently removes creators from Master AND
Shortlist (if present there too). A genuine hard delete, not a soft one —
this is discovery candidate data, not send history, so there's nothing
worth preserving once a human has decided a row shouldn't exist at all.

Supports a comma-separated batch, each isolated (one bad key never blocks
the rest — same convention as update_review_decision.py/
promote_excluded_creator.py). Not being in Shortlist yet is NOT a
failure — most Master rows never get synced there, so "absent from
Shortlist" is the common case, not an error.

Deletions within EACH sheet happen in descending row-number order,
computed once against a single fetched snapshot per sheet — the same
reasoning as promote_excluded_creator.py's docstring: deleting a row
shifts every row below it up by one, corrupting the row numbers of
anything else in the same batch still waiting to be deleted if processed
top-to-bottom.

Run only via GitHub Actions — the "Delete Master Creator" workflow.
"""
import os

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def find_row(records: list, dedup_key: str, campaign: str):
    for i, r in enumerate(records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def plan_deletions(master_records: list, shortlist_records: list, dedup_keys: list, campaign: str) -> list:
    """Pure — no I/O. One result dict per requested key:
      {"dedup_key", "status": "ready" | "failed",
       "master_row_num" (only if ready, always present when ready),
       "shortlist_row_num" (only if ready AND found there — may be None),
       "error" (only if failed)}
    A key not found in Master at all is a failure (nothing to delete);
    not found in Shortlist is fine (shortlist_row_num just stays None)."""
    results = []
    for dedup_key in dedup_keys:
        master_row_num = find_row(master_records, dedup_key, campaign)
        if master_row_num is None:
            results.append({"dedup_key": dedup_key, "status": "failed",
                             "error": f"No Master row found for dedup_key='{dedup_key}', "
                                      f"Campaign='{campaign}'."})
            continue
        shortlist_row_num = find_row(shortlist_records, dedup_key, campaign)
        results.append({"dedup_key": dedup_key, "status": "ready",
                         "master_row_num": master_row_num, "shortlist_row_num": shortlist_row_num})
    return results


def main():
    dedup_keys_raw = os.environ.get("CREATOR_KEY", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()

    missing = [name for name, val in {"CREATOR_KEY": dedup_keys_raw, "CAMPAIGN": campaign}.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    dedup_keys = [k.strip() for k in dedup_keys_raw.split(",") if k.strip()]
    if not dedup_keys:
        raise ValueError("CREATOR_KEY parsed to zero non-blank keys.")

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    master_ws = sheet.worksheet("Master")

    try:
        shortlist_ws = sheet.worksheet("Shortlist")
        shortlist_records = shortlist_ws.get_all_records()
    except gspread.WorksheetNotFound:
        shortlist_ws = None
        shortlist_records = []

    master_records = master_ws.get_all_records()
    plan = plan_deletions(master_records, shortlist_records, dedup_keys, campaign)
    ready = [p for p in plan if p["status"] == "ready"]

    # Master deletions, descending row order.
    for item in sorted(ready, key=lambda p: p["master_row_num"], reverse=True):
        master_ws.delete_rows(item["master_row_num"])

    # Shortlist deletions, descending row order — independent ordering
    # from Master's, since they're different row numbers in a different
    # sheet entirely.
    if shortlist_ws is not None:
        shortlist_deletions = sorted(
            [p["shortlist_row_num"] for p in ready if p["shortlist_row_num"] is not None],
            reverse=True,
        )
        for row_num in shortlist_deletions:
            shortlist_ws.delete_rows(row_num)

    for item in plan:
        if item["status"] == "ready":
            also = ", also removed from Shortlist" if item["shortlist_row_num"] is not None else ""
            print(f"[delete_master_creator] Deleted '{item['dedup_key']}' from Master{also} "
                  f"(Campaign='{campaign}').")
        else:
            print(f"[delete_master_creator] {item['dedup_key']}: FAILED — {item['error']}")

    failed = [p for p in plan if p["status"] == "failed"]
    print(f"\n[delete_master_creator] Done. {len(ready)} deleted, {len(failed)} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
