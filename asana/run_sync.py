"""
run_sync.py — GitHub Actions entry point for asana_sync.py. Handles env
vars, the discovery-sheet connection, and reporting; all the actual sync
logic lives in asana_sync.py.
"""
import os
import sys

import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Explicit here too, not just relied on as a side effect of importing
# asana_sync below — depending on another module's internal sys.path
# fix-up, based purely on import order, is exactly the kind of fragile
# "spooky action at a distance" that broke on the first real test of this
# file. Computed the same way asana_sync.py computes it, independently.
_DISCOVERY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "discovery")
sys.path.insert(0, os.path.normpath(_DISCOVERY_DIR))
import campaign_settings as cs  # noqa: E402
from asana_client import AsanaClient  # noqa: E402
from asana_sync import sync_campaign  # noqa: E402

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# This workflow's job runs with working-directory: asana, but
# campaign_settings.yaml lives at discovery/config/ (relative to the repo
# root) — not "config/campaign_settings.yaml" (discover.py's/shortlist.py's
# own convention, which assumes working-directory: discovery instead).
CAMPAIGN_SETTINGS_PATH = "../discovery/config/campaign_settings.yaml"


def main():
    campaign = os.environ.get("CAMPAIGN", "").strip()
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

    missing = [name for name, val in {
        "CAMPAIGN": campaign,
        "GOOGLE_SERVICE_ACCOUNT_JSON": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        "SPREADSHEET_ID": os.environ.get("SPREADSHEET_ID"),
    }.items() if not val]
    if not dry_run:
        # A real run additionally needs real Asana credentials — a dry
        # run deliberately doesn't, since it never builds a client at all.
        missing += [name for name, val in {
            "ASANA_ACCESS_TOKEN": os.environ.get("ASANA_ACCESS_TOKEN"),
            "ASANA_PROJECT_GID": os.environ.get("ASANA_PROJECT_GID"),
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

    all_settings = cs.load_all_settings(CAMPAIGN_SETTINGS_PATH)
    if not cs.is_asana_sync_enabled(campaign, all_settings):
        print(f"[asana_sync] Campaign '{campaign}' has asana_sync OFF (or was never configured, "
              f"which defaults to OFF) — nothing to do. Enable it on the Creator Research "
              f"page's Campaign Settings first.")
        return

    client = None
    if not dry_run:
        client = AsanaClient(
            access_token=os.environ["ASANA_ACCESS_TOKEN"],
            project_gid=os.environ["ASANA_PROJECT_GID"],
        )

    print(f"[asana_sync] Syncing campaign '{campaign}'" + (" — DRY RUN." if dry_run else "."))

    results = sync_campaign(shortlist_ws, all_settings, client, dry_run=dry_run)

    if not results:
        print("[asana_sync] No eligible rows found (need outreach_channel = email or dm, "
              "and this campaign's asana_sync must be on).")
        return

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "created":
            print(f"  [created] {r['dedup_key']} -> {r['task_gid']}")
        elif r["status"] == "updated":
            print(f"  [updated] {r['dedup_key']} -> {r['task_gid']}")
        elif r["status"] == "preview":
            print(f"  [preview] {r['dedup_key']}: {r['action']} — {r['payload']['name']}")
        elif r["status"] == "failed":
            print(f"  [FAILED]  {r['dedup_key']}: {r['error']}")

    print()
    print(f"[asana_sync] Done. {', '.join(f'{v} {k}' for k, v in sorted(counts.items()))}.")

    if counts.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
