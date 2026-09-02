"""
update_review_decision.py — writes a human's review decision (Approved/
Rejected, and which outreach channel) onto Master rows, identified by
(dedup_key, Campaign) — the same compound key every other part of this
pipeline uses, since the same account can legitimately be a candidate
under two different campaigns with two independent decisions.

Supports a SINGLE creator or a comma-separated BATCH (matching the same
convention run_push.py already uses for CREATOR_KEYS) — the same
review_status/outreach_channel gets applied to every key in the batch.
Each key is isolated: one bad key (typo, wrong Campaign, row not found)
never blocks the rest, same error-isolation philosophy as every other
batch operation in this pipeline.

Run only via GitHub Actions — the "Update Review Decision" workflow,
dispatched from the Creator Research Streamlit page. Deliberately a
separate, minimal script rather than adding write logic to discover.py or
shortlist.py: this only ever touches two columns per row, never creates
rows, never runs discovery or scoring logic.

After this runs, the row's review_status/outreach_channel are live on
Master immediately — Sync Shortlist is a SEPARATE, still-manual step
before that row is visible to dm_drafting.py or the outreach bridge (both
of which read Shortlist, not Master). This script does not trigger that
sync itself, on purpose: keeping "record a decision" and "make it visible
to the downstream stages" as two distinct, individually-inspectable
actions, matching how every other stage boundary in this pipeline already
works.
"""
import os

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

VALID_REVIEW_STATUSES = {"approved", "rejected", "pending"}
VALID_OUTREACH_CHANNELS = {"email", "dm", "none", ""}


def find_target_row(master_records: list, dedup_key: str, campaign: str):
    """Pure logic, no I/O — records is get_all_records()' raw output (row
    1 already consumed as header, so the first record is sheet row 2).
    Returns the 1-indexed sheet row number, or None if no row matches
    this exact (dedup_key, Campaign) pair."""
    for i, r in enumerate(master_records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def apply_decision_to_one(master_ws, header: list, records: list, dedup_key: str, campaign: str,
                           review_status: str, outreach_channel: str) -> dict:
    """Isolated per-creator work: find the row, write the two columns,
    report exactly what happened. Never raises — a failure is returned as
    a result dict, same shape as a success, so the caller can report a
    full batch outcome instead of stopping at the first problem."""
    row_num = find_target_row(records, dedup_key, campaign)
    if row_num is None:
        return {"dedup_key": dedup_key, "status": "failed",
                "error": f"No Master row found for dedup_key='{dedup_key}', Campaign='{campaign}' — "
                         f"check both values match exactly (Campaign is case-sensitive)."}

    updates = {"review_status": review_status, "outreach_channel": outreach_channel}
    for col_name, value in updates.items():
        if col_name not in header:
            return {"dedup_key": dedup_key, "status": "failed",
                     "error": f"Master tab has no '{col_name}' column — this repo's discover.py "
                              f"may be an older version than this script expects."}
    for col_name, value in updates.items():
        col_index = header.index(col_name) + 1
        master_ws.update_cell(row_num, col_index, value)

    return {"dedup_key": dedup_key, "status": "saved", "row": row_num}


def main():
    creator_keys_raw = os.environ.get("CREATOR_KEY", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()
    review_status = os.environ.get("REVIEW_STATUS", "").strip()
    outreach_channel = os.environ.get("OUTREACH_CHANNEL", "").strip()

    missing = [name for name, val in {
        "CREATOR_KEY": creator_keys_raw, "CAMPAIGN": campaign, "REVIEW_STATUS": review_status,
    }.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    if review_status.lower() not in VALID_REVIEW_STATUSES:
        raise ValueError(
            f"REVIEW_STATUS must be one of {sorted(VALID_REVIEW_STATUSES)} (case-insensitive), "
            f"got '{review_status}'."
        )
    if outreach_channel.lower() not in VALID_OUTREACH_CHANNELS:
        raise ValueError(
            f"OUTREACH_CHANNEL must be one of {sorted(VALID_OUTREACH_CHANNELS)} (case-insensitive) "
            f"or blank, got '{outreach_channel}'."
        )

    dedup_keys = [k.strip() for k in creator_keys_raw.split(",") if k.strip()]
    if not dedup_keys:
        raise ValueError("CREATOR_KEY parsed to zero non-blank keys.")

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    master_ws = sheet.worksheet("Master")

    # Fetched ONCE, reused for every key — re-fetching per-creator would
    # be needlessly slow for a real batch and risks a row shifting under
    # us mid-batch if two rows in the same batch were ever adjacent and
    # something else wrote concurrently; a single consistent snapshot
    # avoids that entirely.
    records = master_ws.get_all_records()
    header = master_ws.row_values(1)

    results = [
        apply_decision_to_one(master_ws, header, records, key, campaign, review_status, outreach_channel)
        for key in dedup_keys
    ]

    for r in results:
        if r["status"] == "saved":
            print(f"[review] {r['dedup_key']} (Campaign='{campaign}'): review_status={review_status!r}, "
                  f"outreach_channel={outreach_channel!r} — saved to Master row {r['row']}.")
        else:
            print(f"[review] {r['dedup_key']}: FAILED — {r['error']}")

    failed = [r for r in results if r["status"] == "failed"]
    print(f"\n[review] Done. {len(results) - len(failed)} saved, {len(failed)} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
