"""Pure logic for the Responses tab's reply-from-app (Phase H1: plain text
reply with correct threading + CC/BCC — images and full quoted-thread
reconstruction are later sub-phases, see the Campaigns Hub plan). Keeps
pages/campaigns.py thin and lets this be tested without Streamlit.

Reuses outreach.is_valid_email_format as the one canonical email
validator in this codebase, rather than a second regex living here.
"""
import os
import re
import sys
from datetime import datetime
from email.utils import parseaddr
from typing import Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402


def find_lead_for_response(response: Dict, leads: List[Dict]) -> Optional[Dict]:
    lead_id = str(response.get("LeadID", ""))
    for lead in leads:
        if str(lead.get("LeadID", "")) == lead_id:
            return lead
    return None


def extract_email_address(raw: str) -> str:
    """'Rocky <harishdh16@gmail.com>' -> 'harishdh16@gmail.com'. A bare
    'harishdh16@gmail.com' passes through unchanged. Uses the standard
    library's own header-address parser rather than a hand-rolled regex,
    since "Name <email>" is a real RFC 5322 format with edge cases
    (quoted names, commas inside quotes) worth not re-deriving."""
    _, address = parseaddr(raw or "")
    return address or (raw or "").strip()


def build_reply_defaults(response: Dict, lead: Optional[Dict]) -> Dict[str, str]:
    """Sensible starting values for the reply form — every field stays
    fully editable before sending. Sender account defaults to whichever
    account this lead's automated sequence has been using, so the reply
    comes from the same address the lead already recognizes."""
    subject = (response.get("Subject") or "").strip()
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return {
        "to": extract_email_address(response.get("From", "")),
        "subject": subject or "Re:",
        "sender_account": (lead or {}).get("SenderAccount", ""),
    }


def build_reply_references(response: Dict, lead: Optional[Dict]) -> str:
    """Chains the lead's own accumulated ThreadReferences (from every
    automated stage sent so far) with this specific inbound message's own
    Message-ID. Not a byte-perfect RFC 5322 reconstruction — the Response
    Sheet doesn't store the lead's own References header — but enough for
    every mainstream email client to thread this correctly."""
    existing = ((lead or {}).get("ThreadReferences") or "").strip()
    inbound_id = (response.get("MessageID") or "").strip()
    parts = [p for p in [existing, inbound_id] if p]
    return " ".join(parts)


def parse_email_list(raw: str) -> List[str]:
    """Parses a comma/semicolon-separated Cc/Bcc input string into a
    clean list, dropping blanks and surrounding whitespace."""
    if not raw or not raw.strip():
        return []
    parts = re.split(r"[,;]", raw)
    return [p.strip() for p in parts if p.strip()]


def validate_reply(to_email: str, body: str, sender_account: str, cc: List[str], bcc: List[str],
                    attachment_total_bytes: int = 0) -> List[str]:
    errors = []
    if not sender_account:
        errors.append("No sender account resolved for this lead — check its SenderAccount in the Master Sheet.")
    if not to_email or not outreach.is_valid_email_format(to_email):
        errors.append("'To' must be a valid email address.")
    if not body or not body.strip():
        errors.append("Body is required.")
    for label, addrs in [("Cc", cc), ("Bcc", bcc)]:
        for addr in addrs:
            if not outreach.is_valid_email_format(addr):
                errors.append(f"'{addr}' in {label} is not a valid email address.")
    if attachment_total_bytes > outreach.MAX_TOTAL_ATTACHMENT_BYTES:
        max_mb = outreach.MAX_TOTAL_ATTACHMENT_BYTES / (1024 * 1024)
        actual_mb = attachment_total_bytes / (1024 * 1024)
        errors.append(f"Attachments total {actual_mb:.1f} MB, over the {max_mb:.0f} MB limit.")
    return errors


def build_reply_payload(response: Dict, lead: Optional[Dict], sender_account: str, to_email: str, subject: str,
                         body: str, cc: List[str], bcc: List[str],
                         attachments: Optional[List[Dict[str, str]]] = None) -> Dict:
    """to_email is whatever the caller's "To" field actually holds right
    now — NOT re-derived from response["From"] here. The person can edit
    that field (e.g. to fix a malformed address, or reply to someone
    else entirely), and that edit has to actually take effect; silently
    overriding it with the original raw From value would make the field
    decorative. attachments (optional): [{"filename": str,
    "content_base64": str}, ...] — already base64-encoded, ready to
    commit as JSON. See build_attachment_entries for turning raw
    uploaded bytes into this shape."""
    payload = {
        "sender_account": sender_account,
        "to": to_email,
        "subject": subject,
        "body": body,
        "in_reply_to": response.get("MessageID", ""),
        "references": build_reply_references(response, lead),
        "cc": cc,
        "bcc": bcc,
        "lead_id": str(response.get("LeadID", "")),
    }
    if attachments:
        payload["attachments"] = attachments
    return payload


def build_attachment_entries(uploaded_files: List) -> List[Dict[str, str]]:
    """uploaded_files: Streamlit UploadedFile objects (or anything with
    .name and .getvalue()). Returns [{"filename": str, "content_base64": str}, ...],
    ready to drop straight into build_reply_payload's attachments param."""
    import base64
    entries = []
    for f in uploaded_files:
        entries.append({"filename": f.name, "content_base64": base64.b64encode(f.getvalue()).decode("ascii")})
    return entries


def total_attachment_size_bytes(uploaded_files: List) -> int:
    return sum(len(f.getvalue()) for f in uploaded_files)


def reply_payload_path(campaign_name: str, response_id: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_response_id = str(response_id).replace("/", "_") or "unknown"
    return f"outreach/replies/{campaign_name}/{safe_response_id}-{ts}.json"
