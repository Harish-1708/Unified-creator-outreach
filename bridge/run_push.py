"""
run_push.py — GitHub Actions entry point for the discovery -> outreach
bridge. Connects to the discovery sheet, selects eligible Shortlist rows,
hands them to push_approved_to_campaign.py, and prints a clear per-creator
summary. All the actual mapping/push logic lives there — this file only
handles env vars, the discovery-sheet connection, and reporting.
"""
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

from push_approved_to_campaign import push_creators_to_outreach

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def select_eligible_rows(records: list, creator_keys: set = None) -> list:
    """Pure filtering logic, kept separate from the sheet connection so it
    can be unit-tested directly. `records` is get_all_records()'s raw
    output (row 1 already consumed as the header) — row numbers are
    reconstructed here (row 2 = records[0]) since the caller needs "_row"
    for the bridge to write results back to the right place.

    Eligible means: outreach_channel is exactly "email" (never "dm" or
    "none" — this workflow only ever pushes email rows) AND
    campaign_push_status is blank or "failed" (never re-selects an
    already-"pushed" row — this is the idempotency guarantee: a row
    already marked pushed simply never appears here again). An explicit
    creator_keys set further restricts to just those dedup_keys, when
    provided; blank/None means "every eligible row".
    """
    eligible = []
    for i, r in enumerate(records, start=2):  # row 1 is the header
        if r.get("outreach_channel", "").strip().lower() != "email":
            continue
        if r.get("campaign_push_status", "").strip().lower() not in ("", "failed"):
            continue
        if creator_keys and r.get("dedup_key") not in creator_keys:
            continue
        row = dict(r)
        row["_row"] = i
        eligible.append(row)
    return eligible


def main():
    outreach_campaign = os.environ.get("OUTREACH_CAMPAIGN", "").strip()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"
    creator_keys_raw = os.environ.get("CREATOR_KEYS", "").strip()
    creator_keys = {k.strip() for k in creator_keys_raw.split(",") if k.strip()} if creator_keys_raw else None

    missing = [name for name, val in {
        "OUTREACH_CAMPAIGN": outreach_campaign,
        "GOOGLE_SERVICE_ACCOUNT_JSON": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        "SPREADSHEET_ID": os.environ.get("SPREADSHEET_ID"),
    }.items() if not val]
    if missing:
        print(f"Missing required input(s)/secret(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    shortlist_ws = sheet.worksheet("Shortlist")

    records = shortlist_ws.get_all_records()
    eligible = select_eligible_rows(records, creator_keys)

    if not eligible:
        print("[push] No eligible creators found — nothing to do. (outreach_channel must be "
              "'email' and campaign_push_status must be blank or 'failed'.)")
        return

    print(f"[push] {len(eligible)} eligible creator(s) for campaign '{outreach_campaign}'"
          + (" — DRY RUN, nothing will actually be written." if dry_run else ".") )

    results = push_creators_to_outreach(shortlist_ws, eligible, outreach_campaign, dry_run=dry_run)

    print()
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "pushed":
            print(f"  [pushed]  {r['dedup_key']} -> LeadID {r['outreach_record_id']}")
        elif r["status"] == "preview":
            print(f"  [preview] {r['dedup_key']} -> {r['lead']}")
        elif r["status"] == "failed":
            print(f"  [FAILED]  {r['dedup_key']}: {r['error']}")
        else:
            print(f"  [{r['status']}] {r['dedup_key']}")

    print()
    print(f"[push] Done. {', '.join(f'{v} {k}' for k, v in sorted(counts.items()))}.")

    if counts.get("failed"):
        sys.exit(1)  # non-zero exit so a failed push shows red in Actions, not a quiet green run


if __name__ == "__main__":
    main()
