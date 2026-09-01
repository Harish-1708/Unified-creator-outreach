import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate, current_user  # noqa: E402
from config import WORKFLOW_SEND, WORKFLOW_CHECK_REPLIES, WORKFLOW_BACKFILL_THREAD_SUBJECT  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg, run_preview  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError  # noqa: E402
from send_logic import (  # noqa: E402
    build_send_inputs, build_check_replies_inputs, build_backfill_thread_subject_inputs, confirmation_is_valid,
)
from replies_logic import most_recent_responses  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

mark_active_page("controls")

if not login_gate():
    st.stop()

st.title("🚀 Controls")


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


@st.cache_resource(show_spinner=False)
def _get_sheets_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


try:
    campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list campaigns: {exc}")
    st.stop()

if not campaigns:
    st.info("No campaigns found yet.")
    st.stop()

STAGES = ["intro", "followup1", "followup2", "followup3", "followup4"]
VARIANTS = ["Auto", "A", "B", "C", "D"]

campaign = st.selectbox("Campaign", campaigns, key="controls_campaign")

preview_tab, send_tab, replies_tab, maintenance_tab = st.tabs(
    ["👀 Preview", "📤 Send", "📥 Check Replies", "🔧 Maintenance"])

# ---------------------------------------------------------------------------
# Preview — runs directly in Streamlit. No GitHub Actions round trip, no
# writes, no SMTP credentials anywhere near this process.
# ---------------------------------------------------------------------------
with preview_tab:
    st.caption("Runs instantly, in-app. Nothing is sent or written — identical logic to `outreach.py preview`.")
    stage = st.selectbox("Stage", STAGES, key="preview_stage")
    batch_size = st.number_input("Batch size", min_value=1, max_value=500, value=10, key="preview_batch_size")
    variant = st.selectbox("Variant", VARIANTS, key="preview_variant")
    ignore_wait_days_preview = st.checkbox(
        "Ignore the scheduled wait for this stage (e.g. send followup1 today even if its 3-day wait "
        "hasn't elapsed)", key="preview_ignore_wait_days",
        help="Every other rule still applies — the previous stage must actually have been sent, this "
             "stage can't already be sent, Approval must be Yes, and no reply must have been received.",
    )

    if st.button("Run Preview"):
        try:
            connector = _get_sheets_connector()
            campaign_cfg = get_campaign_cfg(campaign)
            leads = connector.get_all_leads(campaign_cfg["master_tab"])
            plan = run_preview(campaign, stage, int(batch_size), leads, forced_variant=variant,
                                ignore_wait_days=ignore_wait_days_preview)
        except ReadOnlySheetsError as exc:
            st.warning(str(exc))
            plan = None
        except Exception as exc:  # noqa: BLE001
            st.error(f"Preview failed: {exc}")
            plan = None

        if plan is not None:
            if not plan:
                st.info(f"No eligible leads for stage '{stage}'.")
            else:
                st.success(f"{len(plan)} eligible lead(s).")
                for item in plan:
                    lead = item["lead"]
                    with st.expander(f"{lead.get('FirstName', '')} {lead.get('LastName', '')} <{lead.get('Email')}> — variant {item['variant']}"):
                        st.write(f"**Subject:** {item['subject']}"
                                 + (" _(continuing existing thread)_" if item["is_continuation"] else ""))
                        st.text(item["body"])
                        if item["missing_variables"]:
                            st.warning(f"Unrecognized template variable(s): {', '.join(item['missing_variables'])}")

