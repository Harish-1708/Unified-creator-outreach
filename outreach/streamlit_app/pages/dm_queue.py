import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
from github_client import GitHubClient  # noqa: E402
import creator_research_logic as crl  # noqa: E402
import config  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("dm_queue")

if not login_gate():
    st.stop()

st.title("💬 DM Queue")
st.caption(
    "Manual social outreach tracking — reads the discovery pipeline's Shortlist tab (not "
    "Master: dm_draft/dm_status only exist there, once a creator's been synced from Master). "
    "There is no send action anywhere on this page, on purpose — Instagram/TikTok DMs are "
    "always sent by a human, on the platform itself. This page only records what already "
    "happened."
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
        "`discovery_spreadsheet_id` isn't set in Secrets yet — see the Creator Research page "
        "for the same setup note."
    )
    st.stop()

col_refresh, _ = st.columns([1, 5])
with col_refresh:
    if st.button("🔄 Refresh"):
        st.cache_resource.clear()
        st.rerun()

st.page_link("pages/creator_research.py", label="← Back to Creator Research", icon="🔎")

try:
    connector = _get_discovery_connector()
    shortlist_records = connector.get_all_records_from_tab("Shortlist")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read the Shortlist tab: {exc}")
    st.stop()

dm_rows = crl.filter_creator_rows(shortlist_records, "DM")

if not dm_rows:
    st.info(
        "No creators routed to DM yet. A creator shows up here once it's been Approved with "
        "outreach_channel = dm on the Creator Research page, AND synced to Shortlist."
    )
    st.stop()

# =============================================================================
# Queue table — grouped by status so the actual work (what still needs a
# reply, what's genuinely done) is visible without scrolling a flat list.
# =============================================================================
st.subheader("Queue")

status_groups = {}
for r in dm_rows:
    status = r.get("dm_status", "").strip() or "pending_reasoning"
    status_groups.setdefault(status, []).append(r)

for status in crl.DM_STATUS_OPTIONS + ["pending_reasoning"]:
    rows = status_groups.get(status)
    if not rows:
        continue
    with st.expander(f"{status} ({len(rows)})", expanded=(status in ("Not Contacted", "Draft Ready"))):
        for r in rows:
            st.write(f"**{r.get('dedup_key', '—')}** · {r.get('platform', '—')} · "
                     f"Campaign: {r.get('Campaign', '—')}")

st.divider()

# =============================================================================
# Update a creator's DM outcome — the only write action on this page.
# =============================================================================
st.subheader("Update a Creator")

dm_options = [r["dedup_key"] for r in dm_rows if r.get("dedup_key")]
selected_key = st.selectbox("Creator", dm_options, key="dm_creator_select")
selected_row = next((r for r in dm_rows if r["dedup_key"] == selected_key), {})

st.write(f"**Campaign:** {selected_row.get('Campaign', '—')}")
st.write(f"**Platform:** {selected_row.get('platform', '—')}")
st.write(f"**Content angle:** {selected_row.get('content_angle', '—')}")

draft = selected_row.get("dm_draft", "")
if draft:
    st.write("**Draft** (click the copy icon in the top-right of the box):")
    st.code(draft, language=None)
else:
    st.caption("No draft yet — run 'Draft DMs' for this campaign first.")

if selected_row.get("dm_notes"):
    st.caption(f"Previous notes: {selected_row['dm_notes']}")

current_status = selected_row.get("dm_status", "").strip() or "Not Contacted"
status_default_index = (
    crl.DM_STATUS_OPTIONS.index(current_status) if current_status in crl.DM_STATUS_OPTIONS else 0
)
new_status = st.selectbox("Status", crl.DM_STATUS_OPTIONS, index=status_default_index,
                           key="dm_status_select")
new_notes = st.text_area("Notes (optional)", key="dm_notes_input")

if st.button("Save", type="primary", key="save_dm_status"):
    try:
        client = _get_github_client()
        client.dispatch_workflow(config.WORKFLOW_UPDATE_DM_STATUS, {
            "creator_key": selected_key,
            "campaign": selected_row.get("Campaign", ""),
            "dm_status": new_status,
            "dm_notes": new_notes,
        })
        st.success(
            "Dispatched — check the 'Update DM Status' workflow run in the Actions tab. "
            "Refresh this page once it completes to see the updated status."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't dispatch the update: {exc}")
