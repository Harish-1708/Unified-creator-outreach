import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate, current_user  # noqa: E402
from config import TEMPLATES_ROOT, WORKFLOW_DASHBOARD  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns  # noqa: E402
from campaign_builder import (  # noqa: E402
    validate_campaign_name, validate_variant_content, build_campaign_files,
    get_next_stage_for_campaign, commit_message_for_campaign, VARIANT_LETTERS,
)

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

if not login_gate():
    st.stop()

st.title("➕ New Campaign / Add Stage")
st.markdown(
    """
Commits template files **directly** — no GitHub trip, no pull request to approve.
The campaign (or stage) is live the moment you click the button below, and its
Sheet tabs are created automatically right after.
    """
)


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


def _initialize_campaign_tabs(campaign_name: str) -> None:
    """Triggers the Dashboard workflow right after a commit — running it
    connects to the Sheet, which creates every needed tab (Master,
    Responses, Send Log, Error Log, Dashboard) with the correct header as
    a side effect. This is what fixes the "tab doesn't exist" error you'd
    otherwise hit the first time you open Dashboard/Controls for a brand
    new campaign — no manual Preview/Send run required first."""
    try:
        client = _get_github_client()
        client.dispatch_workflow(WORKFLOW_DASHBOARD, {"campaign": campaign_name})
    except GitHubActionsError as exc:
        st.warning(
            f"Campaign created, but couldn't auto-initialize its Sheet tabs: {exc}. "
            f"Run 'Update Dashboard' manually for '{campaign_name}' from the GitHub Actions "
            "tab, or just visit Dashboard/Controls once — either will create them."
        )


try:
    existing_campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list existing campaigns: {exc}")
    existing_campaigns = []

mode = st.radio(
    "What do you want to do?",
    ["Create a new campaign (Intro)", "Add the next stage to an existing campaign"],
    horizontal=True,
)

# =============================================================================
# Mode 1 — brand new campaign, starts at Intro, variant count is your choice
# =============================================================================
if mode == "Create a new campaign (Intro)":
    campaign_name = st.text_input("Campaign name (letters, numbers, underscores only)")
    num_variants = st.slider("Number of Intro variants (A/B/C/D)", min_value=1, max_value=4, value=1)

    variant_inputs = {}
    for letter in VARIANT_LETTERS[:num_variants]:
        st.subheader(f"Variant {letter}")
        subject = st.text_input(f"Subject ({letter})", key=f"new_subject_{letter}")
        body = st.text_area(f"Body ({letter})", key=f"new_body_{letter}", height=150)
        variant_inputs[letter] = {"subject": subject, "body": body}

    confirm = st.checkbox(
        "I've reviewed this content — create the campaign now (it goes live immediately, no approval step)",
        key="new_campaign_confirm",
    )

    if st.button("Create Campaign", type="primary", key="new_campaign_submit", disabled=not confirm):
        errors = []
        name_error = validate_campaign_name(campaign_name, existing_campaigns)
        if name_error:
            errors.append(name_error)
        for letter, content in variant_inputs.items():
            content_error = validate_variant_content(content["subject"], content["body"])
            if content_error:
                errors.append(f"Variant {letter}: {content_error}")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                files = build_campaign_files(campaign_name, "intro", variant_inputs)
                client = _get_github_client()
                client.commit_campaign_files_directly(
                    files=files,
                    commit_message=commit_message_for_campaign(campaign_name, "intro", len(files),
                                                                 current_user(), is_new_campaign=True),
                )
                st.success(f"Campaign '{campaign_name}' created and live.")
                with st.spinner("Initializing Sheet tabs..."):
                    time.sleep(1)  # give GitHub a moment to register the commit before the workflow reads it
                    _initialize_campaign_tabs(campaign_name)
                st.info("Sheet tabs are being created — Dashboard/Controls will work for it within a minute or two.")
            except GitHubActionsError as exc:
                st.error(f"Failed to create campaign: {exc}")

# =============================================================================
# Mode 2 — add the NEXT stage to an existing campaign. Stage name and
# variant letters are NOT free choices here — they're computed from what
# outreach.py's own auto-discovery would accept next (see
# campaign_builder.get_next_stage_for_campaign), so this can never create
# an inconsistent campaign.
# =============================================================================
else:
    if not existing_campaigns:
        st.info("No existing campaigns to add a stage to yet — create one first.")
    else:
        selected_campaign = st.selectbox("Campaign", existing_campaigns)

        try:
            next_stage = get_next_stage_for_campaign(selected_campaign, TEMPLATES_ROOT)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't inspect '{selected_campaign}': {exc}")
            next_stage = None

        if next_stage is None:
            st.info(f"'{selected_campaign}' already has all 5 stages — there's nothing left to add.")
        else:
            stage_prefix, required_variants = next_stage
            st.write(f"**Next stage:** `{stage_prefix}` · **Required variants:** {', '.join(required_variants)}")
            st.caption(
                "These aren't a free choice — every stage must offer the exact same variant "
                "letters as the campaign's existing stages, so all of them are required here."
            )

            variant_inputs = {}
            for letter in required_variants:
                st.subheader(f"Variant {letter}")
                subject = st.text_input(
                    f"Subject ({letter}) — leave blank to continue the same thread as this lead's "
                    "previous email instead of starting a new one",
                    key=f"stage_subject_{letter}",
                )
                body = st.text_area(f"Body ({letter})", key=f"stage_body_{letter}", height=150)
                variant_inputs[letter] = {"subject": subject, "body": body}

            confirm_stage = st.checkbox(
                "I've reviewed this content — add this stage now (it goes live immediately, no approval step)",
                key="add_stage_confirm",
            )

            if st.button("Add Stage", type="primary", key="add_stage_submit", disabled=not confirm_stage):
                errors = []
                for letter, content in variant_inputs.items():
                    content_error = validate_variant_content(content["subject"], content["body"],
                                                               is_first_stage=False)
                    if content_error:
                        errors.append(f"Variant {letter}: {content_error}")

                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    try:
                        files = build_campaign_files(selected_campaign, stage_prefix, variant_inputs)
                        client = _get_github_client()
                        client.commit_campaign_files_directly(
                            files=files,
                            commit_message=commit_message_for_campaign(selected_campaign, stage_prefix, len(files),
                                                                         current_user(), is_new_campaign=False),
                        )
                        st.success(f"'{stage_prefix}' added to '{selected_campaign}' and live.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to add stage: {exc}")
