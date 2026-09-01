import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate, current_user  # noqa: E402
from config import REPO_ROOT, WORKFLOW_CHECK_REPLIES, WORKFLOW_SEND_REPLY, WORKFLOW_MARK_RESPONSES_READ  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from send_logic import build_check_replies_inputs  # noqa: E402
from responses_hub_logic import (  # noqa: E402
    tag_responses_with_campaign, response_key, filter_responses, count_unread,
    sort_responses_newest_first, get_campaign_names_present, CLASSIFICATION_OPTIONS,
    STATUS_FILTER_ALL, INBOX_FILTER_ALL, INBOX_FILTER_UNREAD, search_responses,
    is_response_read, split_keys_by_campaign, build_mark_read_payload,
)
from responses_reply_logic import (  # noqa: E402
    find_lead_for_response, build_reply_defaults, parse_email_list, validate_reply,
    build_reply_payload, reply_payload_path, build_attachment_entries, total_attachment_size_bytes,
)
from conversation_logic import build_conversation_thread, filter_responses_for_lead  # noqa: E402
from data_import_logic import payload_to_bytes  # noqa: E402

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import outreach  # noqa: E402

_just_arrived = mark_active_page("responses")

if not login_gate():
    st.stop()

st.title("💬 Responses")
st.caption(
    "Every reply across every campaign, in one place. For working inside a single campaign's own "
    "context, the Campaigns page has the same reply tools under each campaign's Responses tab."
)


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


@st.cache_data(ttl=30, show_spinner=False)
def _load_all_responses(campaign_names):
    connector = _get_connector()
    all_responses = []
    unavailable = []
    for name in campaign_names:
        try:
            campaign_cfg = get_campaign_cfg(name)
            raw = connector.get_all_responses(campaign_cfg["responses_tab"])
            all_responses.extend(tag_responses_with_campaign(raw, name))
        except ReadOnlySheetsError:
            unavailable.append(name)
        except Exception:  # noqa: BLE001 - a campaign missing config shouldn't sink the whole page
            unavailable.append(name)
    return all_responses, unavailable


@st.cache_data(ttl=30, show_spinner=False)
def _load_all_leads(campaign_names):
    """Needed to resolve each response's SenderAccount for the reply
    form's default — same lookup the per-campaign Responses tab does,
    just across every campaign here instead of one."""
    connector = _get_connector()
    leads_by_campaign = {}
    for name in campaign_names:
        try:
            campaign_cfg = get_campaign_cfg(name)
            leads_by_campaign[name] = connector.get_all_leads(campaign_cfg["master_tab"])
        except Exception:  # noqa: BLE001
            leads_by_campaign[name] = []
    return leads_by_campaign


try:
    campaign_names = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list campaigns: {exc}")
    st.stop()

if st.button("🔄 Refresh"):
    _load_all_responses.clear()
    _load_all_leads.clear()

all_responses, unavailable_campaigns = _load_all_responses(tuple(campaign_names))
leads_by_campaign = _load_all_leads(tuple(campaign_names))

if "read_response_keys" not in st.session_state:
    st.session_state["read_response_keys"] = set()
read_keys = st.session_state["read_response_keys"]

if "pending_sync_keys" not in st.session_state:
    st.session_state["pending_sync_keys"] = set()
pending_sync_keys = st.session_state["pending_sync_keys"]

# Read/unread is now persistent — each response's own IsRead column in
# the Response Sheet is the durable source of truth (see
# responses_hub_logic.is_response_read). read_keys above is only an
# OPTIMISTIC local overlay for the gap between "you opened this" and
# "the sync workflow actually wrote IsRead=Yes" — Streamlit itself never
# writes to the Sheet directly, so that write always goes through
# mark_responses_read.yml, batched rather than one run per response.
unread_count = count_unread(all_responses, read_keys)

