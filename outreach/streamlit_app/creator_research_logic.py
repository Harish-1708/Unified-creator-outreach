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
from typing import Dict, List

# Resolved from this file's own location, not the process's current working
# directory — a bare relative path like "../discovery" only works if
# whatever launched Streamlit happens to have that exact cwd, which isn't
# guaranteed. This works regardless of cwd, the same reasoning the bridge's
# PYTHONPATH setup follows for its own cross-folder import.
_DISCOVERY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "discovery")
sys.path.insert(0, os.path.normpath(_DISCOVERY_DIR))
import campaign_settings as cs  # noqa: E402


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


LEAD_DATA_VIEWS = ["Main", "Shortlisted", "Email", "DM", "Response", "Final"]


def filter_shortlist_rows(shortlist_records: List[Dict], view: str) -> List[Dict]:
    """One function for every 'Lead Data' tab — same underlying Shortlist
    rows, sliced differently. Deliberately NOT separate data sources: a
    creator moving from 'Shortlisted' to 'Email' to 'Response' is one row
    changing state, not a row moving between tables.

    Main         — every row (Shortlisted already means review_status ==
                    Approved, by construction of shortlist.py's sync
                    condition — there's no "unapproved" row to further
                    exclude here).
    Shortlisted  — alias for Main, kept as an explicit view name because
                    that's the language used when this was scoped.
    Email        — outreach_channel == email.
    DM           — outreach_channel == dm.
    Response     — has a recorded reply/response signal. Uses dm_status
                    for DM rows (anything past 'pending_reasoning' — a
                    human has actually recorded an outcome); for Email
                    rows, response state lives in outreach.py's OWN
                    Response Sheet, a different spreadsheet entirely, and
                    isn't duplicated here — this view only ever shows DM
                    response state directly.
    Final        — review_status is Rejected. (A DM-side "closed/final"
                    concept will extend this once the DM Queue page exists
                    and defines its own status vocabulary — not guessed at
                    here ahead of that.)
    """
    if view in ("Main", "Shortlisted"):
        return list(shortlist_records)
    if view == "Email":
        return [r for r in shortlist_records if r.get("outreach_channel", "").strip().lower() == "email"]
    if view == "DM":
        return [r for r in shortlist_records if r.get("outreach_channel", "").strip().lower() == "dm"]
    if view == "Response":
        return [
            r for r in shortlist_records
            if r.get("dm_status", "").strip() not in ("", "pending_reasoning")
        ]
    if view == "Final":
        # Only review_status == Rejected is scoped for now. A DM-side
        # notion of "closed" needs the DM Queue's own status vocabulary
        # to exist first — that page hasn't been built yet, so this
        # deliberately does NOT guess at specific dm_status values that
        # aren't a confirmed contract anywhere yet. Revisit once the DM
        # Queue defines its real status set.
        return [r for r in shortlist_records if r.get("review_status", "").strip().lower() == "rejected"]
    raise ValueError(f"Unknown Lead Data view '{view}' — must be one of {LEAD_DATA_VIEWS}")


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
