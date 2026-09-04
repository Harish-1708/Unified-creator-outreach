"""Pure logic for campaign status — the exact definitions from the
Campaigns Hub plan. All computable from local config + a read-only Sheet
fetch; none of it needs real SMTP credentials.

Status values: "draft", "running", "paused", "completed", "attention".
"draft" and "paused" come directly from campaign_cfg["status"] (explicit
human action). "running"/"completed"/"attention" are all derived for an
"active" campaign_cfg["status"] — never stored, always recomputed, so
they can never go stale.
"""
import os
import sys
from typing import Dict, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402

STATUS_DRAFT = "draft"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ATTENTION = "attention"
STATUS_DELETED = "deleted"

STATUS_LABELS = {
    STATUS_DRAFT: "📝 Draft",
    STATUS_RUNNING: "🟢 Running",
    STATUS_PAUSED: "⏸ Paused",
    STATUS_COMPLETED: "✅ Completed",
    STATUS_ATTENTION: "⚠️ Attention needed",
    STATUS_DELETED: "🗑️ Deleted (temporarily removed)",
}


def compute_campaign_readiness(campaign_cfg: Dict, leads: List[Dict]) -> Tuple[bool, List[str]]:
    """Config-only readiness check — this runs in Streamlit, which never
    has real SMTP credentials, so "has a sender account" is a heuristic
    (does *config* name one?) not a guarantee (GitHub Actions still does
    the real, authoritative check at send time and will raise a clear
    Sender-related error if this heuristic was wrong). Returns
    (is_ready, [problem descriptions])."""
    problems = []

    sending = campaign_cfg.get("sending", {})
    has_sender = bool(
        campaign_cfg.get("_global_default_account")
        or campaign_cfg.get("default_sender_account")
        or sending.get("rotation_accounts")
        or sending.get("sender_rotation")
    )
    if not has_sender:
        problems.append("No sender account configured")

    if not campaign_cfg.get("stages"):
        problems.append("No template stages found")

    has_lead_with_email = any((l.get("Email") or "").strip() for l in leads)
    if not has_lead_with_email:
        problems.append("No leads with an email address")

    return (len(problems) == 0, problems)


def is_lead_finished(lead: Dict, stages: List[Dict]) -> bool:
    """A lead needs no further action: it's in a terminal Status, OR it's
    already been sent the last configured stage."""
    status = lead.get("Status", "")
    if status.startswith("Stopped"):
        return True
    if not stages:
        return False
    last_sent_field = outreach.stage_field_names(len(stages) - 1)["sent_at"]
    return bool((lead.get(last_sent_field) or "").strip())


def compute_campaign_is_complete(campaign_cfg: Dict, leads: List[Dict]) -> bool:
    """True only if there's at least one sendable lead AND every one of
    them is finished — an empty/not-yet-populated campaign is NOT
    "completed", it just hasn't started."""
    stages = campaign_cfg.get("stages", [])
    sendable = [l for l in leads if (l.get("Email") or "").strip()]
    if not sendable:
        return False
    return all(is_lead_finished(l, stages) for l in sendable)


def compute_campaign_status(campaign_cfg: Dict, leads: List[Dict]) -> Tuple[str, List[str]]:
    """Returns (status, problems) — problems is only ever non-empty for
    "attention"."""
    raw_status = campaign_cfg.get("status") or "active"

    if raw_status == "deleted":
        return STATUS_DELETED, []
    if raw_status == "draft":
        return STATUS_DRAFT, []
    if raw_status == "paused":
        return STATUS_PAUSED, []

    # raw_status == "active" (or any unrecognized value — treat as active
    # rather than silently hiding a campaign over a typo)
    ready, problems = compute_campaign_readiness(campaign_cfg, leads)
    if not ready:
        return STATUS_ATTENTION, problems

    if compute_campaign_is_complete(campaign_cfg, leads):
        return STATUS_COMPLETED, []

    return STATUS_RUNNING, []


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