if pending_sync_keys:
    if st.button(f"🔄 Sync read status ({len(pending_sync_keys)} pending)"):
        grouped = split_keys_by_campaign(pending_sync_keys)
        client = _get_github_client()
        synced_campaigns = []
        failed_campaigns = []
        for campaign_for_sync, response_ids in grouped.items():
            try:
                payload = build_mark_read_payload(response_ids)
                path = f"mark_read/{campaign_for_sync}/{time.strftime('%Y-%m-%d-%H%M%S')}.json"
                client.create_file(
                    path, payload_to_bytes(payload),
                    message=f"Mark {len(response_ids)} response(s) read in {campaign_for_sync} "
                            f"(via Streamlit, by {current_user()})",
                )
                client.dispatch_workflow(WORKFLOW_MARK_RESPONSES_READ,
                                          {"campaign": campaign_for_sync, "payload_path": path})
                synced_campaigns.append(campaign_for_sync)
            except GitHubActionsError:
                failed_campaigns.append(campaign_for_sync)
        keys_to_remove = {f"{c}:{rid}" for c in synced_campaigns for rid in grouped[c]}
        st.session_state["pending_sync_keys"] = pending_sync_keys - keys_to_remove
        if synced_campaigns:
            st.success(f"Synced read status for {len(synced_campaigns)} campaign(s).")
        if failed_campaigns:
            st.warning(f"Failed to sync: {', '.join(failed_campaigns)}")

search_query = st.text_input("🔍 Search responses (sender, subject, snippet, campaign)",
                              key="responses_search_query")

col1, col2, col3 = st.columns(3)
with col1:
    status_filter = st.selectbox("Status", CLASSIFICATION_OPTIONS, key="responses_status_filter")
with col2:
    campaign_options = [STATUS_FILTER_ALL] + get_campaign_names_present(all_responses)
    campaign_filter = st.selectbox("Campaign", campaign_options, key="responses_campaign_filter")
with col3:
    inbox_filter = st.selectbox(
        "More", [INBOX_FILTER_ALL, INBOX_FILTER_UNREAD], key="responses_inbox_filter",
        format_func=lambda v: f"Inbox ({unread_count} unread)" if v == INBOX_FILTER_ALL else v,
    )

if unavailable_campaigns:
    st.caption(f"{len(unavailable_campaigns)} campaign(s) not counted yet (no Response Sheet tab exists — "
               f"no replies for them yet): {', '.join(unavailable_campaigns)}")

filtered = search_responses(all_responses, search_query)
filtered = filter_responses(filtered, status_filter, campaign_filter, inbox_filter, read_keys)
filtered = sort_responses_newest_first(filtered)

if not filtered:
    st.info("No responses match these filters.")
