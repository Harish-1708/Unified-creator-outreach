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
import re
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

# Applied to every view EXCEPT "Master" — Master stays full-width (every
# raw column) since that's the actual review surface; these five are
# meant to be quick, glanceable status checks, not another full spreadsheet.
def reorder_priority_columns(row: Dict, priority_columns: List[str]) -> Dict:
    """Pure — moves priority_columns (in the order given) to the front of
    the dict, keeping everything else in its existing relative order
    after them. Used for the Master tab's full-width display, where
    contact_email otherwise sits wherever MASTER_HEADERS happens to place
    it (in practice, buried well after username, not next to it)."""
    reordered = {col: row[col] for col in priority_columns if col in row}
    reordered.update({k: v for k, v in row.items() if k not in priority_columns})
    return reordered


CURATED_LEAD_COLUMNS = [
    "dedup_key", "username", "contact_email", "platform", "Stage", "overall_fit",
    "review_status", "outreach_channel",
    "campaign_push_status", "outreach_record_id", "dm_status", "content_angle",
]


def curate_row(row: Dict) -> Dict:
    """Pure — returns only the curated columns that actually exist on
    this row, in the defined order. Never raises for a missing column
    (Shortlist and Master don't have an identical column set — dm_status
    is Shortlist-only, for instance), and never invents a value for one
    that isn't there."""
    return {col: row[col] for col in CURATED_LEAD_COLUMNS if col in row}


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


def sanitize_to_outreach_campaign_name(discovery_campaign: str) -> str:
    """Discovery Campaign names commonly have spaces ('DudeRobe Creator
    Discovery'), but outreach campaign names become GitHub folder names
    and are restricted to letters, numbers, and underscores (see
    campaign_builder.CAMPAIGN_NAME_RE). Deterministic — the same discovery
    Campaign always maps to the same outreach name, so 'push this
    creator' never has to ask which name to use."""
    sanitized = re.sub(r"[^A-Za-z0-9_]+", "_", discovery_campaign.strip())
    return sanitized.strip("_") or "Campaign"


def build_single_campaign_table(run_log_records: List[Dict], master_rows_for_campaign: List[Dict],
                                 excluded_rows_for_campaign: List[Dict]) -> List[Dict]:
    """Pure — one row per metric, in the order requested, ready to hand
    straight to st.dataframe/st.table for a clean two-column display.
    Deliberately reuses filter_creator_rows for Shortlisted/Email/DM/
    Response/Final rather than recomputing them a different way — this
    guarantees these numbers always match what you'd count by actually
    opening each Data sub-tab, never a second, differently-derived
    version of the same thing that could quietly disagree with it."""
    total_found = 0
    for r in run_log_records:
        try:
            total_found += int(r.get("total_found") or 0)
        except (TypeError, ValueError):
            continue

    metrics = [
        ("Total Found (all runs)", total_found),
        ("Total Master", len(master_rows_for_campaign)),
        ("Total Excluded", len(excluded_rows_for_campaign)),
        ("Total Shortlisted", len(filter_creator_rows(master_rows_for_campaign, "Shortlisted"))),
        ("Total Email", len(filter_creator_rows(master_rows_for_campaign, "Email"))),
        ("Total DM", len(filter_creator_rows(master_rows_for_campaign, "DM"))),
        ("Total Response", len(filter_creator_rows(master_rows_for_campaign, "Response"))),
        ("Total Final", len(filter_creator_rows(master_rows_for_campaign, "Final"))),
    ]
    return [{"Metric": name, "Count": count} for name, count in metrics]


def build_campaign_analytics(run_log_records: List[Dict], master_records: List[Dict]) -> List[Dict]:
    """Pure — one summary row per (brand, campaign) pair found in Run Log,
    with creator counts pulled from Master. A campaign with real Run Log
    history but zero Master rows yet (a run that found nothing, or hasn't
    finished) still gets a row — showing 0s is more honest than silently
    omitting it."""
    seen = {}
    for r in run_log_records:
        brand = r.get("brand_name", "")
        campaign = r.get("campaign", "")
        if not campaign:
            continue
        key = (brand, campaign)
        if key not in seen:
            seen[key] = {"brand": brand, "campaign": campaign, "runs": 0,
                         "total_found": 0, "total_after_filters": 0}
        seen[key]["runs"] += 1
        for field, out_key in (("total_found", "total_found"), ("total_after_filters", "total_after_filters")):
            try:
                seen[key][out_key] += int(r.get(field) or 0)
            except (TypeError, ValueError):
                continue

    campaign_master_rows: Dict[str, List[Dict]] = {}
    for row in master_records:
        campaign_master_rows.setdefault(row.get("Campaign", ""), []).append(row)

    results = []
    for (brand, campaign), summary in seen.items():
        rows = campaign_master_rows.get(campaign, [])
        approved = sum(1 for r in rows if r.get("review_status", "").strip().lower() == "approved")
        rejected = sum(1 for r in rows if r.get("review_status", "").strip().lower() == "rejected")
        pending = sum(1 for r in rows if r.get("review_status", "").strip().lower() not in ("approved", "rejected"))
        email_count = sum(1 for r in rows if r.get("outreach_channel", "").strip().lower() == "email")
        dm_count = sum(1 for r in rows if r.get("outreach_channel", "").strip().lower() == "dm")
        results.append({
            "Brand": brand, "Campaign": campaign, "Runs": summary["runs"],
            "Found (all runs)": summary["total_found"], "Written to Master": summary["total_after_filters"],
            "In Master now": len(rows), "Approved": approved, "Rejected": rejected, "Pending": pending,
            "Email": email_count, "DM": dm_count,
        })
    return sorted(results, key=lambda r: (r["Brand"], r["Campaign"]))


def build_analytics_totals(campaign_rows: List[Dict]) -> Dict[str, int]:
    """Pure — sums build_campaign_analytics' own output across every
    campaign, for the headline metrics at the top of the Analytics page."""
    totals = {"brands": len({r["Brand"] for r in campaign_rows if r["Brand"]}),
              "campaigns": len(campaign_rows), "runs": 0, "in_master": 0,
              "approved": 0, "rejected": 0, "pending": 0, "email": 0, "dm": 0}
    for r in campaign_rows:
        totals["runs"] += r["Runs"]
        totals["in_master"] += r["In Master now"]
        totals["approved"] += r["Approved"]
        totals["rejected"] += r["Rejected"]
        totals["pending"] += r["Pending"]
        totals["email"] += r["Email"]
        totals["dm"] += r["DM"]
    return totals


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
