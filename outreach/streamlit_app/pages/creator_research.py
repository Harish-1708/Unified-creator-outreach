import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
from github_client import GitHubClient  # noqa: E402
import creator_research_logic as crl  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("creator_research")

if not login_gate():
    st.stop()

st.title("🔎 Creator Research")
st.caption(
    "Browses the discovery pipeline's own sheet — a completely separate spreadsheet from "
    "outreach.py's Leads sheet, read here with the same Viewer-scoped credential as the "
    "Overview/Dashboard pages. Approving a creator and pushing them to a real outreach "
    "campaign is a separate action, not yet on this page — see the note at the bottom."
)


@st.cache_resource(show_spinner=False)
def _get_discovery_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("discovery_spreadsheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


if not st.secrets.get("discovery_spreadsheet_id"):
    st.error(
        "`discovery_spreadsheet_id` isn't set in Secrets yet — this is the discovery "
        "pipeline's own Google Sheet ID (a different sheet than `shared_sheet_id`, which is "
        "outreach.py's Leads sheet). Add it, and make sure the same Viewer-scoped service "
        "account under `google_sheets_readonly` has also been shared onto that sheet."
    )
    st.stop()

try:
    connector = _get_discovery_connector()
    run_log_records = connector.get_all_records_from_tab("Run Log")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read the discovery sheet's Run Log: {exc}")
    st.stop()

col_refresh, _ = st.columns([1, 5])
with col_refresh:
    if st.button("🔄 Refresh"):
        st.cache_resource.clear()
        st.rerun()

brands = crl.list_brands(run_log_records)
if not brands:
    st.info("No discovery runs found yet — Run Log is empty. Run the Creator Discovery "
             "Pipeline workflow at least once first.")
    st.stop()

brand = st.selectbox("Brand", brands)
campaigns = crl.list_campaigns_for_brand(run_log_records, brand)
if not campaigns:
    st.info(f"No campaigns found yet for '{brand}'.")
    st.stop()

campaign = st.selectbox("Campaign", campaigns)

summary = crl.campaign_summary(run_log_records, campaign)
s1, s2, s3 = st.columns(3)
s1.metric("Runs", summary["run_count"])
s2.metric("Total found (all runs)", summary["total_found"])
s3.metric("Written to Master (all runs)", summary["total_after_filters"])

st.divider()

# =============================================================================
# Campaign Settings — Asana sync
# =============================================================================
with st.expander("⚙️ Campaign Settings"):
    all_settings = crl.load_current_settings()
    current_status = crl.get_asana_sync_status(all_settings, campaign)

    st.write(
        f"**Asana sync for '{campaign}'**: currently "
        + ("✅ ON" if current_status else "❌ OFF")
    )
    st.caption(
        "Controls BOTH Email and DM creators from this campaign — there's no separate "
        "setting per channel. A campaign that's never been configured defaults to OFF, so a "
        "new test campaign never starts syncing to Asana just because nobody's visited this "
        "page yet."
    )

    new_status = st.toggle("Enable Asana sync for this campaign", value=current_status,
                            key=f"asana_toggle_{campaign}")

    if new_status != current_status:
        if st.button("Save Settings", type="primary", key=f"save_settings_{campaign}"):
            try:
                commit = crl.build_settings_commit(all_settings, campaign, new_status)
                client = _get_github_client()
                client.commit_campaign_files_directly(
                    files=[{"path": commit["path"], "content": commit["content"]}],
                    commit_message=commit["commit_message"],
                )
                st.success(
                    "Saved. It'll take effect here once the app finishes redeploying — "
                    "same as any other settings change in this app."
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't save settings: {exc}")

st.divider()

# =============================================================================
# Lead Data — same underlying Shortlist rows, sliced into different views
# =============================================================================
st.subheader("Lead Data")

try:
    shortlist_records = connector.get_all_records_from_tab("Shortlist")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read the Shortlist tab: {exc}")
    st.stop()

campaign_rows = [r for r in shortlist_records if r.get("Campaign") == campaign]

tabs = st.tabs(crl.LEAD_DATA_VIEWS)
for tab, view in zip(tabs, crl.LEAD_DATA_VIEWS):
    with tab:
        rows = crl.filter_shortlist_rows(campaign_rows, view)
        if not rows:
            st.caption(f"No rows in '{view}' for this campaign yet.")
        else:
            st.dataframe(rows, use_container_width=True)

st.divider()
st.caption(
    "**Not on this page yet:** approving a creator, choosing Email/DM, and pushing to a real "
    "outreach campaign. Right now that's done via the 'Push Approved to Campaign' GitHub "
    "Actions workflow directly. Bringing that action onto this page is the next piece of "
    "work, not yet built."
)