# ---------------------------------------------------------------------------
# Send — always via GitHub Actions. This is the ONLY path that ever touches
# SMTP credentials, and they never leave GitHub Secrets.
# ---------------------------------------------------------------------------
with send_tab:
    st.caption("Triggers the real `send_batch.yml` workflow — same safety checks, same typed confirmation.")
    stage_s = st.selectbox("Stage", STAGES, key="send_stage")
    batch_size_s = st.number_input("Batch size", min_value=1, max_value=500, value=10, key="send_batch_size")
    variant_s = st.selectbox("Variant", VARIANTS, key="send_variant")

    with st.expander("Advanced overrides (optional, this run only)"):
        override_daily_limit = st.number_input("Override daily_limit", min_value=0, value=0, key="send_daily_limit",
                                                  help="0 = use config default")
        override_per_account = st.number_input("Override per_account_daily_limit", min_value=0, value=0,
                                                  key="send_per_account", help="0 = use config default")
        rotation_choice = st.selectbox("sender_rotation override", ["(use config default)", "true", "false"],
                                         key="send_rotation")
        ignore_wait_days_send = st.checkbox(
            "Ignore the scheduled wait for this stage (send now regardless of schedule)",
            key="send_ignore_wait_days",
            help="Every other rule still applies — the previous stage must actually have been sent, this "
                 "stage can't already be sent, Approval must be Yes, and no reply must have been received.",
        )

    st.warning(
        "This will send real emails. Run Preview for this exact batch first if you haven't already."
    )
    if ignore_wait_days_send:
        st.warning(
            "⏱️ 'Ignore scheduled wait' is checked — leads not normally due for this stage yet will be "
            "included, as long as every other rule still passes."
        )
    confirm_text = st.text_input(
        'Type "SEND" to confirm (this is the same deliberate friction as the GitHub Actions workflow — '
        "typing it here does not skip anything, it's still required)",
        key="send_confirm_text",
    )

    if st.button("Send Batch", type="primary"):
        if not confirmation_is_valid(confirm_text):
            st.error('You must type "SEND" (exact match) to confirm.')
        else:
            inputs = build_send_inputs(
                campaign=campaign, stage=stage_s, batch_size=int(batch_size_s), variant=variant_s,
                daily_limit=int(override_daily_limit) or None,
                per_account_daily_limit=int(override_per_account) or None,
                sender_rotation=(None if rotation_choice == "(use config default)" else rotation_choice == "true"),
                ignore_wait_days=ignore_wait_days_send,
            )
            try:
                client = _get_github_client()
                run_details = client.dispatch_workflow(WORKFLOW_SEND, inputs)
                if run_details is None:
                    time.sleep(2)  # give GitHub a moment to register the run before we look for it
                    run_details = client.find_recent_run(WORKFLOW_SEND)
                if run_details:
                    st.session_state["last_send_run_id"] = run_details.get("id") or run_details.get("run_id")
                    st.session_state["last_send_run_url"] = run_details.get("html_url", "")
                st.success(f"Triggered Send for '{campaign}' / stage '{stage_s}', by {current_user()}.")
            except GitHubActionsError as exc:
                st.error(f"Failed to trigger Send: {exc}")

    run_id = st.session_state.get("last_send_run_id")
    if run_id:
        st.divider()
        st.write(f"**Last triggered run:** [{run_id}]({st.session_state.get('last_send_run_url', '')})")
        if st.button("🔄 Refresh run status", key="send_refresh_status"):
            try:
                run = _get_github_client().get_run(run_id)
                status = run.get("status", "unknown")
                conclusion = run.get("conclusion")
                if status == "completed":
                    st.success(f"Completed — conclusion: {conclusion}")
                else:
                    st.info(f"Status: {status}")
            except GitHubActionsError as exc:
                st.error(f"Failed to fetch run status: {exc}")

# ---------------------------------------------------------------------------
# Check Replies — also runs automatically every 30 min; this button is a
# manual trigger for convenience. The Response Sheet is already updated the
# moment the run finishes, so this tab shows those results directly rather
# than just linking out to the Actions log.
# ---------------------------------------------------------------------------
with replies_tab:
    st.caption("This also runs automatically every 30 minutes — use this only if you want it to check right now.")

    if st.button("Check Replies Now"):
        try:
            client = _get_github_client()
            inputs = build_check_replies_inputs(campaign)
            run_details = client.dispatch_workflow(WORKFLOW_CHECK_REPLIES, inputs)
            if run_details is None:
                time.sleep(2)
                run_details = client.find_recent_run(WORKFLOW_CHECK_REPLIES)
            if run_details:
                st.session_state["last_replies_run_id"] = run_details.get("id") or run_details.get("run_id")
                st.session_state["last_replies_run_url"] = run_details.get("html_url", "")
            st.success(f"Triggered Check Replies for '{campaign}'.")
        except GitHubActionsError as exc:
            st.error(f"Failed to trigger Check Replies: {exc}")

    replies_run_id = st.session_state.get("last_replies_run_id")
    if replies_run_id:
        st.write(f"**Last triggered run:** [{replies_run_id}]({st.session_state.get('last_replies_run_url', '')})")
        if st.button("🔄 Refresh run status", key="replies_refresh_status"):
            try:
                run = _get_github_client().get_run(replies_run_id)
                status = run.get("status", "unknown")
                conclusion = run.get("conclusion")
                if status == "completed":
                    st.success(f"Completed — conclusion: {conclusion}. Reply results are below — click "
                               "'Refresh replies' to pull them in.")
                else:
                    st.info(f"Status: {status} (not finished yet — the Response Sheet won't have new rows "
                            "from this run until it completes)")
            except GitHubActionsError as exc:
                st.error(f"Failed to fetch run status: {exc}")

    st.divider()
    st.subheader("Recent replies")
    st.caption(
        "Read directly from this campaign's Response Sheet tab — reflects whatever the most recent "
        "Check Replies run (scheduled or manual) has already written, whether or not it was triggered here."
    )

    if st.button("🔄 Refresh replies"):
        st.session_state["_replies_refresh_nonce"] = st.session_state.get("_replies_refresh_nonce", 0) + 1

    try:
        connector = _get_sheets_connector()
        campaign_cfg = get_campaign_cfg(campaign)
        responses = connector.get_all_responses(campaign_cfg["responses_tab"])
    except ReadOnlySheetsError as exc:
        st.warning(str(exc))
        responses = None
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to read responses: {exc}")
        responses = None

    if responses is not None:
        recent = most_recent_responses(responses, limit=20)
        if not recent:
            st.info("No replies logged for this campaign yet.")
        else:
            for r in recent:
                action = r.get("ActionTaken", "")
                classification = r.get("Classification", "")
                # ActionTaken is what actually happened — a "Logged Only
                # (...)" outcome means the sequence was NOT stopped, even
                # if Classification says "Genuine Reply" (that field only
                # describes the message content, not what the system did
                # about it). Surfacing both, clearly labeled, is the point
                # of this view — the raw sheet columns read confusingly
                # side by side otherwise.
                stopped = action == "Stopped Sequence"
                icon = "🛑" if stopped else "📝"
                label = f"{icon} {r.get('From', '')} — {r.get('Subject', '')} ({r.get('ReceivedAt', '')})"
                with st.expander(label):
                    st.write(f"**Classification:** {classification}")
                    st.write(f"**Action taken:** {action}"
                             + ("" if stopped else " — sequence was NOT stopped for this lead"))
                    st.write(f"**Match method:** {r.get('MatchMethod', '')}")
                    st.text(r.get("Snippet", ""))

