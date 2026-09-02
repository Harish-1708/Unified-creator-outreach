import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
import creator_research_logic as crl  # noqa: E402

mark_active_page("analytics")

if not login_gate():
    st.stop()

st.title("📈 Analytics")
st.caption("One view across every brand and campaign — no campaign selection needed.")

if not st.secrets.get("discovery_spreadsheet_id"):
    st.error("`discovery_spreadsheet_id` isn't set in Secrets — see the Workspace page's setup note.")
    st.stop()


@st.cache_resource(show_spinner=False)
def _get_discovery_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("discovery_spreadsheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_tab(tab_name: str):
    return _get_discovery_connector().get_all_records_from_tab(tab_name)


if st.button("🔄 Refresh"):
    st.cache_resource.clear()
    st.cache_data.clear()
    st.rerun()

try:
    run_log_records = _fetch_tab("Run Log")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read Run Log: {exc}")
    run_log_records = []

try:
    master_records = _fetch_tab("Master")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read Master: {exc}")
    master_records = []

campaign_rows = crl.build_campaign_analytics(run_log_records, master_records)

if not campaign_rows:
    st.info("No discovery runs found yet.")
    st.stop()

totals = crl.build_analytics_totals(campaign_rows)

st.subheader("Overview")
t1, t2, t3, t4 = st.columns(4)
t1.metric("Brands", totals["brands"])
t2.metric("Campaigns", totals["campaigns"])
t3.metric("Total runs", totals["runs"])
t4.metric("In Master now", totals["in_master"])

t5, t6, t7, t8, t9 = st.columns(5)
t5.metric("Approved", totals["approved"])
t6.metric("Rejected", totals["rejected"])
t7.metric("Pending", totals["pending"])
t8.metric("Routed to Email", totals["email"])
t9.metric("Routed to DM", totals["dm"])

st.divider()
st.subheader("By campaign")

brands = sorted({r["Brand"] for r in campaign_rows if r["Brand"]})
brand_filter = st.selectbox("Filter by brand", ["All"] + brands, key="analytics_brand_filter")
visible_rows = campaign_rows if brand_filter == "All" else [r for r in campaign_rows if r["Brand"] == brand_filter]

st.dataframe(visible_rows, use_container_width=True, hide_index=True)
