import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate, current_user  # noqa: E402
import config  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from email_account_slots_logic import read_local_slot_mapping  # noqa: E402
from accounts_logic import merge_account_directories  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from settings_logic import (  # noqa: E402
    load_raw_override, validate_settings, build_updated_override,
    override_to_yaml_bytes, override_file_path,
)
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
import creator_research_logic as crl  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("settings")

if not login_gate():
    st.stop()

st.title("⚙️ Settings")
st.caption(
    "Same underlying logic as the Campaigns page's own Settings tab (settings_logic.py) — this "
    "is a separate top-level page calling the same tested functions, not a rebuild of them. "
    "campaigns.py itself is untouched."
)


def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


try:
    outreach_campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list outreach campaigns: {exc}")
    st.stop()

if not outreach_campaigns:
    st.info("No outreach campaigns exist yet — create one from the Campaigns page first.")
    st.stop()

outreach_campaign = st.selectbox("Outreach campaign", outreach_campaigns, key="settings_outreach_campaign")

try:
    campaign_cfg = get_campaign_cfg(outreach_campaign)
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't load '{outreach_campaign}': {exc}")
    st.stop()

sending = campaign_cfg.get("sending", {})

# =============================================================================
# Sender accounts / sending limits — identical logic and flow to
# campaigns.py's own Settings tab (_render_settings_tab), reused directly.
# =============================================================================
st.subheader("Sender accounts")

streamlit_secret_directory = dict(st.secrets.get("email_accounts_directory", {}))
slot_mapping = read_local_slot_mapping(config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH)
account_directory = merge_account_directories(streamlit_secret_directory, slot_mapping)
available_accounts = list(account_directory.keys())

if not available_accounts:
    st.info(
        "No accounts configured yet — add one on the Email Accounts page, or add "
        "[email_accounts_directory] to Streamlit Secrets for the legacy path."
    )
    rotation_accounts = list(sending.get("rotation_accounts") or [])
else:
    ms_key = f"top_level_settings_rotation_accounts_{outreach_campaign}"
    if ms_key not in st.session_state:
        st.session_state[ms_key] = [a for a in (sending.get("rotation_accounts") or [])
                                     if a in available_accounts]

    if st.button("Select all accounts"):
        st.session_state[ms_key] = available_accounts
        st.rerun()

    rotation_accounts = st.multiselect(
        "🔍 Search sender accounts", available_accounts, key=ms_key,
        help="Leave empty to use every configured account for rotation.",
    )

sender_rotation = st.checkbox("Rotate across multiple sender accounts",
                               value=bool(sending.get("sender_rotation")))

st.divider()
st.subheader("Sending limits")
daily_limit = st.number_input("Daily limit (across all accounts)", min_value=1,
                               value=int(sending.get("daily_limit", 100)))
has_per_account_limit = st.checkbox("Set a per-account daily limit",
                                     value=sending.get("per_account_daily_limit") is not None)
per_account_daily_limit = None
if has_per_account_limit:
    per_account_daily_limit = st.number_input(
        "Per-account daily limit", min_value=1,
        value=int(sending.get("per_account_daily_limit") or 20),
    )

st.divider()
if st.button("💾 Save Settings", type="primary"):
    errors = validate_settings(daily_limit, per_account_daily_limit)
    if errors:
        for e in errors:
            st.error(e)
    else:
        try:
            raw_override = load_raw_override(outreach_campaign, config.CAMPAIGNS_DIR)
            updated = build_updated_override(raw_override, daily_limit, per_account_daily_limit,
                                              sender_rotation, rotation_accounts)
            client = _get_github_client()
            client.create_file(
                override_file_path(outreach_campaign), override_to_yaml_bytes(updated),
                message=f"Update settings for {outreach_campaign} (via Settings page, by {current_user()})",
            )
            st.success("Settings saved. May take a minute to actually reflect here while the app "
                       "redeploys — it's in effect for sending immediately either way.")
        except GitHubActionsError as exc:
            st.error(f"Save failed: {exc}")

