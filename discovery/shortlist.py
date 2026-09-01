"""
shortlist.py — Stage 9: Manual Shortlist Sync.

Self-contained. Run after you've reviewed the Master tab and marked
Shortlisted = Y on rows you've actually checked. Copies those rows into a
Shortlist tab (adding fit_reasoning / personalization_notes / dm_draft /
dm_reasoning / dm_status columns) so dm_drafting.py has something to work
from.

Run only via GitHub Actions — the "Sync Shortlist" workflow. Safe to
re-run, skips rows already present in Shortlist.
"""
import os
import time

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Must match discover.py's SECTOR_HEADERS exactly, or copied rows will be
# misaligned. discover.py derives SECTOR_HEADERS from MASTER_HEADERS minus
# the niche columns — kept as an explicit list here (files stay
# self-contained per the README's own rationale), but every field below is
# a deliberate 1:1 copy of that derivation, including the Campaign /
# review_status / outreach_channel / campaign_push_status fields added
# alongside the unified pipeline. If discover.py's MASTER_HEADERS changes,
# this list must change with it.
SECTOR_HEADERS = [
    "dedup_key", "platform", "profile_link", "username",
    "Campaign",
    "city", "country", "location_verified", "gender_inferred", "gender_confidence",
    "account_type", "account_type_confidence",
    "product_fit_score", "content_opportunity_score", "creator_quality_score",
    "niche_match", "audience_match", "location_match",
    "content_angle_strength", "partnership_signal_score", "overall_fit", "fit_explanation",
    "content_angle", "brand_affinity_note", "partnership_signal_matched", "competitor_affinity",
    "dr_audience_gender", "dr_research_confidence", "dr_concerns", "dr_name",
    "recent_post_captions",
    "outreach_readiness",
    "total_posts", "followers_count", "follower_verification", "follower_source",
    "engagement_rate", "posting_frequency", "audience_quality_score",
    "last_post_date", "activity_status",
    "contact_email", "contact_phone", "contact_source",
    "data_source", "data_confidence",
    "matched_query", "matched_hashtag", "matched_archetype", "matched_lane", "discovery_method",
    "date_added",
    "review_status", "outreach_channel", "campaign_push_status",
]

SHORTLIST_EXTRA_HEADERS = ["fit_reasoning", "personalization_notes", "dm_draft", "dm_reasoning", "dm_status"]
SHORTLIST_HEADERS = SECTOR_HEADERS + SHORTLIST_EXTRA_HEADERS

MAX_RETRIES = 5


def with_backoff(fn, *args, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_error = e
            wait = 2 ** attempt
            print(f"[sheets] API error ({e}), retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Exceeded {MAX_RETRIES} retries writing to Google Sheets") from last_error


def sync_shortlist():
    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ["SPREADSHEET_ID"])

    master = sheet.worksheet("Master")
    try:
        shortlist_tab = sheet.worksheet("Shortlist")
    except gspread.WorksheetNotFound:
        shortlist_tab = sheet.add_worksheet(title="Shortlist", rows=500, cols=len(SHORTLIST_HEADERS) + 2)
        with_backoff(shortlist_tab.append_row, SHORTLIST_HEADERS)

    master_records = with_backoff(master.get_all_records)
    # (dedup_key, Campaign), not dedup_key alone — the same account can be
    # independently approved for two different campaigns, and both need
    # their own Shortlist row (see discover.py's load_master_index()).
    existing_keys = {(r["dedup_key"], r.get("Campaign", ""))
                      for r in with_backoff(shortlist_tab.get_all_records) if r.get("dedup_key")}

    new_rows = []
    for r in master_records:
        if r.get("review_status", "").strip().lower() != "approved":
            continue
        if (r.get("dedup_key"), r.get("Campaign", "")) in existing_keys:
            continue
        row = [r.get(h, "") for h in SECTOR_HEADERS]
        row += ["", "", "", "", "pending_reasoning"]  # fit_reasoning left for you to fill in
        new_rows.append(row)

    if new_rows:
        with_backoff(shortlist_tab.append_rows, new_rows)
        print(f"[shortlist] Added {len(new_rows)} new shortlisted creator(s) to the Shortlist tab.")
        print("  -> Fill in 'fit_reasoning' for each before running the 'Draft DMs' workflow.")
    else:
        print("[shortlist] No new shortlisted rows to sync.")


if __name__ == "__main__":
    sync_shortlist()
