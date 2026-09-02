"""
asana_sync.py — the actual one-way Sheet -> Asana projection. Independent
of the bridge and DM Queue on purpose: an earlier design considered calling
this inline from the bridge/DM-drafting scripts, and that was deliberately
rejected — Asana going down must never be able to block or slow an email
push or a DM draft. This runs as its own process, on its own schedule,
reading whatever state already exists in the Shortlist tab.

Deliberately one-way: writes asana_task_id/asana_synced_at back onto the
Shortlist row (this sync's own bookkeeping), but never reads anything FROM
Asana to decide what outreach.py, dm_drafting.py, or the bridge should do —
nothing here can influence a send or a draft.

Uses only basic Asana task fields (name, notes) — see asana_client.py's
own docstring for why: Paula's actual project custom-field structure isn't
known yet, and this way a working v1 doesn't need to wait for it.
"""
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Resolved from this file's own location, not the process's current working
# directory — same reasoning as every other cross-folder import in this
# build. NOTE: only ONE dirname() call here, not two — asana/ is a direct
# sibling of discovery/, unlike outreach/streamlit_app/ (two levels deep),
# where creator_research_logic.py's version of this needs two. Copying
# that pattern without adjusting for the different depth is exactly the
# bug this comment exists to prevent reintroducing.
_DISCOVERY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "discovery")
import sys  # noqa: E402
sys.path.insert(0, os.path.normpath(_DISCOVERY_DIR))
import campaign_settings as cs  # noqa: E402

from asana_client import AsanaClient  # noqa: E402


def select_rows_to_sync(shortlist_records: List[Dict], all_campaign_settings: Dict) -> List[Dict]:
    """Every row whose Campaign has asana_sync enabled AND has been routed
    to a real channel (email or dm — never "none" or blank, since those
    were explicitly decided against outreach). Includes rows that are
    ALREADY synced (asana_task_id present) — those get an update, not a
    skip, so Asana doesn't go stale the moment a status changes. row["_row"]
    must already be set by the caller, matching every other script here."""
    eligible = []
    for r in shortlist_records:
        campaign = r.get("Campaign", "")
        if not cs.is_asana_sync_enabled(campaign, all_campaign_settings):
            continue
        channel = r.get("outreach_channel", "").strip().lower()
        if channel not in ("email", "dm"):
            continue
        eligible.append(r)
    return eligible


def build_task_payload(row: Dict) -> Dict[str, str]:
    """Pure function — a creator row becomes an Asana task's name/notes.
    No I/O, so the exact wording is fully unit-testable without touching
    a real Asana account."""
    name = f"{row.get('username', '?')} — {row.get('Campaign', '?')}"

    channel = row.get("outreach_channel", "").strip().lower()
    if channel == "dm":
        # Explicit, not implied — Paula's team should never mistake a DM
        # row for something the pipeline will finish automatically.
        status_line = f"Channel: DM — awaiting manual send. Status: {row.get('dm_status') or 'Not Contacted'}"
    else:
        pushed = row.get("campaign_push_status", "").strip() or "not yet pushed"
        status_line = f"Channel: Email. Push status: {pushed}"
        if row.get("outreach_campaign"):
            status_line += f" (outreach campaign: {row['outreach_campaign']})"

    lines = [
        status_line,
        f"Platform: {row.get('platform', '—')}",
        f"Followers: {row.get('followers_count', '—')} ({row.get('follower_verification', 'unverified')})",
        f"Contact: {row.get('contact_email') or '—'}",
        f"Overall fit: {row.get('overall_fit', '—')}",
        f"Content angle: {row.get('content_angle', '—')}",
    ]
    if row.get("dr_concerns"):
        lines.append(f"Concerns: {row['dr_concerns']}")

    return {"name": name, "notes": "\n".join(lines)}


def sync_campaign(shortlist_ws, all_campaign_settings: Dict, asana_client: Optional[AsanaClient],
                   dry_run: bool = False) -> List[Dict]:
    """The actual sync pass. asana_client is None only when dry_run is
    True — a dry run never needs real Asana credentials at all, matching
    the bridge's own dry-run contract (no connector built, nothing that
    could possibly write anywhere).

    Returns one result dict per row: {"dedup_key", "status": "created" |
    "updated" | "preview" | "failed", "task_gid" (when known), "error"
    (when failed)}. A single row's failure never stops the rest — same
    error-isolation philosophy as everywhere else in this pipeline.
    """
    records = shortlist_ws.get_all_records()
    eligible = []
    for i, r in enumerate(records, start=2):
        row = dict(r)
        row["_row"] = i
        eligible.append(row)

    rows_to_sync = select_rows_to_sync(eligible, all_campaign_settings)
    results = []
    now = datetime.now(timezone.utc).isoformat()

    for row in rows_to_sync:
        payload = build_task_payload(row)
        existing_task_id = row.get("asana_task_id", "").strip()

        if dry_run:
            action = "would update" if existing_task_id else "would create"
            results.append({"dedup_key": row.get("dedup_key"), "status": "preview",
                             "action": action, "payload": payload})
            continue

        try:
            if existing_task_id and asana_client.task_exists(existing_task_id):
                asana_client.update_task(existing_task_id, name=payload["name"], notes=payload["notes"])
                results.append({"dedup_key": row.get("dedup_key"), "status": "updated",
                                 "task_gid": existing_task_id})
                _write_sync_result(shortlist_ws, row["_row"], existing_task_id, now)
            else:
                # Either never synced, or the task was deleted directly in
                # Asana since the last sync — either way, create fresh.
                new_gid = asana_client.create_task(payload["name"], payload["notes"])
                results.append({"dedup_key": row.get("dedup_key"), "status": "created", "task_gid": new_gid})
                _write_sync_result(shortlist_ws, row["_row"], new_gid, now)
        except Exception as exc:  # noqa: BLE001 - isolate this row, keep going
            results.append({"dedup_key": row.get("dedup_key"), "status": "failed", "error": str(exc)})

    return results


def _write_sync_result(shortlist_ws, row_num: int, task_gid: str, synced_at: str) -> None:
    header = shortlist_ws.row_values(1)
    updates = {"asana_task_id": task_gid, "asana_synced_at": synced_at}
    for col_name, value in updates.items():
        if col_name in header:
            col_index = header.index(col_name) + 1
            shortlist_ws.update_cell(row_num, col_index, value)
