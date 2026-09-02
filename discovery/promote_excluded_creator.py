"""
promote_excluded_creator.py — moves creators from the Excluded tab into
Master, for cases where a human disagrees with the pipeline's automatic
rejection and wants one or more back in the review queue.

Supports a SINGLE creator or a comma-separated BATCH (same convention as
update_review_decision.py/run_push.py's CREATOR_KEYS), each isolated —
one bad key never blocks the rest of the batch.

A genuine MOVE, not a copy: each row is removed from Excluded once it's
been added to Master. Deletions happen in DESCENDING row-number order,
computed once against a single fetched snapshot — deleting a row shifts
every row below it up by one, so processing top-to-bottom would silently
corrupt the row numbers of creators still waiting to be deleted later in
the same batch. Bottom-to-top means every deletion only ever affects rows
already handled, never ones still pending.

Self-contained (reads the real header rows directly) rather than
importing discover.py's EXCLUDED_HEADERS/MASTER_HEADERS, matching every
other script in this build for the same reason.

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
    Excluded don't share an identical column set, so any column not
    present on the source record is left blank, not omitted."""
    return [str(source_record.get(h, "")) for h in header]


def plan_promotions(excluded_records: list, master_records: list, dedup_keys: list, campaign: str) -> list:
    """Pure — no I/O. Validates each requested key independently against
    the two snapshots and returns one result dict per key:
      {"dedup_key", "status": "ready" | "failed",
       "excluded_row_num" (only if ready), "source_record" (only if
       ready), "error" (only if failed)}
    Isolated exactly like update_review_decision.py's
    apply_decision_to_one — one bad key never blocks the rest."""
    results = []
    for dedup_key in dedup_keys:
        excluded_row_num = find_row(excluded_records, dedup_key, campaign)
        if excluded_row_num is None:
            results.append({"dedup_key": dedup_key, "status": "failed",
                             "error": f"No Excluded row found for dedup_key='{dedup_key}', "
                                      f"Campaign='{campaign}'."})
            continue
        if find_row(master_records, dedup_key, campaign) is not None:
            results.append({"dedup_key": dedup_key, "status": "failed",
                             "error": f"'{dedup_key}' already exists in Master under Campaign "
                                      f"'{campaign}' — nothing to promote."})
            continue
        results.append({
            "dedup_key": dedup_key, "status": "ready",
            "excluded_row_num": excluded_row_num,
            "source_record": excluded_records[excluded_row_num - 2],
        })
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
    excluded_ws = sheet.worksheet("Excluded")
    master_ws = sheet.worksheet("Master")

    excluded_records = excluded_ws.get_all_records()
    master_records = master_ws.get_all_records()
    master_header = master_ws.row_values(1)

    plan = plan_promotions(excluded_records, master_records, dedup_keys, campaign)
    ready = [p for p in plan if p["status"] == "ready"]

    # Appends first (order doesn't matter, doesn't shift anything), THEN
    # deletes in descending row-number order (see module docstring for why).
    for item in ready:
        new_row = build_row_for_header(item["source_record"], master_header)
        master_ws.append_row(new_row, value_input_option="RAW")

    for item in sorted(ready, key=lambda p: p["excluded_row_num"], reverse=True):
        excluded_ws.delete_rows(item["excluded_row_num"])

    for item in plan:
        if item["status"] == "ready":
            print(f"[promote_excluded_creator] Moved '{item['dedup_key']}' from Excluded to Master "
                  f"under Campaign '{campaign}'.")
        else:
            print(f"[promote_excluded_creator] {item['dedup_key']}: FAILED — {item['error']}")

    failed = [p for p in plan if p["status"] == "failed"]
    print(f"\n[promote_excluded_creator] Done. {len(ready)} promoted, {len(failed)} failed.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
