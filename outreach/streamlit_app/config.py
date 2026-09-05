"""Shared path/config helpers for the Streamlit control panel.

Deliberately computes absolute paths from this file's location rather than
trusting the process's current working directory — Streamlit Community
Cloud's CWD conventions aren't guaranteed, and outreach.py's own functions
expect explicit paths when not run from the repo root.
"""
import os

STREAMLIT_APP_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(STREAMLIT_APP_DIR)

SETTINGS_PATH = os.path.join(REPO_ROOT, "config", "settings.yaml")
CAMPAIGNS_DIR = os.path.join(REPO_ROOT, "config", "campaigns")
TEMPLATES_ROOT = os.path.join(REPO_ROOT, "templates")

WORKFLOW_SEND = "send_batch.yml"
WORKFLOW_PREVIEW = "preview_batch.yml"
WORKFLOW_CHECK_REPLIES = "check_replies.yml"
WORKFLOW_DASHBOARD = "dashboard.yml"
WORKFLOW_BACKFILL_THREAD_SUBJECT = "backfill_thread_subject.yml"
WORKFLOW_IMPORT_LEADS = "import_leads.yml"
WORKFLOW_SYNC_ASANA = "sync_asana.yml"
WORKFLOW_SYNC_DM_ASANA = "sync_dm_asana.yml"
WORKFLOW_REMOVE_LEADS = "remove_leads.yml"
WORKFLOW_SEND_REPLY = "send_reply.yml"
WORKFLOW_MARK_RESPONSES_READ = "mark_responses_read.yml"
WORKFLOW_CHECK_ACCOUNT_HEALTH = "check_account_health.yml"
WORKFLOW_UPDATE_REVIEW_DECISION = "update_review_decision.yml"
WORKFLOW_UPDATE_DM_STATUS = "update_dm_status.yml"
WORKFLOW_ADD_MANUAL_CREATOR = "add_manual_creator.yml"
WORKFLOW_SYNC_SHORTLIST = "sync_shortlist.yml"
WORKFLOW_PUSH_TO_CAMPAIGN = "push_to_campaign.yml"
WORKFLOW_DISCOVER = "discover.yml"
WORKFLOW_PROMOTE_EXCLUDED = "promote_excluded_creator.yml"
WORKFLOW_EDIT_MASTER_ROW = "edit_master_row.yml"
WORKFLOW_DELETE_MASTER_CREATOR = "delete_master_creator.yml"

EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH = os.path.join(REPO_ROOT, "config", "email_account_slots.yaml")
