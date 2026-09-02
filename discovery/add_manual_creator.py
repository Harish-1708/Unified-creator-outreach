"""
add_manual_creator.py — appends ONE manually-entered creator directly to
the Master tab, for outreach that needs to happen without ever running
discovery on that creator (a referral, someone you already know about).

Deliberately self-contained rather than importing discover.py directly —
same reasoning shortlist.py's own docstring already states for this
codebase: discover.py is a large file with real dependencies (Anthropic,
Serper, Tavily API clients) this script has no need to load just to
append one row. Builds the row against whatever the sheet's REAL header
row actually is (matching update_review_decision.py's own approach),
rather than hardcoding MASTER_HEADERS here a second time — any column not
recognized just stays blank, which is correct: a manually-added creator
was never scored by the pipeline, so most columns SHOULD be blank.

Run only via GitHub Actions — the "Add Manual Creator" workflow. Bulk-add
is a client-side concern: the Streamlit page dispatches this workflow
once per creator in a pasted batch, rather than this script accepting a
batch itself — keeps this script's contract identical whether it's one
creator or one of many.
"""
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_manual_creator_dict(platform: str, username: str, campaign: str, profile_link: str = "",
                               contact_email: str = "", content_angle: str = "",
                               review_status: str = "", outreach_channel: str = "") -> dict:
    """Pure — no I/O. dedup_key follows the exact 'platform:username'
    convention every other part of this pipeline already uses."""
    platform_clean = platform.strip().lower()
    username_clean = username.strip()
    return {
        "dedup_key": f"{platform_clean}:{username_clean.lower()}",
        "platform": platform_clean,
        "username": username_clean,
        "profile_link": profile_link.strip() or f"https://{platform_clean}.com/{username_clean}",
        "Campaign": campaign,
        "contact_email": contact_email.strip(),
        "content_angle": content_angle.strip(),
        "review_status": review_status.strip(),
        "outreach_channel": outreach_channel.strip(),
        "data_source": "manual_add",
        "discovery_method": "manual_add",
        "date_added": datetime.now(timezone.utc).date().isoformat(),
    }


def find_existing_row(master_records: list, dedup_key: str, campaign: str):
    """Same compound-key shape as update_review_decision.py's
    find_target_row — returns the 1-indexed sheet row if this exact
    (dedup_key, Campaign) pair already exists, else None. Checked BEFORE
    adding so a manual add can never silently create a duplicate row for
    a creator the pipeline (or a previous manual add) already found."""
    for i, r in enumerate(master_records, start=2):
        if r.get("dedup_key") == dedup_key and r.get("Campaign", "") == campaign:
            return i
    return None


def build_row_for_header(creator: dict, header: list) -> list:
    """Builds a row matching whatever the sheet's ACTUAL header row is —
    not a hardcoded column list — so this stays correct even as
    MASTER_HEADERS grows over time. Any column not present in `creator`
    is left blank, which is the correct, honest state for a manually
    added row: it was never scored, so it has no score."""
    return [str(creator.get(h, "")) for h in header]


def main():
    platform = os.environ.get("PLATFORM", "").strip()
    username = os.environ.get("USERNAME", "").strip()
    campaign = os.environ.get("CAMPAIGN", "").strip()

    missing = [name for name, val in {
        "PLATFORM": platform, "USERNAME": username, "CAMPAIGN": campaign,
    }.items() if not val]
    if missing:
        raise ValueError(f"Missing required input(s): {', '.join(missing)}")

    creator = build_manual_creator_dict(
        platform, username, campaign,
        profile_link=os.environ.get("PROFILE_LINK", ""),
        contact_email=os.environ.get("CONTACT_EMAIL", ""),
        content_angle=os.environ.get("CONTENT_ANGLE", ""),
        review_status=os.environ.get("REVIEW_STATUS", ""),
        outreach_channel=os.environ.get("OUTREACH_CHANNEL", ""),
    )

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    master_ws = sheet.worksheet("Master")

    records = master_ws.get_all_records()
    existing_row = find_existing_row(records, creator["dedup_key"], campaign)
    if existing_row is not None:
        raise ValueError(
            f"'{creator['dedup_key']}' already exists under Campaign '{campaign}' (Master row "
            f"{existing_row}) — use 'Update Review Decision' to change it instead of adding it again."
        )

    header = master_ws.row_values(1)
    row = build_row_for_header(creator, header)
    master_ws.append_row(row, value_input_option="RAW")

    print(f"[add_manual_creator] Added '{creator['dedup_key']}' to Master under Campaign '{campaign}'.")


if __name__ == "__main__":
    main()
