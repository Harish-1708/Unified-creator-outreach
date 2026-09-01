import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
from overview_logic import build_all_campaigns_overview, OVERVIEW_COLUMNS  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("overview")

if not login_gate():
    st.stop()

st.title("📈 Overview")
st.caption(
    "Every campaign at a glance — read-only, same Viewer-scoped credential as the Dashboard page. "
    "A campaign that's never had a Preview/Send/Check Replies run yet has no Sheet tabs, so it's "
    "listed separately below rather than shown as zero."
)


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


def _fetch_campaign_data(campaign_name: str):
    connector = _get_connector()
    campaign_cfg = get_campaign_cfg(campaign_name)
    leads = connector.get_all_leads(campaign_cfg["master_tab"])
    responses = connector.get_all_responses(campaign_cfg["responses_tab"])
    send_log = connector.get_all_send_log(campaign_cfg["send_log_tab"])
    return campaign_cfg, leads, responses, send_log


@st.cache_data(ttl=30, show_spinner=False)
def _load_overview():
    campaign_names = list_campaigns()
    return build_all_campaigns_overview(campaign_names, _fetch_campaign_data)


if st.button("🔄 Refresh now"):
    _load_overview.clear()

try:
    rows, errors = _load_overview()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't build the overview: {exc}")
    st.stop()

if rows:
    total_pending = sum(int(r[OVERVIEW_COLUMNS.index("Pending (Not Yet Contacted)")]) for r in rows)
    total_sent = sum(int(r[OVERVIEW_COLUMNS.index("Total Sent")]) for r in rows)
    total_replies = sum(int(r[OVERVIEW_COLUMNS.index("Replies")]) for r in rows)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Sent (all campaigns)", total_sent)
    col2.metric("Total Pending (all campaigns)", total_pending)
    col3.metric("Total Replies (all campaigns)", total_replies)

    st.divider()
    st.dataframe(
        {col: [row[i] for row in rows] for i, col in enumerate(OVERVIEW_COLUMNS)},
        width="stretch",
        hide_index=True,
    )
else:
    st.info("No campaigns with any activity yet.")

if errors:
    with st.expander(f"{len(errors)} campaign(s) not shown yet (no runs made for them)"):
        for name, message in errors:
            st.write(f"**{name}** — {message}")
