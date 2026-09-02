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
# Lead Data — same underlying Master rows, sliced into different views
# =============================================================================
st.subheader("Lead Data")

try:
    master_records = connector.get_all_records_from_tab("Master")
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't read the Master tab: {exc}")
    st.stop()

try:
    shortlist_records_for_stage = connector.get_all_records_from_tab("Shortlist")
except Exception:  # noqa: BLE001
    # Shortlist tab may not exist yet on a brand-new sheet — stage
    # computation degrades gracefully (email/dm rows just show as
    # "not yet synced/pushed" / "draft pending", which is accurate).
    shortlist_records_for_stage = []

shortlist_index = crl.index_shortlist_by_key(shortlist_records_for_stage)

campaign_rows = [r for r in master_records if r.get("Campaign") == campaign]
for r in campaign_rows:
    shortlist_row = shortlist_index.get((r.get("dedup_key"), r.get("Campaign", "")))
    r["Stage"] = crl.compute_lifecycle_stage(r, shortlist_row)

tabs = st.tabs(crl.LEAD_DATA_VIEWS)
for tab, view in zip(tabs, crl.LEAD_DATA_VIEWS):
    with tab:
        rows = crl.filter_creator_rows(campaign_rows, view)
        if not rows:
            st.caption(f"No rows in '{view}' for this campaign yet.")
        else:
            # Stage first — the single most useful column to see without
            # scrolling, given everything else on this row.
            display_rows = [
                {"Stage": r.get("Stage", ""), **{k: v for k, v in r.items() if k != "Stage"}}
                for r in rows
            ]
            st.dataframe(display_rows, use_container_width=True)

st.divider()

# =============================================================================
# Review creators — writes review_status/outreach_channel onto Master, via
# the "Update Review Decision" workflow. Streamlit never writes to the
# sheet directly, matching every other write action in this app. Supports
# selecting several creators at once and applying the same decision to
# all of them in one dispatch — the workflow itself already isolates each
# one, so a typo'd key in the middle of a big batch doesn't lose the rest.
# =============================================================================
st.subheader("Review Creators")

pending_rows = crl.filter_creator_rows(campaign_rows, "Main")
review_options = [r["dedup_key"] for r in pending_rows if r.get("dedup_key")]

if not review_options:
    st.caption("No creators found for this campaign yet.")
else:
    selected_keys = st.multiselect("Creators (select one or several)", review_options,
                                    key="review_creator_multiselect")

    if not selected_keys:
        st.caption("Select at least one creator to review.")
    elif len(selected_keys) == 1:
        selected_row = next((r for r in pending_rows if r["dedup_key"] == selected_keys[0]), {})
        with st.expander("Evidence", expanded=True):
            st.write(f"**Overall fit:** {selected_row.get('overall_fit', '—')}")
            st.write(f"**Fit explanation:** {selected_row.get('fit_explanation', '—')}")
            st.write(f"**Content angle:** {selected_row.get('content_angle', '—')}")
            if selected_row.get("recent_post_captions"):
                st.write(f"**Recent captions:** {selected_row['recent_post_captions']}")
            if selected_row.get("dr_concerns"):
                st.warning(f"**Concerns:** {selected_row['dr_concerns']}")
            st.write(f"**Current review status:** {selected_row.get('review_status') or '(pending)'}")
            st.write(f"**Current outreach channel:** {selected_row.get('outreach_channel') or '(none)'}")
    else:
        st.caption(f"{len(selected_keys)} creators selected — the same decision below will be "
                   f"applied to all of them:")
        with st.expander("Selected creators"):
            for key in selected_keys:
                row = next((r for r in pending_rows if r["dedup_key"] == key), {})
                st.write(f"- **{key}** — currently {row.get('review_status') or 'pending'}, "
                         f"channel: {row.get('outreach_channel') or 'none'}")

    new_review_status = st.radio("Review status", ["Approved", "Rejected", "Pending"],
                                  key="review_status_radio")
    new_channel = st.radio("Outreach channel", ["email", "dm", "none"], key="review_channel_radio")

    button_label = f"Save Decision for {len(selected_keys)} creator(s)" if selected_keys else "Save Decision"
    if st.button(button_label, type="primary", key="save_review_decision", disabled=not selected_keys):
        try:
            client = _get_github_client()
            client.dispatch_workflow(config.WORKFLOW_UPDATE_REVIEW_DECISION, {
                "creator_key": ",".join(selected_keys),
                "campaign": campaign,
                "review_status": new_review_status,
                "outreach_channel": new_channel,
            })
            st.success(
                "Dispatched — check the 'Update Review Decision' workflow run in the Actions "
                "tab. Refresh this page once it completes to see the updated status."
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't dispatch the update: {exc}")

st.divider()

# =============================================================================
# Sync Shortlist — a decision saved above only reaches the Shortlist tab
# (what dm_drafting.py and the outreach bridge actually read from) once
# this runs. Deliberately a separate, visible step, not automatic — same
# reasoning as every other stage boundary in this pipeline.
# =============================================================================
st.subheader("Sync Shortlist")
st.caption(
    "A saved decision above updates Master immediately, but doesn't reach the Shortlist tab "
    "— what DM drafting and the 'Push to Outreach' step below actually read from — until this "
    "runs. Safe to run any time; it only ever adds newly-approved rows, never removes anything."
)
if st.button("Run Sync Shortlist Now"):
    try:
        client = _get_github_client()
        client.dispatch_workflow(config.WORKFLOW_SYNC_SHORTLIST, {})
        st.success("Dispatched — check the 'Sync Shortlist' workflow run in the Actions tab.")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't dispatch Sync Shortlist: {exc}")

st.divider()

# =============================================================================
# Push to Outreach — dispatches the existing bridge workflow directly.
# Deliberately does NOT pre-check eligibility here (whether the creator is
# actually in Shortlist yet, whether it's already been pushed) — the
# workflow's own already-tested logic does that and reports clearly in its
# run log; duplicating that check here would mean two places that could
# disagree about what "eligible" means.
# =============================================================================
st.subheader("Push to Outreach")

email_rows = crl.filter_creator_rows(campaign_rows, "Email")
push_options = [r["dedup_key"] for r in email_rows if r.get("dedup_key")]

if not push_options:
    st.caption("No creators routed to Email for this campaign yet — set a creator's "
               "outreach channel to 'email' above first.")
else:
    push_key = st.selectbox("Creator", push_options, key="push_creator_select")
    outreach_campaign_name = st.text_input(
        "Outreach campaign (must match an existing templates/ folder exactly)",
        key="push_campaign_name",
    )
    dry_run = st.checkbox("Dry run (preview only, writes nothing)", value=True, key="push_dry_run")

    if st.button("Push", type="primary", key="push_button", disabled=not outreach_campaign_name):
        try:
            client = _get_github_client()
            client.dispatch_workflow(config.WORKFLOW_PUSH_TO_CAMPAIGN, {
                "outreach_campaign": outreach_campaign_name,
                "dry_run": "true" if dry_run else "false",
                "creator_keys": push_key,
            })
            st.success(
                "Dispatched — check the 'Push Approved to Campaign' workflow run in the Actions "
                "tab for the actual per-creator result (pushed/skipped/failed)."
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't dispatch the push: {exc}")

st.divider()
st.caption(
    "**Not on this page yet:** DM Queue (manually tracking DM outreach outcomes) and Asana "
    "status visibility. Both are separate, still-unbuilt pieces of work."
)
