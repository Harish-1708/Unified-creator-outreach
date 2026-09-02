"""
workspace.py — everything in one page, one set of tabs, matching how
campaigns.py itself is already structured (st.tabs([...]) under one
selection). This does NOT replace campaigns.py, and does NOT import
anything from it — every tab here calls the SAME already-tested logic
modules campaigns.py's own tabs call (settings_logic, schedule_logic,
sequences_logic, campaign_status_logic, launch_logic, send_logic), just
with new, separate UI code around them. campaigns.py stays exactly as it
is; this page is a second front door onto the same real functionality,
plus the discovery-side pieces (Creator Research, DM Drafting) that
campaigns.py never had at all.

Two tabs are deliberately narrower than their campaigns.py counterpart:
- Responses shows recent responses read-only plus a check-replies-now
  button; the full reply-with-attachments flow (real email sending, file
  uploads) stays on the existing Responses page rather than being
  reproduced here — that specific flow is high-risk enough to send real
  email that duplicating it in a first pass isn't worth the risk.
- Status controls (Launch/Pause/Resume) and the Danger Zone are built
  using the same pure logic functions as campaigns.py's own versions, but
  without that page's own hub-navigation session-state coupling (which
  doesn't exist here) — the underlying writes are identical either way.
"""
import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate, current_user  # noqa: E402
import config  # noqa: E402

if config.REPO_ROOT not in sys.path:
    sys.path.insert(0, config.REPO_ROOT)  # needed for `import outreach` inside campaign_status_logic

from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
from email_account_slots_logic import read_local_slot_mapping  # noqa: E402
from accounts_logic import merge_account_directories  # noqa: E402
from settings_logic import (  # noqa: E402
    load_raw_override, validate_settings, build_updated_override,
    override_to_yaml_bytes, override_file_path,
)
from schedule_logic import (  # noqa: E402
    validate_schedule, build_updated_schedule_override, get_current_schedule,
    timezone_display_name, COMMON_TIMEZONES, DAY_OPTIONS,
)
from sequences_logic import (  # noqa: E402
    get_existing_stages_and_variants, load_variant_content, next_available_variant_letter,
    build_variant_edit_file, build_new_variant_files_for_all_stages, validate_new_variant_contents,
    has_content_changed, can_delete_stage, build_stage_deletion_paths, can_delete_variant,
    build_variant_deletion_paths,
)
from campaign_builder import (  # noqa: E402
    validate_variant_content, validate_campaign_name, build_campaign_files, get_next_stage_for_campaign,
    confirmation_matches_campaign_name, list_campaign_files_to_delete,
)
from campaign_status_logic import (  # noqa: E402
    compute_campaign_readiness, compute_campaign_status, status_label,
    STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_ATTENTION, STATUS_COMPLETED,
)
from launch_logic import build_status_override, build_delete_override  # noqa: E402
from send_logic import confirmation_is_valid, build_send_inputs, build_backfill_thread_subject_inputs  # noqa: E402
import creator_research_logic as crl  # noqa: E402
import outreach  # noqa: E402

mark_active_page("workspace")

if not login_gate():
    st.stop()

st.title("🧭 Workspace")
st.caption(
    "One page, one set of tabs — Creator Research, Campaigns, Email, Schedule, Settings, "
    "Responses, DM Drafting. Every write action here calls the exact same tested logic "
    "campaigns.py's own tabs use; this doesn't rebuild that logic, just gives it a second, "
    "unified front door."
)


def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