# ---------------------------------------------------------------------------
# Maintenance — one-time/occasional migration tools. Currently just the
# ThreadSubject backfill, but this is where any future "fix up old data"
# tool belongs, kept separate from the day-to-day Preview/Send/Check
# Replies tabs.
# ---------------------------------------------------------------------------
with maintenance_tab:
    st.subheader("Backfill ThreadSubject")
    st.caption(
        "For leads already mid-sequence before ThreadSubject existed — needed so a later stage's blank "
        "Subject (\"continue the thread\") has something to continue from. Never overwrites an "
        "already-set ThreadSubject, so this is safe to run more than once — it only fills gaps. "
        "New leads never need this: it's written automatically at every real send going forward."
    )
    st.caption(
        "Reconstructs each lead's ThreadSubject from the CURRENT template content of the most recently "
        "sent stage that actually had a real subject — automatically skipping past any more recent stage "
        "that was itself sent using the blank-Subject convention. Accurate as long as that stage's "
        "template hasn't been edited since it was actually sent."
    )
    st.warning(
        "Prefer this over pasting a subject in manually — it's easy to accidentally copy the wrong "
        "lead's or campaign's subject when they're worded similarly, and a wrong value won't show an "
        "error, it'll just silently mis-thread that lead's next reply."
    )

    dry_run = st.checkbox("Dry run (preview only, don't write anything)", value=True, key="backfill_dry_run")

    if st.button("Run Backfill"):
        try:
            client = _get_github_client()
            inputs = build_backfill_thread_subject_inputs(campaign, dry_run=dry_run)
            run_details = client.dispatch_workflow(WORKFLOW_BACKFILL_THREAD_SUBJECT, inputs)
            if run_details is None:
                time.sleep(2)
                run_details = client.find_recent_run(WORKFLOW_BACKFILL_THREAD_SUBJECT)
            if run_details:
                st.session_state["last_backfill_run_id"] = run_details.get("id") or run_details.get("run_id")
                st.session_state["last_backfill_run_url"] = run_details.get("html_url", "")
            st.success(f"Triggered backfill for '{campaign}'" + (" (dry run)" if dry_run else "") + ".")
        except GitHubActionsError as exc:
            st.error(f"Failed to trigger backfill: {exc}")

    backfill_run_id = st.session_state.get("last_backfill_run_id")
    if backfill_run_id:
        st.write(f"**Last triggered run:** [{backfill_run_id}]({st.session_state.get('last_backfill_run_url', '')})")
        if st.button("🔄 Refresh run status", key="backfill_refresh_status"):
            try:
                run = _get_github_client().get_run(backfill_run_id)
                status = run.get("status", "unknown")
                conclusion = run.get("conclusion")
                if status == "completed":
                    st.success(f"Completed — conclusion: {conclusion}. Check the run's job summary on "
                               "GitHub for exactly which leads were backfilled or skipped.")
                else:
                    st.info(f"Status: {status}")
            except GitHubActionsError as exc:
                st.error(f"Failed to fetch run status: {exc}")