st.divider()

# =============================================================================
# Sync Shortlist — dispatches the existing discovery-side workflow.
# Deliberately keyed by the DISCOVERY Campaign, not the outreach campaign
# selected above — they are genuinely different concepts (see the bridge's
# own docstring: one discovery Campaign can route creators into several
# different outreach campaigns), so this section gets its own selector.
# =============================================================================
st.subheader("Sync Shortlist")
st.caption(
    "A saved review decision updates Master immediately, but doesn't reach the Shortlist tab — "
    "what DM drafting and the outreach bridge actually read from — until this runs. Safe to run "
    "any time; it only ever adds newly-approved rows, never removes anything."
)

if not st.secrets.get("discovery_spreadsheet_id"):
    st.warning("`discovery_spreadsheet_id` isn't set in Secrets — Campaign Settings and Sync "
               "Shortlist below need it. See the Creator Research page's setup note.")
else:
    if st.button("Run Sync Shortlist Now", key="settings_sync_shortlist"):
        try:
            client = _get_github_client()
            client.dispatch_workflow(config.WORKFLOW_SYNC_SHORTLIST, {})
            st.success("Dispatched — check the 'Sync Shortlist' workflow run in the Actions tab.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't dispatch Sync Shortlist: {exc}")

st.divider()

# =============================================================================
# Campaign Settings (Asana sync) — moved here from the Creator Research
# page, matching where this was actually asked to live. Keyed by discovery
# Campaign (see campaign_settings.py's own docstring for why).
# =============================================================================
st.subheader("⚙️ Campaign Settings (Asana Sync)")

if st.secrets.get("discovery_spreadsheet_id"):
    @st.cache_resource(show_spinner=False)
    def _get_discovery_connector() -> ReadOnlySheetsConnector:
        sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
        sheet_id = st.secrets.get("discovery_spreadsheet_id", "")
        return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)

    try:
        discovery_connector = _get_discovery_connector()
        run_log_records = discovery_connector.get_all_records_from_tab("Run Log")
        discovery_campaigns = sorted({r["campaign"] for r in run_log_records if r.get("campaign")})
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't read the discovery sheet: {exc}")
        discovery_campaigns = []

    if not discovery_campaigns:
        st.caption("No discovery campaigns found yet.")
    else:
        discovery_campaign = st.selectbox("Discovery campaign", discovery_campaigns,
                                           key="settings_discovery_campaign")

        all_settings = crl.load_current_settings()
        current_status = crl.get_asana_sync_status(all_settings, discovery_campaign)

        st.write(f"**Asana sync for '{discovery_campaign}'**: currently "
                 + ("✅ ON" if current_status else "❌ OFF"))
        st.caption(
            "Controls BOTH Email and DM creators from this campaign — there's no separate "
            "setting per channel. A campaign that's never been configured defaults to OFF."
        )

        new_status = st.toggle("Enable Asana sync for this campaign", value=current_status,
                                key=f"settings_asana_toggle_{discovery_campaign}")

        if new_status != current_status:
            if st.button("Save Campaign Settings", type="primary", key="settings_save_asana"):
                try:
                    commit = crl.build_settings_commit(all_settings, discovery_campaign, new_status)
                    client = _get_github_client()
                    client.commit_campaign_files_directly(
                        files=[{"path": commit["path"], "content": commit["content"]}],
                        commit_message=commit["commit_message"],
                    )
                    st.success("Saved. Takes effect here once the app finishes redeploying.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Couldn't save settings: {exc}")
else:
    st.caption("Needs `discovery_spreadsheet_id` in Secrets — see above.")

st.divider()
st.caption(
    "**Not on this page yet:** Backfill ThreadSubject, the Send section, and the Danger Zone "
    "(pause/delete/restore a campaign). Still only available from the Campaigns page for now."
)