else:
    for response in filtered:
        campaign_name = response["_campaign"]
        key = response_key(response)
        is_unread = not is_response_read(response, read_keys)
        leads = leads_by_campaign.get(campaign_name, [])
        lead = find_lead_for_response(response, leads)
        label = f"{response.get('From', '(unknown sender)')} — {response.get('Subject', '(no subject)')}"

        with st.container(border=True):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                prefix = "🔵 " if is_unread else ""
                st.markdown(f"{prefix}**{label}**")
                intent = response.get("Intent", "")
                intent_confidence = response.get("IntentConfidence", "")
                intent_badge = f" · 🎯 {intent} ({intent_confidence} confidence)" if intent else ""
                st.caption(f"{campaign_name} · {response.get('ReceivedAt', '')} · "
                           f"{response.get('Classification', '')}{intent_badge}")
            with col_b:
                st.caption(response.get("ActionTaken", ""))
                if is_unread:
                    # A deliberate, explicit action — st.expander's body
                    # runs on every rerun REGARDLESS of whether it's
                    # actually open, so marking read merely because the
                    # Reply expander's code executed would mark
                    # EVERYTHING read on the very first page load.
                    if st.button("✓ Mark as read", key=f"mark_read_{key}"):
                        read_keys.add(key)
                        pending_sync_keys.add(key)
                        st.rerun()
            st.write(response.get("Snippet", ""))

            with st.expander("💬 View full conversation"):
                if lead is None:
                    st.caption("Can't reconstruct this conversation — no matching lead found in the "
                               "Master Sheet (the response may predate the lead being added).")
                else:
                    responses_for_this_lead = filter_responses_for_lead(all_responses, lead.get("LeadID", ""))
                    campaign_cfg_for_thread = get_campaign_cfg(campaign_name)
                    thread = build_conversation_thread(campaign_cfg_for_thread, lead, responses_for_this_lead)
                    if not thread:
                        st.caption("No messages found for this lead yet.")
                    for msg in thread:
                        if msg["direction"] == "outgoing":
                            st.markdown(f"**You** · {msg['timestamp']}")
                        else:
                            st.markdown(f"**{msg.get('from', 'Them')}** · {msg['timestamp']}")
                        st.text(msg["body"])
                        st.divider()

            with st.expander("↩️ Reply"):
                defaults = build_reply_defaults(response, lead)
                to_email = st.text_input("To", value=defaults["to"], key=f"hub_reply_to_{key}")
                subject = st.text_input("Subject", value=defaults["subject"], key=f"hub_reply_subject_{key}")
                cc_raw = st.text_input("Cc (comma-separated)", key=f"hub_reply_cc_{key}")
                bcc_raw = st.text_input("Bcc (comma-separated)", key=f"hub_reply_bcc_{key}")
                body = st.text_area("Message", key=f"hub_reply_body_{key}", height=150)
                uploaded_files = st.file_uploader(
                    "Attach images or files (optional)", accept_multiple_files=True,
                    key=f"hub_reply_attachments_{key}",
                )
                if uploaded_files:
                    total_bytes = total_attachment_size_bytes(uploaded_files)
                    st.caption(f"{len(uploaded_files)} file(s), {total_bytes / (1024*1024):.1f} MB total "
                               f"(limit: {outreach.MAX_TOTAL_ATTACHMENT_BYTES / (1024*1024):.0f} MB)")
                if defaults["sender_account"]:
                    st.caption(f"Sending as: {defaults['sender_account']}")
                else:
                    st.caption("⚠️ Couldn't find this lead's sender account — check the Master Sheet.")

                if st.button("Send Reply", type="primary", key=f"hub_send_reply_{key}"):
                    cc = parse_email_list(cc_raw)
                    bcc = parse_email_list(bcc_raw)
                    attachment_bytes = total_attachment_size_bytes(uploaded_files) if uploaded_files else 0
                    errors = validate_reply(to_email, body, defaults["sender_account"], cc, bcc,
                                             attachment_total_bytes=attachment_bytes)
                    if errors:
                        for e in errors:
                            st.error(e)
                    else:
                        try:
                            attachments = build_attachment_entries(uploaded_files) if uploaded_files else None
                            payload = build_reply_payload(response, lead, defaults["sender_account"], to_email,
                                                           subject, body, cc, bcc, attachments=attachments)
                            path = reply_payload_path(campaign_name, response.get("ResponseID", "unknown"))
                            client = _get_github_client()
                            client.create_file(
                                path, payload_to_bytes(payload),
                                message=f"Send reply for {campaign_name} (via Streamlit, by {current_user()})",
                            )
                            client.dispatch_workflow(WORKFLOW_SEND_REPLY,
                                                      {"campaign": campaign_name, "payload_path": path})
                            read_keys.add(key)
                            pending_sync_keys.add(key)
                            st.success("Reply queued — it'll be sent within a minute or two.")
                        except GitHubActionsError as exc:
                            st.error(f"Failed to send: {exc}")

st.divider()
if st.button("📥 Check Replies Now (all campaigns)"):
    client = _get_github_client()
    triggered = []
    failed = []
    for name in campaign_names:
        try:
            inputs = build_check_replies_inputs(name)
            client.dispatch_workflow(WORKFLOW_CHECK_REPLIES, inputs)
            triggered.append(name)
        except GitHubActionsError:
            failed.append(name)
    st.success(f"Triggered a check for {len(triggered)} campaign(s).")
    if failed:
        st.warning(f"Failed to trigger for: {', '.join(failed)}")
