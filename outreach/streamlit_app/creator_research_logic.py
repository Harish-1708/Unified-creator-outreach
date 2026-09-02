"""
creator_research_logic.py — pure logic for the Creator Research + Lead
Data + Campaign Settings pages. No Streamlit widgets, no I/O — every
function takes already-fetched data and returns what the page should
render, matching how every other *_logic.py module in this app is already
structured (testable without mocking Streamlit at all).

Brand and (discovery) Campaign lists are derived from the discovery
sheet's Run Log tab, not a new table — every discovery run already stamps
both brand_name and campaign there.
"""
import os
import sys
from typing import Dict, List, Optional

# Resolved from this file's own location, not the process's current working
# directory — a bare relative path like "../discovery" only works if
# whatever launched Streamlit happens to have that exact cwd, which isn't
# guaranteed. This works regardless of cwd, the same reasoning the bridge's
# PYTHONPATH setup follows for its own cross-folder import.
_DISCOVERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "discovery")
sys.path.insert(0, os.path.normpath(_DISCOVERY_DIR))
import campaign_settings as cs  # noqa: E402
import shortlist as sl  # noqa: E402

DM_STATUS_OPTIONS = sl.DM_STATUS_OPTIONS  # single source of truth stays in shortlist.py


def list_brands(run_log_records: List[Dict]) -> List[str]:
    """Distinct brand_name values from Run Log, sorted."""
    return sorted({r["brand_name"] for r in run_log_records if r.get("brand_name")})


def list_campaigns_for_brand(run_log_records: List[Dict], brand: str) -> List[str]:
    """Distinct Campaign values from Run Log, scoped to one brand."""
    return sorted({
        r["campaign"] for r in run_log_records
        if r.get("brand_name") == brand and r.get("campaign")
    })


def campaign_summary(run_log_records: List[Dict], campaign: str) -> Dict[str, int]:
    """Rolls up this Campaign's Run Log entries into the headline numbers
    the top of the page shows. Uses discover.py's own already-computed
    reconciliation totals (total_found, total_after_filters) — doesn't
    recompute anything discover.py already did the work of tallying."""
    rows = [r for r in run_log_records if r.get("campaign") == campaign]

    def _sum(field):
        total = 0
        for r in rows:
            try:
                total += int(r.get(field) or 0)
            except (TypeError, ValueError):
                continue
        return total

    return {
        "run_count": len(rows),
        "total_found": _sum("total_found"),
        "total_after_filters": _sum("total_after_filters"),
    }


LEAD_DATA_VIEWS = ["Master", "Shortlisted", "Email", "DM", "Response", "Final"]


def filter_creator_rows(master_records: List[Dict], view: str) -> List[Dict]:
    """One function for every 'Lead Data' tab — same underlying MASTER
    rows, sliced differently. Deliberately reads MASTER, not Shortlist:
    Shortlist only ever contains rows where review_status is ALREADY
    "Approved" (that's shortlist.py's own sync condition), so it can never
    show a creator still pending review — exactly the thing a human needs
    to see and act on. Master has review_status/outreach_channel directly
    on it, updated the instant a decision is saved, with no dependency on
    a separate Sync Shortlist step having run yet.

    Master       — every row, including ones nobody has reviewed yet.
                    This is the review queue. (Named to match this
                    pipeline's own Master tab — "Excluded" is a genuinely
                    separate sheet tab, rendered as its own tab directly
                    on the page rather than through this function, since
                    it isn't a filter of Master rows at all.)
    Shortlisted  — review_status == Approved specifically.
    Email        — outreach_channel == email.
    DM           — outreach_channel == dm.
    Response     — has a recorded DM outcome (dm_status set to something
                    other than blank or the "pending_reasoning" default).
                    Email-side response state lives entirely in
                    outreach.py's own Response Sheet, a different
                    spreadsheet, and isn't duplicated here.
    Final        — review_status is Rejected. (A DM-side "closed/final"
                    concept will extend this once the DM Queue page exists
                    and defines its own status vocabulary — not guessed at
                    here ahead of that.)
    """
    if view == "Master":
        return list(master_records)
    if view == "Shortlisted":
        return [r for r in master_records if r.get("review_status", "").strip().lower() == "approved"]
    if view == "Email":
        return [r for r in master_records if r.get("outreach_channel", "").strip().lower() == "email"]
    if view == "DM":
        return [r for r in master_records if r.get("outreach_channel", "").strip().lower() == "dm"]
    if view == "Response":
        return [
            r for r in master_records
            if r.get("dm_status", "").strip() not in ("", "pending_reasoning")
        ]
    if view == "Final":
        return [r for r in master_records if r.get("review_status", "").strip().lower() == "rejected"]
    raise ValueError(f"Unknown Lead Data view '{view}' — must be one of {LEAD_DATA_VIEWS}")


def index_shortlist_by_key(shortlist_records: List[Dict]) -> Dict[tuple, Dict]:
    """(dedup_key, Campaign) -> Shortlist row — same compound key every
    other part of this pipeline uses. Lets compute_lifecycle_stage() look
    up a creator's Shortlist state (if it's been synced there yet)
    without re-scanning the whole list per creator."""
    return {(r.get("dedup_key"), r.get("Campaign", "")): r for r in shortlist_records}


