"""Pure logic for reconstructing a full conversation thread for one lead
— every outgoing stage actually sent (re-rendered from the same
templates used at send time, with the lead's own locked-in variant, so
this can never drift from what was truly sent) merged chronologically
with every inbound reply already logged (using FullBody when available,
falling back to Snippet for responses logged before that column
existed). No live IMAP fetch, no new store — everything here comes from
data the system already has.
"""
import os
import sys
from typing import Dict, List

import config

if config.REPO_ROOT not in sys.path:
    sys.path.insert(0, config.REPO_ROOT)

import outreach  # noqa: E402


def build_outgoing_messages_for_lead(campaign_cfg: Dict, lead: Dict) -> List[Dict]:
    """One entry per stage actually sent to this lead (has a non-blank
    SentAt), re-rendered from the exact template + variant locked in at
    send time. A stage that fails to render for some reason (a template
    since deleted, a data issue) is skipped rather than breaking the
    whole conversation view over one bad stage."""
    templates_dir = os.path.join(config.TEMPLATES_ROOT, campaign_cfg["_campaign_name"])
    messages = []
    for index, stage_prefix in enumerate(outreach.CANONICAL_STAGE_ORDER):
        fields = outreach.stage_field_names(index)
        sent_at = (lead.get(fields["sent_at"]) or "").strip()
        if not sent_at:
            continue
        variant = (lead.get(fields["variant"]) or "").strip()
        if not variant:
            continue
        try:
            rendered = outreach.render_email(templates_dir, stage_prefix, variant, lead,
                                              is_first_stage=(index == 0))
        except Exception:  # noqa: BLE001 - one bad stage shouldn't sink the whole thread
            continue
        messages.append({
            "direction": "outgoing", "timestamp": sent_at, "subject": rendered["subject"],
            "body": rendered["body"],
        })
    return messages


def build_incoming_messages_for_responses(responses_for_lead: List[Dict]) -> List[Dict]:
    """One entry per logged reply — FullBody when available, falling
    back to Snippet for responses logged before that column existed
    (so an old conversation still shows something, just shorter)."""
    messages = []
    for r in responses_for_lead:
        body = r.get("FullBody") or r.get("Snippet", "")
        messages.append({
            "direction": "incoming", "timestamp": r.get("ReceivedAt", ""),
            "subject": r.get("Subject", ""), "body": body, "from": r.get("From", ""),
        })
    return messages


def filter_responses_for_lead(responses: List[Dict], lead_id: str) -> List[Dict]:
    """Every logged response for one specific lead, from a campaign's
    full response list — a conversation thread is always scoped to one
    lead, never mixed with anyone else's replies."""
    lead_id = str(lead_id)
    return [r for r in responses if str(r.get("LeadID", "")) == lead_id]


def build_conversation_thread(campaign_cfg: Dict, lead: Dict, responses_for_lead: List[Dict]) -> List[Dict]:
    """Every outgoing stage actually sent, plus every logged reply for
    this lead, merged into one chronologically-sorted thread — oldest
    first, matching how a real email client displays a conversation."""
    outgoing = build_outgoing_messages_for_lead(campaign_cfg, lead)
    incoming = build_incoming_messages_for_responses(responses_for_lead)
    combined = outgoing + incoming
    return sorted(combined, key=lambda m: m.get("timestamp", ""))