@st.cache_resource(show_spinner=False)
def _get_discovery_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("discovery_spreadsheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_discovery_tab(tab_name: str):
    """Cached by tab_name alone (a plain, hashable string) — the connector
    itself doesn't need to be an argument, since _get_discovery_connector
    is already a cached singleton (@st.cache_resource above); calling it
    from inside this cached function doesn't create a new connection.
    Without this, every rerun re-reads every tab from scratch — Streamlit
    reruns the ENTIRE script on almost any click, so a few minutes of
    ordinary use easily exceeds Google's per-minute read quota and returns
    a 429. Same fix, same reasoning as outreach.py's own Campaigns page
    (_fetch_full_campaign_data_cached)."""
    return _get_discovery_connector().get_all_records_from_tab(tab_name)


@st.cache_resource(show_spinner=False)
def _get_outreach_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_outreach_campaign_data(outreach_campaign_name: str):
    campaign_cfg = get_campaign_cfg(outreach_campaign_name)
    connector = _get_outreach_connector()
    leads = connector.get_all_leads(campaign_cfg["master_tab"])
    responses = connector.get_all_responses(campaign_cfg["responses_tab"])
    return leads, responses


HAS_DISCOVERY = bool(st.secrets.get("discovery_spreadsheet_id"))
if not HAS_DISCOVERY:
    st.warning(
        "`discovery_spreadsheet_id` isn't set in Secrets — Creator Research, Campaigns, and DM "
        "Drafting tabs below need it. Email, Schedule, Settings, and Responses still work "
        "without it (they only need `shared_sheet_id`, already required by the rest of this app)."
    )

# =============================================================================
# Brand / Campaign management — search, create, browse. Shared context for
# the discovery-side tabs (Creator Research, Campaigns, DM Drafting).
# Email/Schedule/Settings/Responses have their OWN outreach-campaign
# selector inside their tabs, since a discovery Campaign and an outreach
# campaign are genuinely different concepts that don't always share a
# name (see the bridge's own docstring for why).
# =============================================================================
discovery_campaign = None
discovery_run_log = []
master_records = []
excluded_records = []
if HAS_DISCOVERY:
    try:
        discovery_run_log = _fetch_discovery_tab("Run Log")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't read the discovery sheet: {exc}")
        discovery_run_log = []
    try:
        master_records = _fetch_discovery_tab("Master")
    except Exception:  # noqa: BLE001
        master_records = []
    try:
        excluded_records = _fetch_discovery_tab("Excluded")
    except Exception:  # noqa: BLE001
        excluded_records = []

    all_settings_top = crl.load_current_settings()
    discovery_campaign = st.session_state.get("workspace_active_discovery_campaign")

    # Everything below — search, +Add Brand, +Add Campaign, the brand
    # cards — is the BROWSER. It only renders when nothing is selected
    # yet. Once a campaign IS active, this whole block is skipped
    # entirely (not rendered-then-hidden — never reached at all), and
    # st.stop() below guarantees the tabs section further down is never
    # reached while still in browser mode. The bug this replaces: the
    # browser used to render unconditionally, with the tabs appended
    # below it whenever a campaign was active — showing both at once.
    if not discovery_campaign:
        col_search, col_add_brand, col_add_campaign, col_refresh = st.columns([3, 1, 1, 1])
        with col_search:
            brand_search = st.text_input("🔍 Search brands", label_visibility="collapsed",
                                          placeholder="🔍 Search brands", key="workspace_brand_search")
        with col_add_brand:
            if st.button("➕ Add Brand", width="stretch"):
                st.session_state["workspace_show_add_brand"] = True
        with col_add_campaign:
            if st.button("➕ Add Campaign", width="stretch"):
                st.session_state["workspace_show_add_campaign"] = True
        with col_refresh:
            if st.button("🔄 Refresh", width="stretch"):
                st.cache_resource.clear()
                st.cache_data.clear()
                st.rerun()

        all_brands = crl.list_all_brands_combined(discovery_run_log, all_settings_top)

        if st.session_state.get("workspace_show_add_brand"):
            with st.form("workspace_add_brand_form"):
                st.subheader("Add Brand")
                new_brand_name = st.text_input("Brand name")
                col_save, col_cancel = st.columns(2)
                with col_save:
                    submitted = st.form_submit_button("Save", type="primary")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel")
                if cancelled:
                    st.session_state["workspace_show_add_brand"] = False
                    st.rerun()
                if submitted:
                    if not new_brand_name.strip():
                        st.error("Brand name can't be blank.")
                    else:
                        try:
                            registry = crl.load_brand_registry()
                            commit = crl.build_add_brand_commit(registry, new_brand_name.strip())
                            _get_github_client().commit_campaign_files_directly(
                                files=[{"path": commit["path"], "content": commit["content"]}],
                                commit_message=commit["commit_message"])
                            st.success(f"Brand '{new_brand_name.strip()}' added. It'll appear below once "
                                       "the app finishes redeploying.")
                            st.session_state["workspace_show_add_brand"] = False
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Couldn't add brand: {exc}")

        if st.session_state.get("workspace_show_add_campaign"):
            with st.form("workspace_add_campaign_form"):
                st.subheader("Add Campaign")
                if not all_brands:
                    st.caption("No brands exist yet — add one first.")
                else:
                    campaign_brand = st.selectbox("Brand", all_brands, key="workspace_new_campaign_brand")
                    new_campaign_name = st.text_input("New campaign name")
                col_save, col_cancel = st.columns(2)
                with col_save:
                    submitted = st.form_submit_button("Save", type="primary")
                with col_cancel:
                    cancelled = st.form_submit_button("Cancel")
                if cancelled:
                    st.session_state["workspace_show_add_campaign"] = False
                    st.rerun()
                if submitted and all_brands:
                    if not new_campaign_name.strip():
                        st.error("Campaign name can't be blank.")
                    else:
                        try:
                            commit = crl.build_add_campaign_commit(all_settings_top, new_campaign_name.strip(),
                                                                    campaign_brand)
                            _get_github_client().commit_campaign_files_directly(
                                files=[{"path": commit["path"], "content": commit["content"]}],
                                commit_message=commit["commit_message"])
                            st.success(f"Campaign '{new_campaign_name.strip()}' created under "
                                       f"'{campaign_brand}'. It'll appear below once the app finishes "
                                       f"redeploying.")
                            st.session_state["workspace_show_add_campaign"] = False
                        except ValueError as exc:
                            st.error(str(exc))
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Couldn't add campaign: {exc}")

        st.divider()

        visible_brands = [b for b in all_brands if brand_search.strip().lower() in b.lower()] \
            if brand_search.strip() else all_brands

        if not visible_brands:
            st.info("No brands found yet." if not all_brands else f"No brand matches '{brand_search}'.")
        else:
            for b in visible_brands:
                with st.expander(b, expanded=(b == st.session_state.get("workspace_selected_brand"))):
                    campaigns_for_this_brand = crl.list_all_campaigns_for_brand_combined(
                        discovery_run_log, b, all_settings_top)
                    if not campaigns_for_this_brand:
                        st.caption("No campaigns yet for this brand.")
                    else:
                        for camp in campaigns_for_this_brand:
                            summary = crl.campaign_summary(discovery_run_log, camp)
                            master_count = len([r for r in master_records if r.get("Campaign") == camp])
                            excluded_count = len([r for r in excluded_records if r.get("Campaign") == camp])
                            with st.container(border=True):
                                c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
                                c1.markdown(f"**{camp}**")
                                c2.write(f"{summary['run_count']} run(s)")
                                c3.write(f"{summary['total_found']} found")
                                c4.write(f"{master_count} in Master")
                                c5.write(f"{excluded_count} excluded")
                                if st.button("Open →", key=f"workspace_open_campaign_{b}_{camp}"):
                                    st.session_state["workspace_selected_brand"] = b
                                    st.session_state["workspace_active_discovery_campaign"] = camp
                                    st.rerun()

        st.stop()  # browser mode ends here — the tabs section below is never reached

col_back, col_title = st.columns([1, 5])
with col_back:
    if st.button("← Back to Brands"):
        st.session_state["workspace_active_discovery_campaign"] = None
        st.rerun()
with col_title:
    st.title(discovery_campaign)

# Shared outreach-campaign selector for the Email/Schedule/Settings/
# Responses tabs — placed here, once, clearly above the tab bar, rather
# than sandwiched between two tab declarations. That used to place it
# outside any `with tabs[N]:` block entirely, so it rendered on EVERY
# tab regardless of which one was active — the "same thing shows on
# every page" bug. This is a genuine shared control across those four
# tabs (they're all facets of the same outreach.py campaign), so it
# belongs at this level, visibly, not hidden inside one specific tab.
try:
    outreach_campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list outreach campaigns: {exc}")
    outreach_campaigns = []

outreach_campaign = None
campaign_cfg = None
leads_for_campaign, responses_for_campaign = [], []
if outreach_campaigns:
    outreach_campaign = st.selectbox("Outreach campaign (for Email / Schedule / Settings / Responses)",
                                      outreach_campaigns, key="workspace_outreach_campaign")
    try:
        campaign_cfg = get_campaign_cfg(outreach_campaign)
        is_draft = (campaign_cfg.get("status") or "active") == "draft"
        if not is_draft:
            leads_for_campaign, responses_for_campaign = _fetch_outreach_campaign_data(outreach_campaign)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't load '{outreach_campaign}': {exc}")
        campaign_cfg = None

tab_names = ["🔎 Creator Research", "🗂️ Campaigns", "✉️ Email", "📅 Schedule", "⚙️ Settings",
             "💬 Responses", "📱 DM Drafting"]
tabs = st.tabs(tab_names)

# =============================================================================
# TAB 1 — Creator Research
# =============================================================================
with tabs[0]:
    summary = crl.campaign_summary(discovery_run_log, discovery_campaign)
    s1, s2, s3 = st.columns(3)
    s1.metric("Runs", summary["run_count"])
    s2.metric("Total found (all runs)", summary["total_found"])
    s3.metric("Written to Master (all runs)", summary["total_after_filters"])

    # Reuses master_records/excluded_records fetched once at the top of
    # the page (for the brand-card stats above) rather than re-reading
    # the same tabs again here.
    cr_master_rows = [r for r in master_records if r.get("Campaign") == discovery_campaign]
    cr_excluded_rows = [r for r in excluded_records if r.get("Campaign") == discovery_campaign]

    st.write(f"**Master:** {len(cr_master_rows)} row(s) · **Excluded:** {len(cr_excluded_rows)} row(s)")

    with st.expander("Master data", expanded=True):
        if cr_master_rows:
            st.dataframe(cr_master_rows, use_container_width=True)
        else:
            st.caption("No Master rows for this campaign yet.")
    with st.expander("Excluded data"):
        if cr_excluded_rows:
            st.dataframe(cr_excluded_rows, use_container_width=True)
        else:
            st.caption("No excluded rows for this campaign yet.")

    st.divider()

    # =========================================================================
    # Run New Research — dispatches the real discover.yml, using its exact
    # current input schema (verified against the actual file, not guessed).
    # campaign is locked to the campaign already open on this page — a new
    # run here always adds to THIS campaign, matching how every other
    # action on this page already scopes to the currently open campaign.
    # =========================================================================
    st.subheader("▶ Run New Research")

    run_state_key = f"workspace_discover_run_{discovery_campaign}"

    with st.form("workspace_run_discovery_form"):
        st.caption(f"Campaign: **{discovery_campaign}** (this run adds to it)")
        default_brand = st.session_state.get("workspace_selected_brand", "")
        brand_name_input = st.text_input("Brand name", value=default_brand,
                                          key="workspace_discover_brand_name")

        c1, c2 = st.columns(2)
        with c1:
            niche_input = st.text_input("Niche (e.g. loungewear)", key="workspace_discover_niche")
            location_input = st.text_input("Location (city/country)", key="workspace_discover_location")
            platform_input = st.selectbox("Platform", ["both", "instagram", "tiktok"],
                                           key="workspace_discover_platform")
        with c2:
            target_gender_input = st.selectbox("Target gender", ["both", "male", "female"],
                                                key="workspace_discover_target_gender")
            result_limit_input = st.number_input("Result limit (qualified creators, 1-60)",
                                                   min_value=1, max_value=60, value=5,
                                                   key="workspace_discover_result_limit")

        brand_website_input = st.text_input("Brand website (optional)", key="workspace_discover_website")
        brand_brief_input = st.text_area("Brand brief (optional but high-value — what it is, who buys "
                                          "it, when it's used)", key="workspace_discover_brief", height=100)

        with st.expander("Advanced options"):
            target_buyer_input = st.text_input(
                "Target buyer (optional, if different from the creator's audience)",
                key="workspace_discover_target_buyer")
            use_cases_input = st.text_input("Use cases (optional, e.g. 'gym, shower, beach, travel')",
                                             key="workspace_discover_use_cases")
            creator_types_input = st.text_input(
                "Creator types to prioritize (optional, e.g. 'fitness dads, men's lifestyle')",
                key="workspace_discover_creator_types")
            exclude_input = st.text_input(
                "Exclude (optional, e.g. 'children's apparel, lingerie, retailers')",
                key="workspace_discover_exclude")
            competitor_brands_input = st.text_input("Competitor brands (comma-separated, optional)",
                                                      key="workspace_discover_competitors")

            ac1, ac2 = st.columns(2)
            with ac1:
                search_budget_input = st.text_input("Search budget (optional, auto-scaled if blank)",
                                                      key="workspace_discover_search_budget")
                min_followers_input = st.text_input("Min followers (optional, e.g. 50k)",
                                                      key="workspace_discover_min_followers")
                min_overall_fit_input = st.number_input("Min overall fit (0-10)", min_value=0, max_value=10,
                                                          value=5, key="workspace_discover_min_fit")
                creator_size_tier_input = st.selectbox(
                    "Creator size tier (optional — overrides min/max followers)",
                    ["", "emerging", "mid", "large", "mega"], key="workspace_discover_size_tier")
            with ac2:
                llm_candidate_limit_input = st.text_input("LLM candidate limit (optional, auto if blank)",
                                                            key="workspace_discover_llm_limit")
                max_followers_input = st.text_input("Max followers (optional, e.g. 5 lakh)",
                                                      key="workspace_discover_max_followers")
                unknown_followers_policy_input = st.selectbox(
                    "Unknown followers policy", ["needs_verification", "include"],
                    key="workspace_discover_unknown_followers")

            require_activity_verified_input = st.checkbox(
                "Require verified recent-post date (leave off for Serper-only enrichment)",
                key="workspace_discover_require_activity")
            sonnet_refinement_input = st.checkbox(
                "Sonnet refinement (more critical second pass — costs more per run)",
                key="workspace_discover_sonnet_refinement")
            weight_overrides_input = st.text_input(
                "Fit weight overrides (optional, e.g. 'product_fit=0.4,audience=0.2')",
                key="workspace_discover_weight_overrides")
            search_vocabulary_input = st.text_area(
                "Search vocabulary (optional — your own terms/hashtags/archetypes; leave blank to let "
                "Claude expand it)", key="workspace_discover_search_vocab", height=80)

        run_submitted = st.form_submit_button("🚀 Run Research", type="primary")

    if run_submitted:
        missing_required = [name for name, val in {
            "Brand name": brand_name_input, "Niche": niche_input, "Location": location_input,
        }.items() if not val.strip()]
        if missing_required:
            st.error(f"Required: {', '.join(missing_required)}")
        else:
            try:
                client = _get_github_client()
                run_details = client.dispatch_workflow(config.WORKFLOW_DISCOVER, {
                    "campaign": discovery_campaign,
                    "niche": niche_input,
                    "brand_name": brand_name_input,
                    "brand_website": brand_website_input,
                    "brand_brief": brand_brief_input,
                    "target_buyer": target_buyer_input,
                    "use_cases": use_cases_input,
                    "creator_types": creator_types_input,
                    "exclude": exclude_input,
                    "location": location_input,
                    "platform": platform_input,
                    "target_gender": target_gender_input,
                    "result_limit": str(result_limit_input),
                    "search_budget": search_budget_input,
                    "llm_candidate_limit": llm_candidate_limit_input,
                    "min_followers": min_followers_input,
                    "max_followers": max_followers_input,
                    "unknown_followers_policy": unknown_followers_policy_input,
                    "min_overall_fit": str(min_overall_fit_input),
                    "require_activity_verified": "true" if require_activity_verified_input else "false",
                    "sonnet_refinement": "true" if sonnet_refinement_input else "false",
                    "competitor_brands": competitor_brands_input,
                    "weight_overrides": weight_overrides_input,
                    "search_vocabulary": search_vocabulary_input,
                    "creator_size_tier": creator_size_tier_input,
                })
                if run_details is None:
                    time.sleep(2)
                    run_details = client.find_recent_run(config.WORKFLOW_DISCOVER)
                st.session_state[run_state_key] = {
                    "run_id": (run_details or {}).get("id") or (run_details or {}).get("run_id"),
                    "run_url": (run_details or {}).get("html_url", ""),
                    "started_at": time.time(),
                }
                st.success(f"Dispatched — running discovery for '{discovery_campaign}'.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't dispatch: {exc}")

    # ---------- Live status ----------
    run_info = st.session_state.get(run_state_key)
    if run_info:
        st.divider()
        elapsed_seconds = int(time.time() - run_info["started_at"])
        elapsed_display = f"{elapsed_seconds // 60}m {elapsed_seconds % 60}s"

        status_text, conclusion = "unknown", None
        if run_info.get("run_id"):
            try:
                run = _get_github_client().get_run(run_info["run_id"])
                status_text = run.get("status", "unknown")
                conclusion = run.get("conclusion")
            except Exception:  # noqa: BLE001
                pass

        if status_text == "completed":
            icon = "✅" if conclusion == "success" else "❌"
            st.write(f"{icon} **Completed** — conclusion: {conclusion}. Took {elapsed_display}.")
            st.caption("Refresh the page (or click Refresh at the top) to see the new Master/Excluded rows.")
        else:
            st.write(f"🟢 **Running** — status: {status_text}. Elapsed: {elapsed_display}.")
            st.caption("Typical runs take ~15-30 minutes depending on result_limit and search_budget — "
                       "there's no per-step progress signal available, only overall run status.")

        col_refresh_run, col_clear_run, col_link = st.columns([1, 1, 2])
        with col_refresh_run:
            if st.button("🔄 Refresh Status", key="workspace_refresh_discover_run"):
                st.rerun()
        with col_clear_run:
            if st.button("✕ Dismiss", key="workspace_dismiss_discover_run"):
                del st.session_state[run_state_key]
                st.rerun()
        with col_link:
            if run_info.get("run_url"):
                st.markdown(f"[View full log in Actions]({run_info['run_url']})")

# =============================================================================
# TAB 2 — Campaigns (Master | Excluded | Shortlist | Email | DM | Response
# | Final), plus bulk review/approve — the same content as the old
# standalone Creator Research page's Lead Data + Review sections.
# =============================================================================
with tabs[1]:
    if not HAS_DISCOVERY or not discovery_campaign:
        st.caption("Select a Brand and Discovery Campaign above.")
    else:
        # master_records/excluded_records reused from the top-level fetch
        # (used for the brand cards) — only Shortlist is fetched fresh
        # here, since nothing else on the page needs it yet.
        try:
            shortlist_records = _fetch_discovery_tab("Shortlist")
        except Exception:  # noqa: BLE001
            shortlist_records = []

        shortlist_index = crl.index_shortlist_by_key(shortlist_records)
        campaign_rows = [r for r in master_records if r.get("Campaign") == discovery_campaign]
        for r in campaign_rows:
            sl_row = shortlist_index.get((r.get("dedup_key"), r.get("Campaign", "")))
            r["Stage"] = crl.compute_lifecycle_stage(r, sl_row)
        excluded_campaign_rows = [r for r in excluded_records if r.get("Campaign") == discovery_campaign]

        inner_tab_names = [crl.LEAD_DATA_VIEWS[0], "Excluded"] + crl.LEAD_DATA_VIEWS[1:]
        inner_tabs = st.tabs(inner_tab_names)

        with inner_tabs[0]:
            rows = crl.filter_creator_rows(campaign_rows, "Master")
            if rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.caption("No rows yet.")
        with inner_tabs[1]:
            if excluded_campaign_rows:
                st.dataframe(excluded_campaign_rows, use_container_width=True)
            else:
                st.caption("No excluded rows yet.")
        for inner_tab, view in zip(inner_tabs[2:], crl.LEAD_DATA_VIEWS[1:]):
            with inner_tab:
                rows = crl.filter_creator_rows(campaign_rows, view)
                if rows:
                    st.dataframe(rows, use_container_width=True)
                else:
                    st.caption(f"No rows in '{view}' yet.")

        st.divider()
        st.subheader("Review Creators (Master \u2192 approve/reject, choose channel)")
        pending_rows = crl.filter_creator_rows(campaign_rows, "Master")
        review_options = [r["dedup_key"] for r in pending_rows if r.get("dedup_key")]

        if not review_options:
            st.caption("No creators found for this campaign yet.")
        else:
            selected_keys = st.multiselect("Creators (select one or several)", review_options,
                                            key="workspace_review_multiselect")
            if selected_keys:
                new_review_status = st.radio("Review status", ["Approved", "Rejected", "Pending"],
                                              key="workspace_review_status_radio")
                new_channel = st.radio("Outreach channel", ["email", "dm", "none"],
                                        key="workspace_review_channel_radio")
                if st.button(f"Save Decision for {len(selected_keys)} creator(s)", type="primary",
                             key="workspace_save_review_decision"):
                    try:
                        client = _get_github_client()
                        client.dispatch_workflow(config.WORKFLOW_UPDATE_REVIEW_DECISION, {
                            "creator_key": ",".join(selected_keys),
                            "campaign": discovery_campaign,
                            "review_status": new_review_status,
                            "outreach_channel": new_channel,
                        })
                        st.success("Dispatched — check 'Update Review Decision' in the Actions tab.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Couldn't dispatch: {exc}")

        st.divider()
        st.subheader("Push to Outreach")
        email_rows = crl.filter_creator_rows(campaign_rows, "Email")
        push_options = [r["dedup_key"] for r in email_rows if r.get("dedup_key")]
        if push_options:
            push_key = st.selectbox("Creator", push_options, key="workspace_push_creator")

            try:
                existing_outreach_campaigns = list_campaigns()
            except Exception:  # noqa: BLE001
                existing_outreach_campaigns = []

            # Genuinely two different destinations, made explicit rather
            # than forcing every push through the separate "Outreach
            # campaign" selector at the top of the Email/Schedule/
            # Settings/Responses tabs — that selector is for MANAGING an
            # outreach campaign that already exists; this is for GETTING
            # a creator into one, new or existing, without leaving this
            # section at all.
            push_target_mode = st.radio("Send to", ["Existing outreach campaign", "+ Create new campaign"],
                                         key="workspace_push_target_mode", horizontal=True)

            if push_target_mode == "Existing outreach campaign":
                if not existing_outreach_campaigns:
                    st.caption("No outreach campaigns exist yet — use '+ Create new campaign' instead.")
                    push_campaign_name = None
                else:
                    push_campaign_name = st.selectbox("Outreach campaign", existing_outreach_campaigns,
                                                        key="workspace_push_existing_campaign")
            else:
                new_outreach_campaign_name = st.text_input("New campaign name",
                                                             key="workspace_push_new_campaign_name")
                st.caption("Needs a first email — this becomes the Intro, Variant A. You can add more "
                           "variants and follow-up stages later from the Email tab.")
                new_subject = st.text_input("Subject", key="workspace_push_new_campaign_subject")
                new_body = st.text_area("Body", key="workspace_push_new_campaign_body", height=120)
                push_campaign_name = new_outreach_campaign_name.strip() or None

            dry_run_push = st.checkbox("Dry run", value=True, key="workspace_push_dry_run")

            if st.button("Push", type="primary", key="workspace_push_button", disabled=not push_campaign_name):
                validation_error = None
                if push_target_mode == "+ Create new campaign":
                    validation_error = (
                        validate_campaign_name(push_campaign_name, existing_outreach_campaigns)
                        or validate_variant_content(new_subject, new_body, is_first_stage=True)
                    )

                if validation_error:
                    st.error(validation_error)
                else:
                    try:
                        client = _get_github_client()

                        if push_target_mode == "+ Create new campaign":
                            files = build_campaign_files(push_campaign_name, "intro",
                                                          {"A": {"subject": new_subject, "body": new_body}})
                            client.commit_campaign_files_directly(
                                files=files,
                                commit_message=f"Create campaign '{push_campaign_name}' (via Workspace, "
                                               f"by {current_user()})")
                            st.info(f"Campaign '{push_campaign_name}' created. Waiting a moment for it "
                                    f"to register before pushing...")
                            time.sleep(3)

                        client.dispatch_workflow(config.WORKFLOW_PUSH_TO_CAMPAIGN, {
                            "outreach_campaign": push_campaign_name,
                            "dry_run": "true" if dry_run_push else "false",
                            "creator_keys": push_key,
                        })
                        st.success("Dispatched — check 'Push Approved to Campaign' in the Actions tab.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Couldn't dispatch: {exc}")
        else:
            st.caption("No creators routed to Email yet.")

        st.divider()
        st.subheader("+ Add Creator")
        st.caption(
            "For creators that never went through discovery — a referral, someone you already know "
            "about. Adds directly to Master, same as a discovered row, just without any scores "
            "(nothing scored it). Fails clearly rather than duplicating if this creator already "
            "exists under this Campaign."
        )
        add_mode = st.radio("Mode", ["Single", "Bulk"], key="workspace_add_creator_mode", horizontal=True)

        if add_mode == "Single":
            ac1, ac2 = st.columns(2)
            with ac1:
                new_platform = st.text_input("Platform", key="workspace_add_platform",
                                              placeholder="instagram")
                new_username = st.text_input("Username (no @)", key="workspace_add_username")
                new_email = st.text_input("Contact email (optional)", key="workspace_add_email")
            with ac2:
                new_profile_link = st.text_input("Profile link (optional)", key="workspace_add_link")
                new_review_status = st.selectbox("Review status", ["", "Approved", "Rejected", "Pending"],
                                                  key="workspace_add_review_status")
                new_channel = st.selectbox("Outreach channel", ["", "email", "dm", "none"],
                                            key="workspace_add_channel")
            new_content_angle = st.text_area("Notes (optional)", key="workspace_add_notes")

            if st.button("Add Creator", type="primary", key="workspace_add_creator_button",
                         disabled=not (new_platform and new_username)):
                try:
                    client = _get_github_client()
                    client.dispatch_workflow(config.WORKFLOW_ADD_MANUAL_CREATOR, {
                        "platform": new_platform,
                        "username": new_username,
                        "campaign": discovery_campaign,
                        "profile_link": new_profile_link,
                        "contact_email": new_email,
                        "content_angle": new_content_angle,
                        "review_status": new_review_status,
                        "outreach_channel": new_channel,
                    })
                    st.success("Dispatched — check 'Add Manual Creator' in the Actions tab.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Couldn't dispatch: {exc}")
        else:
            st.caption(
                "One creator per line: `platform,username,email,profile_link` — email and profile_link "
                "are optional but the commas must still be there (e.g. `instagram,dudedad,,`). Review "
                "status and channel apply to every row in this batch."
            )
            bulk_text = st.text_area("Creators", height=150, key="workspace_bulk_add_text",
                                      placeholder="instagram,dudedad,,\ntiktok,someuser,someone@example.com,")
            bc1, bc2 = st.columns(2)
            with bc1:
                bulk_review_status = st.selectbox("Review status", ["", "Approved", "Rejected", "Pending"],
                                                   key="workspace_bulk_review_status")
            with bc2:
                bulk_channel = st.selectbox("Outreach channel", ["", "email", "dm", "none"],
                                             key="workspace_bulk_channel")

            if st.button("Add All", type="primary", key="workspace_bulk_add_button",
                         disabled=not bulk_text.strip()):
                lines = [ln.strip() for ln in bulk_text.strip().splitlines() if ln.strip()]
                client = _get_github_client()
                dispatched, failed_lines = 0, []
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 2 or not parts[0] or not parts[1]:
                        failed_lines.append(line)
                        continue
                    platform_val, username_val = parts[0], parts[1]
                    email_val = parts[2] if len(parts) > 2 else ""
                    link_val = parts[3] if len(parts) > 3 else ""
                    try:
                        client.dispatch_workflow(config.WORKFLOW_ADD_MANUAL_CREATOR, {
                            "platform": platform_val,
                            "username": username_val,
                            "campaign": discovery_campaign,
                            "profile_link": link_val,
                            "contact_email": email_val,
                            "review_status": bulk_review_status,
                            "outreach_channel": bulk_channel,
                        })
                        dispatched += 1
                    except Exception:  # noqa: BLE001
                        failed_lines.append(line)
                st.success(f"Dispatched {dispatched} creator(s) — check 'Add Manual Creator' runs in "
                           f"the Actions tab.")
                if failed_lines:
                    st.error(f"{len(failed_lines)} line(s) couldn't be parsed or dispatched: "
                             + "; ".join(failed_lines))

# =============================================================================
# TAB 3 — Email (Sequences)
# =============================================================================
with tabs[2]:
    if not campaign_cfg:
        st.caption("No outreach campaign selected/loaded.")
    else:
        cname = campaign_cfg["_campaign_name"]
        try:
            stages, existing_variants = get_existing_stages_and_variants(cname, config.TEMPLATES_ROOT)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't read templates for '{cname}': {exc}")
            stages, existing_variants = [], []

        sample_lead = leads_for_campaign[0] if leads_for_campaign else {}

        if stages:
            st.subheader("Edit templates")
            pending_edits = {}
            for idx, stage in enumerate(stages):
                prefix = stage["template_prefix"]
                is_first = (idx == 0)
                st.markdown(f"**{stage['name']}**")
                for variant in existing_variants:
                    try:
                        original = load_variant_content(cname, prefix, variant, config.TEMPLATES_ROOT)
                    except Exception as exc:  # noqa: BLE001
                        st.warning(f"{prefix}_{variant}: {exc}")
                        continue
                    with st.expander(f"Variant {variant}"):
                        unlocked = st.checkbox("🔓 Unlock to edit", key=f"ws_unlock_{prefix}_{variant}")
                        subject = st.text_input("Subject", value=original["subject"],
                                                 key=f"ws_subject_{prefix}_{variant}", disabled=not unlocked)
                        body = st.text_area("Body", value=original["body"], height=150,
                                             key=f"ws_body_{prefix}_{variant}", disabled=not unlocked)
                        if unlocked and has_content_changed(original, subject, body):
                            pending_edits[(prefix, variant)] = (subject, body, is_first)
                            st.caption("✏️ Changed — will be saved")
                        if sample_lead:
                            st.markdown("**Preview**")
                            st.write(f"Subject: {outreach.render_text(subject, sample_lead)}")
                            st.text(outreach.render_text(body, sample_lead))

            if pending_edits:
                errors = [f"{p}_{v}: {e}" for (p, v), (s, b, isf) in pending_edits.items()
                          for e in [validate_variant_content(s, b, is_first_stage=isf)] if e]
                if errors:
                    for e in errors:
                        st.error(e)
                elif st.button(f"💾 Save Changes ({len(pending_edits)})", type="primary", key="ws_save_edits"):
                    try:
                        files = [build_variant_edit_file(cname, p, v, s, b)
                                 for (p, v), (s, b, _) in pending_edits.items()]
                        client = _get_github_client()
                        client.commit_campaign_files_directly(
                            files=files, commit_message=f"Edit {len(files)} template(s) in {cname} "
                                                         f"(via Workspace, by {current_user()})")
                        st.success(f"Saved {len(files)} template(s).")
                    except GitHubActionsError as exc:
                        st.error(f"Save failed: {exc}")

            st.divider()
            next_letter = next_available_variant_letter(existing_variants)
            with st.expander(f"➕ Add variant {next_letter}" if next_letter else "➕ Add variant (maximum reached)"):
                if not next_letter:
                    st.info("Every stage already has all 4 variants (A–D).")
                else:
                    contents_by_stage = {}
                    for stage in stages:
                        prefix = stage["template_prefix"]
                        st.markdown(f"**{stage['name']}**")
                        subj = st.text_input("Subject", key=f"ws_newvar_subj_{prefix}")
                        bod = st.text_area("Body", key=f"ws_newvar_body_{prefix}", height=120)
                        contents_by_stage[prefix] = {"subject": subj, "body": bod}
                    if st.button(f"Add Variant {next_letter}", type="primary", key="ws_add_variant"):
                        errs = validate_new_variant_contents(stages, contents_by_stage)
                        if errs:
                            for e in errs:
                                st.error(e)
                        else:
                            try:
                                files = build_new_variant_files_for_all_stages(cname, stages, next_letter,
                                                                                contents_by_stage)
                                client = _get_github_client()
                                client.commit_campaign_files_directly(
                                    files=files, commit_message=f"Add variant {next_letter} to {cname} "
                                                                 f"(via Workspace, by {current_user()})")
                                st.success(f"Variant {next_letter} added.")
                            except GitHubActionsError as exc:
                                st.error(f"Failed: {exc}")

            try:
                next_stage = get_next_stage_for_campaign(cname, config.TEMPLATES_ROOT)
            except Exception as exc:  # noqa: BLE001
                next_stage = None
                st.error(f"Couldn't determine next stage: {exc}")
            with st.expander("➕ Add a follow-up stage" if next_stage else "➕ Add a follow-up stage (none left)"):
                if next_stage is None:
                    st.info("This campaign already has all 5 stages.")
                else:
                    stage_prefix, required_variants = next_stage
                    st.write(f"**Next stage:** `{stage_prefix}` · **Variants:** {', '.join(required_variants)}")
                    variant_inputs = {}
                    for letter in required_variants:
                        st.markdown(f"**Variant {letter}**")
                        subj = st.text_input("Subject (blank continues thread)", key=f"ws_fu_subj_{letter}")
                        bod = st.text_area("Body", key=f"ws_fu_body_{letter}", height=120)
                        variant_inputs[letter] = {"subject": subj, "body": bod}
                    if st.button(f"Add {stage_prefix}", type="primary", key="ws_add_stage"):
                        errs = [f"Variant {letter}: {e}" for letter, c in variant_inputs.items()
                                for e in [validate_variant_content(c["subject"], c["body"], is_first_stage=False)] if e]
                        if errs:
                            for e in errs:
                                st.error(e)
                        else:
                            try:
                                files = build_campaign_files(cname, stage_prefix, variant_inputs)
                                client = _get_github_client()
                                client.commit_campaign_files_directly(
                                    files=files, commit_message=f"Add {stage_prefix} to {cname} "
                                                                 f"(via Workspace, by {current_user()})")
                                st.success(f"'{stage_prefix}' added.")
                            except GitHubActionsError as exc:
                                st.error(f"Failed: {exc}")

            with st.expander("🗑️ Delete a variant"):
                variant_to_delete = st.selectbox("Variant to delete", existing_variants, key="ws_delete_variant")
                can_del, reason = can_delete_variant(existing_variants, variant_to_delete)
                if can_del:
                    st.warning(f"Removes variant {variant_to_delete} from EVERY stage — not recoverable.")
                    confirm = st.checkbox(f"I understand, delete variant {variant_to_delete}",
                                           key="ws_confirm_delete_variant")
                    if st.button(f"Delete Variant {variant_to_delete}", disabled=not confirm,
                                 key="ws_delete_variant_btn"):
                        try:
                            paths = build_variant_deletion_paths(cname, stages, variant_to_delete)
                            client = _get_github_client()
                            for path in paths:
                                client.delete_file(path, message=f"Delete variant {variant_to_delete} from "
                                                                   f"{cname} (via Workspace, by {current_user()})")
                            st.success(f"Variant {variant_to_delete} deleted from {len(paths)} stage(s).")
                        except GitHubActionsError as exc:
                            st.error(f"Failed: {exc}")
                else:
                    st.info(reason)

            with st.expander("🗑️ Delete the last stage"):
                last_stage = stages[-1]
                can_del_s, reason_s = can_delete_stage(stages, last_stage["template_prefix"])
                if can_del_s:
                    st.warning(f"Removes '{last_stage['name']}' — not recoverable.")
                    confirm_s = st.checkbox(f"I understand, delete {last_stage['name']}",
                                             key="ws_confirm_delete_stage")
                    if st.button(f"Delete {last_stage['name']}", disabled=not confirm_s,
                                 key="ws_delete_stage_btn"):
                        try:
                            paths = build_stage_deletion_paths(cname, last_stage["template_prefix"],
                                                                existing_variants)
                            client = _get_github_client()
                            for path in paths:
                                client.delete_file(path, message=f"Delete stage {last_stage['name']} from "
                                                                   f"{cname} (via Workspace, by {current_user()})")
                            st.success(f"'{last_stage['name']}' deleted.")
                        except GitHubActionsError as exc:
                            st.error(f"Failed: {exc}")
                else:
                    st.info(reason_s)

# =============================================================================
# TAB 4 — Schedule
# =============================================================================
with tabs[3]:
    if not campaign_cfg:
        st.caption("No outreach campaign selected/loaded.")
    else:
        cname = campaign_cfg["_campaign_name"]
        current = get_current_schedule(campaign_cfg)
        st.caption(
            "Leave this alone and your campaign sends anytime. Setting a schedule restricts Send "
            "Batch to only run within the window and days you choose (Preview works anytime either way)."
        )
        display_names = [d for d, _ in COMMON_TIMEZONES]
        iana_names = [i for _, i in COMMON_TIMEZONES]
        current_display = timezone_display_name(current["timezone"]) or display_names[0]
        selected_display = st.selectbox("Time zone", display_names, index=display_names.index(current_display),
                                         key="ws_schedule_tz")
        selected_timezone = iana_names[display_names.index(selected_display)]
        col1, col2 = st.columns(2)
        with col1:
            window_start = st.text_input("Start time (24-hour, HH:MM)", value=current["window_start"],
                                          key="ws_schedule_start")
        with col2:
            window_end = st.text_input("End time (24-hour, HH:MM)", value=current["window_end"],
                                        key="ws_schedule_end")
        st.write("Days")
        day_cols = st.columns(7)
        send_days = []
        for col, (label, code) in zip(day_cols, DAY_OPTIONS):
            with col:
                if st.checkbox(label[:3], value=code in current["send_days"], key=f"ws_schedule_day_{code}"):
                    send_days.append(code)
        if st.button("💾 Save Schedule", type="primary", key="ws_save_schedule"):
            errors = validate_schedule(selected_timezone, window_start, window_end, send_days)
            if errors:
                for e in errors:
                    st.error(e)
            else:
                try:
                    raw_override = load_raw_override(cname, config.CAMPAIGNS_DIR)
                    updated = build_updated_schedule_override(raw_override, selected_timezone, window_start,
                                                               window_end, send_days)
                    client = _get_github_client()
                    client.create_file(override_file_path(cname), override_to_yaml_bytes(updated),
                                        message=f"Update schedule for {cname} (via Workspace, by {current_user()})")
                    st.success("Schedule saved.")
                except GitHubActionsError as exc:
                    st.error(f"Save failed: {exc}")

# =============================================================================
# TAB 5 — Settings (sender accounts, limits, Sync Shortlist, Campaign
# Settings/Asana, Backfill, Send, Danger Zone)
# =============================================================================
with tabs[4]:
    if not campaign_cfg:
        st.caption("No outreach campaign selected/loaded.")
    else:
        cname = campaign_cfg["_campaign_name"]
        sending = campaign_cfg.get("sending", {})
        status, problems = compute_campaign_status(campaign_cfg, leads_for_campaign)

        col1, col2 = st.columns([3, 1])
        with col1:
            st.subheader(status_label(status))
            if problems:
                st.caption("⚠️ " + " · ".join(problems))
        with col2:
            if status == STATUS_DRAFT:
                if st.button("🚀 Launch", key="ws_launch"):
                    st.session_state["ws_launch_confirm"] = True
            elif status in (STATUS_RUNNING, STATUS_ATTENTION):
                if st.button("⏸ Pause", key="ws_pause"):
                    try:
                        raw = load_raw_override(cname, config.CAMPAIGNS_DIR)
                        updated = build_status_override(raw, "paused")
                        _get_github_client().create_file(
                            override_file_path(cname), override_to_yaml_bytes(updated),
                            message=f"Set status=paused for {cname} (via Workspace, by {current_user()})")
                        st.success("Paused.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed: {exc}")
            elif status == STATUS_PAUSED:
                if st.button("▶ Resume", key="ws_resume"):
                    try:
                        raw = load_raw_override(cname, config.CAMPAIGNS_DIR)
                        updated = build_status_override(raw, "active")
                        _get_github_client().create_file(
                            override_file_path(cname), override_to_yaml_bytes(updated),
                            message=f"Set status=active for {cname} (via Workspace, by {current_user()})")
                        st.success("Resumed.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed: {exc}")

        if status == STATUS_DRAFT and st.session_state.get("ws_launch_confirm"):
            ready, launch_problems = compute_campaign_readiness(campaign_cfg, leads_for_campaign)
            st.warning("**Launch this campaign?** This makes it active and allows Send Batch to run.")
            if not ready:
                st.caption("Heads up, not a blocker: " + ", ".join(launch_problems))
            cc1, cc2 = st.columns(2)
            with cc1:
                if st.button("Confirm Launch", type="primary", key="ws_confirm_launch"):
                    try:
                        raw = load_raw_override(cname, config.CAMPAIGNS_DIR)
                        updated = build_status_override(raw, "active")
                        _get_github_client().create_file(
                            override_file_path(cname), override_to_yaml_bytes(updated),
                            message=f"Set status=active for {cname} (via Workspace, by {current_user()})")
                        st.session_state["ws_launch_confirm"] = False
                        st.success("Launched.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed: {exc}")
            with cc2:
                if st.button("Cancel", key="ws_cancel_launch"):
                    st.session_state["ws_launch_confirm"] = False
                    st.rerun()

        st.divider()
        st.subheader("Sender accounts")
        streamlit_secret_directory = dict(st.secrets.get("email_accounts_directory", {}))
        slot_mapping = read_local_slot_mapping(config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH)
        account_directory = merge_account_directories(streamlit_secret_directory, slot_mapping)
        available_accounts = list(account_directory.keys())

        if not available_accounts:
            st.info("No accounts configured yet — add one on the Email Accounts page.")
            rotation_accounts = list(sending.get("rotation_accounts") or [])
        else:
            ms_key = f"ws_rotation_accounts_{cname}"
            if ms_key not in st.session_state:
                st.session_state[ms_key] = [a for a in (sending.get("rotation_accounts") or [])
                                             if a in available_accounts]
            if st.button("Select all accounts", key="ws_select_all_accounts"):
                st.session_state[ms_key] = available_accounts
                st.rerun()
            rotation_accounts = st.multiselect("🔍 Search sender accounts", available_accounts, key=ms_key)

        sender_rotation = st.checkbox("Rotate across multiple sender accounts",
                                       value=bool(sending.get("sender_rotation")), key="ws_sender_rotation")

        st.divider()
        st.subheader("Sending limits")
        daily_limit = st.number_input("Daily limit (across all accounts)", min_value=1,
                                       value=int(sending.get("daily_limit", 100)), key="ws_daily_limit")
        has_per_account = st.checkbox("Set a per-account daily limit",
                                       value=sending.get("per_account_daily_limit") is not None,
                                       key="ws_has_per_account_limit")
        per_account_limit = None
        if has_per_account:
            per_account_limit = st.number_input("Per-account daily limit", min_value=1,
                                                  value=int(sending.get("per_account_daily_limit") or 20),
                                                  key="ws_per_account_limit")

        if st.button("💾 Save Settings", type="primary", key="ws_save_settings"):
            errors = validate_settings(daily_limit, per_account_limit)
            if errors:
                for e in errors:
                    st.error(e)
            else:
                try:
                    raw_override = load_raw_override(cname, config.CAMPAIGNS_DIR)
                    updated = build_updated_override(raw_override, daily_limit, per_account_limit,
                                                      sender_rotation, rotation_accounts)
                    client = _get_github_client()
                    client.create_file(override_file_path(cname), override_to_yaml_bytes(updated),
                                        message=f"Update settings for {cname} (via Workspace, by {current_user()})")
                    st.success("Settings saved.")
                except GitHubActionsError as exc:
                    st.error(f"Save failed: {exc}")

        st.divider()
        st.subheader("Sync Shortlist")
        st.caption(
            "A saved review decision updates Master immediately, but doesn't reach the Shortlist "
            "tab until this runs. Safe to run any time; only ever adds newly-approved rows."
        )
        if HAS_DISCOVERY and st.button("Run Sync Shortlist Now", key="ws_sync_shortlist"):
            try:
                _get_github_client().dispatch_workflow(config.WORKFLOW_SYNC_SHORTLIST, {})
                st.success("Dispatched.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Couldn't dispatch: {exc}")

        st.divider()
        st.subheader("⚙️ Campaign Settings (Asana Sync)")
        if HAS_DISCOVERY and discovery_campaign:
            all_settings = crl.load_current_settings()
            current_asana = crl.get_asana_sync_status(all_settings, discovery_campaign)
            st.write(f"**Asana sync for '{discovery_campaign}'**: currently "
                     + ("✅ ON" if current_asana else "❌ OFF"))
            st.caption("Controls BOTH Email and DM creators from this campaign.")
            new_asana = st.toggle("Enable Asana sync for this campaign", value=current_asana,
                                   key=f"ws_asana_toggle_{discovery_campaign}")
            if new_asana != current_asana and st.button("Save Campaign Settings", type="primary",
                                                          key="ws_save_asana"):
                try:
                    commit = crl.build_settings_commit(all_settings, discovery_campaign, new_asana)
                    _get_github_client().commit_campaign_files_directly(
                        files=[{"path": commit["path"], "content": commit["content"]}],
                        commit_message=commit["commit_message"])
                    st.success("Saved.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Couldn't save: {exc}")
        else:
            st.caption("Select a Discovery Campaign above to manage its Asana setting.")

        st.divider()
        with st.expander("🔧 Maintenance: Backfill ThreadSubject"):
            st.caption("For leads already mid-sequence before ThreadSubject existed. Safe to re-run.")
            dry_run_bf = st.checkbox("Dry run", value=True, key="ws_backfill_dry_run")
            if st.button("Run Backfill", key="ws_run_backfill"):
                try:
                    client = _get_github_client()
                    inputs = build_backfill_thread_subject_inputs(cname, dry_run=dry_run_bf)
                    client.dispatch_workflow(config.WORKFLOW_BACKFILL_THREAD_SUBJECT, inputs)
                    st.success("Triggered.")
                except GitHubActionsError as exc:
                    st.error(f"Failed: {exc}")

        st.divider()
        st.subheader("Send")
        if status != STATUS_RUNNING:
            reason = {
                STATUS_DRAFT: "Launch it above to enable sending.",
                STATUS_PAUSED: "Resume it above to enable sending.",
                STATUS_COMPLETED: "Every approved lead has already finished.",
                STATUS_ATTENTION: "Fix the issue shown above.",
            }.get(status, "")
            st.info(f"Sending is only available while Running — this one is {status_label(status)}. {reason}")
        else:
            stage_s = st.selectbox("Stage", ["intro", "followup1", "followup2", "followup3", "followup4"],
                                    key="ws_send_stage")
            variant_s = st.selectbox("Variant", ["Auto", "A", "B", "C", "D"], key="ws_send_variant")
            ignore_wait = st.checkbox("Ignore the scheduled wait for this stage", key="ws_send_ignore_wait")
            st.warning("This will send real emails. Run Preview first if you haven't already.")
            confirm_text = st.text_input('Type "SEND" to confirm', key="ws_send_confirm_text")
            if st.button("Send Batch", type="primary", key="ws_send_batch_button"):
                if not confirmation_is_valid(confirm_text):
                    st.error('You must type "SEND" (exact match) to confirm.')
                else:
                    batch_size = int(sending.get("daily_limit", 100))
                    inputs = build_send_inputs(campaign=cname, stage=stage_s, batch_size=batch_size,
                                                variant=variant_s, ignore_wait_days=ignore_wait)
                    try:
                        _get_github_client().dispatch_workflow(config.WORKFLOW_SEND, inputs)
                        st.success(f"Triggered Send for '{cname}' / stage '{stage_s}'.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to trigger Send: {exc}")

        st.divider()
        with st.expander("🗑️ Danger Zone"):
            st.subheader("Temporarily Remove")
            if st.button("Temporarily Remove Campaign", key="ws_temp_remove"):
                try:
                    raw = load_raw_override(cname, config.CAMPAIGNS_DIR)
                    updated = build_delete_override(raw, campaign_cfg.get("status") or "active")
                    _get_github_client().create_file(
                        override_file_path(cname), override_to_yaml_bytes(updated),
                        message=f"Temporarily remove {cname} (via Workspace, by {current_user()})")
                    st.success(f"'{cname}' temporarily removed. Restore it from the Campaigns page.")
                except GitHubActionsError as exc:
                    st.error(f"Failed: {exc}")

            st.divider()
            st.subheader("Permanently Delete")
            st.warning("Removes templates permanently — not recoverable from this app. Sheet data untouched.")
            typed_name = st.text_input(f'Type "{cname}" to confirm', key="ws_delete_confirm_text")
            confirmed = confirmation_matches_campaign_name(typed_name, cname)
            if st.button("Permanently Delete Campaign", type="primary", disabled=not confirmed,
                         key="ws_delete_campaign_btn"):
                try:
                    paths = list_campaign_files_to_delete(cname, config.TEMPLATES_ROOT, config.CAMPAIGNS_DIR)
                    client = _get_github_client()
                    for path in paths:
                        client.delete_file(path, message=f"Delete campaign {cname} "
                                                          f"(via Workspace, by {current_user()})")
                    st.success(f"'{cname}' deleted ({len(paths)} file(s)).")
                except GitHubActionsError as exc:
                    st.error(f"Failed: {exc}")

# =============================================================================
# TAB 6 — Responses (simplified: read-only + check-now; full reply flow
# with attachments stays on the existing Responses page — see module note)
# =============================================================================
with tabs[5]:
    if not campaign_cfg:
        st.caption("No outreach campaign selected/loaded.")
    else:
        cname = campaign_cfg["_campaign_name"]
        st.caption(
            "Checking replies also runs automatically every 30 minutes — use this only if you "
            "want it checked right now."
        )
        if st.button("Check Replies Now", key="ws_check_replies"):
            try:
                _get_github_client().dispatch_workflow(config.WORKFLOW_CHECK_REPLIES, {"campaign": cname})
                st.success("Dispatched.")
            except GitHubActionsError as exc:
                st.error(f"Failed: {exc}")

        if not responses_for_campaign:
            st.info("No responses yet.")
        else:
            sorted_responses = sorted(responses_for_campaign, key=lambda r: r.get("ReceivedAt", ""), reverse=True)
            for response in sorted_responses:
                label = f"{response.get('From', '(unknown)')} — {response.get('Subject', '(no subject)')}"
                action = response.get("ActionTaken", "")
                icon = "🛑" if action == "Stopped Sequence" else "📝"
                with st.container(border=True):
                    st.markdown(f"{icon} **{label}**")
                    st.caption(f"{response.get('ReceivedAt', '')} · {response.get('Classification', '')} · {action}")
                    st.write(response.get("Snippet", ""))
            st.page_link("pages/responses.py", label="Reply to a message (full thread + attachments) →",
                         icon="💬")

# =============================================================================
# TAB 7 — DM Drafting
# =============================================================================
with tabs[6]:
    if not HAS_DISCOVERY or not discovery_campaign:
        st.caption("Select a Brand and Discovery Campaign above.")
    else:
        try:
            shortlist_records_dm = _fetch_discovery_tab("Shortlist")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't read Shortlist: {exc}")
            shortlist_records_dm = []

        dm_campaign_rows = [r for r in shortlist_records_dm if r.get("Campaign") == discovery_campaign]
        dm_rows = crl.filter_creator_rows(dm_campaign_rows, "DM")

        if not dm_rows:
            st.info("No creators routed to DM yet for this campaign.")
        else:
            status_groups = {}
            for r in dm_rows:
                status = r.get("dm_status", "").strip() or "pending_reasoning"
                status_groups.setdefault(status, []).append(r)
            for status in crl.DM_STATUS_OPTIONS + ["pending_reasoning"]:
                rows = status_groups.get(status)
                if not rows:
                    continue
                with st.expander(f"{status} ({len(rows)})",
                                 expanded=(status in ("Not Contacted", "Draft Ready"))):
                    for r in rows:
                        st.write(f"**{r.get('dedup_key', '—')}** · {r.get('platform', '—')}")

            st.divider()
            st.subheader("Update a Creator")
            dm_options = [r["dedup_key"] for r in dm_rows if r.get("dedup_key")]
            selected_dm_key = st.selectbox("Creator", dm_options, key="ws_dm_creator_select")
            selected_dm_row = next((r for r in dm_rows if r["dedup_key"] == selected_dm_key), {})

            draft = selected_dm_row.get("dm_draft", "")
            if draft:
                st.code(draft, language=None)
            else:
                st.caption("No draft yet — run 'Draft DMs' for this campaign first.")

            current_dm_status = selected_dm_row.get("dm_status", "").strip() or "Not Contacted"
            idx = crl.DM_STATUS_OPTIONS.index(current_dm_status) if current_dm_status in crl.DM_STATUS_OPTIONS else 0
            new_dm_status = st.selectbox("Status", crl.DM_STATUS_OPTIONS, index=idx, key="ws_dm_status_select")
            new_dm_notes = st.text_area("Notes (optional)", key="ws_dm_notes_input")

            if st.button("Save", type="primary", key="ws_save_dm_status"):
                try:
                    client = _get_github_client()
                    client.dispatch_workflow(config.WORKFLOW_UPDATE_DM_STATUS, {
                        "creator_key": selected_dm_key,
                        "campaign": discovery_campaign,
                        "dm_status": new_dm_status,
                        "dm_notes": new_dm_notes,
                    })
                    st.success("Dispatched — check 'Update DM Status' in the Actions tab.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Couldn't dispatch: {exc}")
