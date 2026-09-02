"""
push_approved_to_campaign.py — the discovery -> outreach bridge.

Takes creator rows from the discovery pipeline's Shortlist tab that a
human has approved and routed to email, and pushes them into a real
outreach campaign as leads — reusing outreach.py's own, already-tested
import_leads() rather than writing new Sheets-append logic.

Deliberately does NOT modify outreach.py. Only imports three already-public
things from it: get_campaign(), SheetsConnector, and import_leads(). The
one thing import_leads() doesn't give back — which LeadID got assigned to
which email — is recovered by re-reading the campaign's leads after import
and matching on email, not by changing that function's return contract.

Deliberately does NOT modify discover.py or shortlist.py's own writing
logic either. Reads the Shortlist tab directly, writes back only the five
bridge-owned columns (campaign_push_status, outreach_campaign,
outreach_record_id, pushed_at, push_error) that discover.py's MASTER_HEADERS
already reserves for this.

Design notes worth keeping in mind while reading or changing this file:

- The outreach campaign a creator gets pushed to is chosen manually, per
  creator, at push time — it is NOT automatically the discovery Campaign
  string. Those are two different concepts (see the review discussion this
  was built from): a research campaign describes what was discovered, an
  outreach campaign describes what email sequence a lead receives, and one
  research campaign's creators may reasonably be split across several
  different outreach campaigns.

- Custom lead columns (SourceCreatorID, Platform, ProductFitScore,
  ContentAngle, FitExplanation) only actually land in the target sheet if
  that campaign's Master Sheet tab already has matching column headers —
  outreach.py's append_lead() writes against whatever the sheet's REAL
  header row already is, same as it does for any other custom column. This
  is existing, documented behavior, not something this bridge adds.

- Idempotency is enforced by the CALLER's query, not by this module
  re-checking: only pass in rows where campaign_push_status is blank or
  "failed". A row already marked "pushed" should never be included in
  `creators` in the first place.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from outreach import ConfigError, SheetsConnector, get_campaign, import_leads

# Which Shortlist-tab column becomes which outreach lead field. Kept as an
# explicit table (not scattered through the mapping function) so it's the
# one place to look when a template needs a new variable wired through.
_CUSTOM_LEAD_COLUMNS = {
    "SourceCreatorID": "dedup_key",
    "Platform": "platform",
    "ProductFitScore": "product_fit_score",
    "ContentAngle": "content_angle",
    "FitExplanation": "fit_explanation",
}


def map_creator_to_lead(creator: Dict[str, str]) -> Dict[str, str]:
    """Pure mapping, no I/O — a discovery Shortlist row becomes an
    outreach lead dict. Kept separate from the push logic so the mapping
    itself can be previewed (dry-run) or unit-tested without touching any
    sheet.

    FirstName prefers the Deep Research report's real display name
    (dr_name) over the raw handle — falling back to the handle only when
    no display name was ever captured (true for Serper/Gemini-only
    candidates). This is deliberate: reusing the raw @handle as a lead's
    FirstName would reintroduce, at this layer, the exact display-name/
    username confusion already fixed at the discovery layer.
    """
    dr_name = (creator.get("dr_name") or "").strip()
    first_name = dr_name.split()[0] if dr_name else (creator.get("username") or "").strip()

    lead = {
        "Email": (creator.get("contact_email") or "").strip(),
        "FirstName": first_name,
    }
    for lead_col, creator_col in _CUSTOM_LEAD_COLUMNS.items():
        lead[lead_col] = creator.get(creator_col, "")
    return lead


def push_creators_to_outreach(discovery_ws, creators: List[Dict], outreach_campaign: str,
                               dry_run: bool = False) -> List[Dict]:
    """
    creators: rows already fetched by the caller from the Shortlist tab —
    each must include "_row" (1-indexed sheet row number, for writing the
    result back) alongside the usual discovery fields. Callers are
    responsible for filtering to outreach_channel == "email" and
    campaign_push_status in ("", "failed") BEFORE calling this — this
    function pushes whatever list it's given, no filtering of its own.

    Returns one result dict per input creator:
      {"dedup_key": ..., "status": "pushed"|"failed"|"skipped_duplicate"|
                                    "skipped_no_email"|"preview",
       "outreach_record_id": ... (only when status == "pushed"),
       "error": ... (only when status == "failed"),
       "lead": ... (only when dry_run, the mapped lead dict)}

    A bad campaign name fails every creator in the batch with the same
    clear reason (get_campaign's own ConfigError, unchanged) rather than
    each one raising its own confusing per-row exception. A single
    creator's bad data (no email, duplicate email) is isolated to that
    creator — the rest of the batch is unaffected, matching outreach.py's
    own error-isolation philosophy in send_batch/check_replies.
    """
    results: List[Dict] = []
    if not creators:
        return results

    now = datetime.now(timezone.utc).isoformat()

    try:
        campaign_cfg = get_campaign(outreach_campaign)
    except ConfigError as e:
        for c in creators:
            results.append({"dedup_key": c["dedup_key"], "status": "failed", "error": str(e)})
            if not dry_run:
                _write_push_result(discovery_ws, c["_row"], "failed", pushed_at=now, push_error=str(e))
        return results

    if dry_run:
        for c in creators:
            results.append({"dedup_key": c["dedup_key"], "status": "preview", "lead": map_creator_to_lead(c)})
        return results

    sheets = SheetsConnector(
        campaign_cfg["sheet_id"], campaign_cfg["master_tab"], campaign_cfg["responses_tab"],
        campaign_cfg["send_log_tab"], campaign_cfg["error_log_tab"], campaign_cfg["dashboard_tab"],
    )

    leads_to_import = []
    creator_by_lead_index = []
    for c in creators:
        lead = map_creator_to_lead(c)
        email = lead["Email"]
        if not email:
            results.append({"dedup_key": c["dedup_key"], "status": "skipped_no_email"})
            _write_push_result(discovery_ws, c["_row"], "skipped_no_email", pushed_at=now)
            continue
        leads_to_import.append(lead)
        creator_by_lead_index.append(c)

    if not leads_to_import:
        return results

    # Captured BEFORE import specifically so "this email already existed"
    # and "this email exists because we just added it" are distinguishable
    # afterward — matching on the post-import state alone can't tell them
    # apart, since both look identical once the call returns.
    existing_emails_before = {
        (lead.get("Email") or "").strip().lower() for lead in sheets.get_all_leads()
    }

    import_leads(sheets, outreach_campaign, leads_to_import)

    lead_id_by_email = {
        (lead.get("Email") or "").strip().lower(): lead.get("LeadID")
        for lead in sheets.get_all_leads()
    }

    # Iterated as a list, not a dict keyed by email — two different
    # creators can legitimately share one contact email (e.g. the same
    # agency inbox), and a dict would silently collapse them into one
    # result, dropping the second creator's outcome entirely.
    seen_this_batch = set()
    for lead, c in zip(leads_to_import, creator_by_lead_index):
        email_lower = lead["Email"].strip().lower()
        if email_lower in existing_emails_before or email_lower in seen_this_batch:
            results.append({"dedup_key": c["dedup_key"], "status": "skipped_duplicate"})
            _write_push_result(discovery_ws, c["_row"], "skipped_duplicate", pushed_at=now)
            continue
        seen_this_batch.add(email_lower)
        lead_id = lead_id_by_email.get(email_lower)
        results.append({"dedup_key": c["dedup_key"], "status": "pushed", "outreach_record_id": lead_id})
        _write_push_result(discovery_ws, c["_row"], "pushed", pushed_at=now,
                            outreach_campaign=outreach_campaign, outreach_record_id=lead_id)

    return results


def _write_push_result(discovery_ws, row_num: int, status: str, pushed_at: str,
                        outreach_campaign: Optional[str] = None,
                        outreach_record_id: Optional[str] = None,
                        push_error: Optional[str] = None) -> None:
    """Writes back onto the discovery Shortlist row — only the five
    bridge-owned columns discover.py's MASTER_HEADERS already reserves for
    this, never anything else on the row."""
    header = discovery_ws.row_values(1)
    updates = {
        "campaign_push_status": status,
        "pushed_at": pushed_at,
        "outreach_campaign": outreach_campaign or "",
        "outreach_record_id": outreach_record_id or "",
        "push_error": push_error or "",
    }
    for col_name, value in updates.items():
        if col_name in header:
            col_index = header.index(col_name) + 1
            discovery_ws.update_cell(row_num, col_index, value)
