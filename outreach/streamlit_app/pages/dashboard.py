import sys
import os

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate  # noqa: E402
from config import REPO_ROOT, SETTINGS_PATH  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError  # noqa: E402
from preview_logic import get_campaign_cfg, list_campaigns  # noqa: E402

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import outreach  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("dashboard")

if not login_gate():
    st.stop()

st.title("📊 Dashboard")
st.caption(
    "Read-only. Uses a Viewer-scoped Google credential — this page can never write to "
    "the Sheet. Numbers are computed with the exact same logic as the Sheet's own "
    "Dashboard tab (outreach.compute_campaign_dashboard), so the two always agree."
)


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id") or _sheet_id_from_settings()
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


def _sheet_id_from_settings() -> str:
    settings = outreach.load_settings(SETTINGS_PATH)
    return settings.get("shared_sheet_id", "")


@st.cache_data(ttl=30, show_spinner=False)
def _load_campaign_data(campaign_name: str):
    connector = _get_connector()
    campaign_cfg = get_campaign_cfg(campaign_name)
    leads = connector.get_all_leads(campaign_cfg["master_tab"])
    responses = connector.get_all_responses(campaign_cfg["responses_tab"])
    send_log = connector.get_all_send_log(campaign_cfg["send_log_tab"])
    error_log = connector.get_all_error_log(campaign_cfg["error_log_tab"])
    return campaign_cfg, leads, responses, send_log, error_log


try:
    campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list campaigns from templates/: {exc}")
    st.stop()

if not campaigns:
    st.info("No campaigns found yet — no subfolders under templates/.")
    st.stop()

col1, col2 = st.columns([2, 1])
with col1:
    selected = st.selectbox("Campaign", campaigns)
with col2:
    if st.button("🔄 Refresh now"):
        _load_campaign_data.clear()

try:
    campaign_cfg, leads, responses, send_log, error_log = _load_campaign_data(selected)
except ReadOnlySheetsError as exc:
    st.warning(str(exc))
    st.stop()
except Exception as exc:  # noqa: BLE001
    st.error(f"Failed to read data for '{selected}': {exc}")
    st.stop()

rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)

sections: dict = {}
for section, metric, value in rows:
    sections.setdefault(section, []).append((metric, value))

overview = dict(sections.get("Overview", []))
metric_cols = st.columns(4)
metric_labels = [
    ("Total Leads (with Email)", "Total Leads"),
    ("Total Emails Sent", "Emails Sent"),
    ("Genuine Replies", "Replies"),
    ("Reply Rate (Replies / Unique Contacted)", "Reply Rate"),
]
for col, (key, label) in zip(metric_cols, metric_labels):
    col.metric(label, overview.get(key, "—"))

st.divider()

tab_names = [s for s in sections.keys() if s != "Overview"]
tabs = st.tabs(["Overview"] + tab_names)

with tabs[0]:
    st.table({"Metric": list(overview.keys()), "Value": list(overview.values())})

for tab, name in zip(tabs[1:], tab_names):
    with tab:
        metrics = sections[name]
        st.table({"Metric": [m for m, _ in metrics], "Value": [v for _, v in metrics]})

st.caption(f"Last updated: {overview.get('Last Updated', '—')} · cached up to 30s")