def compute_lifecycle_stage(master_row: Dict, shortlist_row: Optional[Dict] = None) -> str:
    """Derives one human-readable stage from data that already exists on
    Master and (if the creator's been synced) Shortlist. Deliberately does
    NOT reach into outreach.py's own Response Sheet — that's a different
    spreadsheet, and pulling live email-reply state in here would mean
    this function doing its own Sheets I/O rather than staying pure. Email
    reply status stays visible on outreach.py's own Responses page, not
    duplicated here.
    """
    review_status = (master_row.get("review_status") or "").strip().lower()
    if not review_status or review_status == "pending":
        return "Discovered"
    if review_status == "rejected":
        return "Rejected"

    # From here on, review_status == "approved"
    channel = (master_row.get("outreach_channel") or "").strip().lower()
    if not channel or channel == "none":
        return "Approved"

    if channel == "email":
        push_status = (shortlist_row or {}).get("campaign_push_status", "").strip().lower()
        if push_status == "pushed":
            return "Email — Pushed to Outreach"
        if push_status == "failed":
            return "Email — Push Failed"
        return "Approved — Email (not yet synced/pushed)"

    if channel == "dm":
        dm_status = (shortlist_row or {}).get("dm_status", "").strip()
        if not dm_status or dm_status == "pending_reasoning":
            return "Approved — DM (draft pending)"
        return f"DM — {dm_status}"

    return "Approved"  # an outreach_channel value that isn't email/dm/none — unexpected, not fatal


# ---------- Campaign Settings (Asana sync toggle) ----------

# The commit path (used for the GitHub API) is repo-root-relative;
# reading the file locally needs an actual filesystem path — the two are
# NOT the same thing, and conflating them is exactly the kind of mistake
# worth a named constant to prevent.
_LOCAL_CAMPAIGN_SETTINGS_PATH = os.path.join(_DISCOVERY_DIR, "config", "campaign_settings.yaml")


def load_current_settings() -> Dict:
    """Reads whatever's on local disk right now. Same eventual-consistency
    model this app's New Campaign page already uses for its own commits —
    a change committed via Save here won't be visible from THIS reload
    until Streamlit Cloud finishes redeploying, matching that page's own
    "it'll appear... once the app finishes redeploying" behavior. Not a
    new limitation, just the same one applied consistently."""
    return cs.load_all_settings(_LOCAL_CAMPAIGN_SETTINGS_PATH)


def get_asana_sync_status(all_settings: Dict, campaign: str) -> bool:
    """Thin pass-through to campaign_settings — kept here too so pages
    only ever import creator_research_logic, not both modules directly."""
    return cs.is_asana_sync_enabled(campaign, all_settings)


def load_brand_registry() -> list:
    """Reads config/brands.yaml (explicitly-added brands with possibly
    zero campaigns yet) — thin pass-through, same reasoning as
    load_current_settings above."""
    return cs.load_brands(os.path.join(_DISCOVERY_DIR, "config", "brands.yaml"))


def list_all_brands_combined(run_log_records: List[Dict], all_settings: Dict) -> List[str]:
    """Union of Run Log history, explicitly-added brands, and brands
    implied by an explicitly-created campaign — see
    campaign_settings.list_all_brands's own docstring for why all three
    sources matter."""
    run_log_brands = list_brands(run_log_records)
    registry_brands = load_brand_registry()
    return cs.list_all_brands(registry_brands, run_log_brands, all_settings)


def list_all_campaigns_for_brand_combined(run_log_records: List[Dict], brand: str,
                                           all_settings: Dict) -> List[str]:
    run_log_campaigns = list_campaigns_for_brand(run_log_records, brand)
    return cs.list_all_campaigns_for_brand(brand, run_log_campaigns, all_settings)


def build_add_brand_commit(existing_brands: list, new_brand: str) -> Dict[str, object]:
    new_yaml = cs.build_updated_brands_yaml(existing_brands, new_brand)
    return {
        "path": "discovery/config/brands.yaml",
        "content": new_yaml.encode("utf-8"),
        "commit_message": f"Add brand '{new_brand}'",
    }


def build_add_campaign_commit(all_settings: Dict, campaign: str, brand_name: str) -> Dict[str, object]:
    """May raise ValueError (campaign name already used under a different
    brand) — the caller is expected to catch it and show it as an error,
    same as every other write action in this app."""
    new_yaml = cs.build_new_campaign_yaml(all_settings, campaign, brand_name)
    return {
        "path": "discovery/config/campaign_settings.yaml",
        "content": new_yaml.encode("utf-8"),
        "commit_message": f"Add campaign '{campaign}' under brand '{brand_name}'",
    }


def build_settings_commit(all_settings: Dict, campaign: str, asana_sync: bool) -> Dict[str, object]:
    """Returns exactly what the page needs to hand to GitHubClient's
    commit method — the file content plus a clear commit message — without
    this logic module knowing anything about GitHubClient itself."""
    new_yaml = cs.build_updated_settings_yaml(all_settings, campaign, asana_sync)
    action = "Enable" if asana_sync else "Disable"
    return {
        "path": "discovery/config/campaign_settings.yaml",
        "content": new_yaml.encode("utf-8"),
        "commit_message": f"{action} Asana sync for campaign '{campaign}'",
    }
