"""
sync_dm_asana.py — creates/updates Asana tasks for creators routed to DM,
in the EXACT SAME Asana project and stage vocabulary as email leads. This
is deliberately not a separate, parallel Asana integration — an earlier
version of this feature was exactly that (its own dormant workflow, its
own dedupe columns, disconnected from the outreach-side sync), and it was
removed specifically because running two independent Asana integrations
side by side was confusing and risked drifting out of sync with each
other. This reuses outreach.py's own, already-tested Asana functions
directly (project lookup, task create/update, the 404-vs-403 self-heal
logic, the retry wrapper) rather than re-deriving any of it a second
time — the two integrations share one set of rules by construction, not
by convention.

Reads Shortlist rows for ONE campaign where outreach_channel == "dm" and
review_status == "Approved". A DM creator's Asana stage is derived from
dm_status via outreach.compute_dm_asana_stage — Not Contacted/Draft Ready
= Sourced, Sent = Outreach Sent, Follow-up Needed/No Response = Follow-up,
Replied/Interested/Not Interested/Closed = Negotiating. Rights Secured
and Declined/Dead are never auto-assigned here either — same rule as the
email side, always a human decision made directly in Asana.

Run only via GitHub Actions — the "Sync DM to Asana" workflow, dispatched
either on its own or alongside "Sync Asana" (the email-side workflow)
from the same "Sync to Asana Now" button, so one click covers both
channels going into the same project.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outreach"))

import gspread  # noqa: E402
from google.oauth2.service_account import Credentials  # noqa: E402

import outreach  # noqa: E402 — reuses its Asana functions directly, see module docstring for why

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def build_dm_asana_task_name(row: dict) -> str:
    """Adapts a discovery Shortlist row's field names (Campaign, dr_name/
    username, content_angle) into the {Client, CreatorHandle, Product}
    shape outreach.build_asana_task_name expects, then calls that
    function UNCHANGED — reusing its exact naming logic (including the
    CreatorHandle-falls-back-to-Creator rule) rather than re-deriving a
    second, potentially-divergent version of the same naming
    convention."""
    adapted = {
        "Client": row.get("Campaign", ""),
        "CreatorHandle": row.get("dr_name", ""),
        "Creator": row.get("username", ""),
        "Product": row.get("content_angle", ""),
    }
    return outreach.build_asana_task_name(adapted)


def sync_dm_to_asana(shortlist_ws, campaign: str, project_name: str, api_key: str) -> dict:
    """Returns {"created": int, "updated": int, "skipped": int, "errors": [str, ...]}
    — same shape as outreach.sync_campaign_to_asana, for a consistent
    summary regardless of which channel ran."""
    project_gid = outreach.asana_find_project_gid(project_name, api_key)
    structure = outreach.asana_get_project_structure(project_gid, api_key)
    sections = structure["sections"]
    custom_field_defs = structure["custom_fields"]

    header = shortlist_ws.row_values(1)
    records = shortlist_ws.get_all_records()

    created, updated, skipped = 0, 0, 0
    errors = []

    for i, row in enumerate(records, start=2):
        if row.get("Campaign") != campaign:
            continue
        if (row.get("outreach_channel") or "").strip().lower() != "dm":
            continue
        if (row.get("review_status") or "").strip().lower() != "approved":
            skipped += 1
            continue

        dedup_key = row.get("dedup_key", "")
        stored_gid = outreach._safe_lead_str(row.get("asana_task_id"))
        existing_gid_override = None
        current_section = None

        if stored_gid:
            try:
                current_section = outreach.asana_get_task_current_section(stored_gid, project_gid, api_key)
            except outreach.AsanaTaskNotFoundError:
                existing_gid_override = ""  # self-heal: same reasoning as the email-side sync
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{dedup_key}: couldn't check existing task {stored_gid} — {exc}")
                continue

        target_stage = outreach.compute_dm_asana_stage(row.get("dm_status", ""))
        decision = outreach.decide_asana_sync_action(
            {"AsanaTaskGID": stored_gid}, current_section, existing_task_gid=existing_gid_override,
            computed_stage=target_stage)
        task_name = build_dm_asana_task_name(row)
        custom_fields_payload = outreach.build_asana_custom_fields_payload(row, custom_field_defs)

        try:
            if decision["action"] == "create":
                target_section_gid = sections.get(decision["target_stage"])
                new_gid = outreach.asana_create_task(project_gid, target_section_gid, task_name,
                                                      custom_fields_payload, api_key)
                if "asana_task_id" in header:
                    col_index = header.index("asana_task_id") + 1
                    shortlist_ws.update_cell(i, col_index, new_gid)
                created += 1
            else:
                # asana_update_task has no section-moving concept of its
                # own — moving sections is a SEPARATE Asana API call
                # (asana_move_task_to_section), made only when the stage
                # actually needs to change and only when
                # decide_asana_sync_action's own human-only-stage
                # protection allows it (target_stage is None otherwise).
                outreach.asana_update_task(stored_gid, task_name, custom_fields_payload, api_key)
                if decision["target_stage"] and decision["target_stage"] != current_section:
                    move_to_gid = sections.get(decision["target_stage"])
                    if move_to_gid:
                        outreach.asana_move_task_to_section(stored_gid, move_to_gid, api_key)
                updated += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{dedup_key}: {exc}")

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def main():
    campaign = os.environ.get("CAMPAIGN", "").strip()
    project_name = os.environ.get("ASANA_PROJECT_NAME", "").strip() or campaign
    api_key = os.environ.get("ASANA_ACCESS_TOKEN", "")

    if not campaign:
        raise ValueError("Missing required input: CAMPAIGN")
    if not api_key:
        print("ASANA_ACCESS_TOKEN is not set — nothing to do.")
        return

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    shortlist_ws = sheet.worksheet("Shortlist")

    summary = sync_dm_to_asana(shortlist_ws, campaign, project_name, api_key)
    print(f"DM Asana sync for '{campaign}': {summary['created']} created, {summary['updated']} updated, "
          f"{summary['skipped']} skipped (not approved).")
    for err in summary["errors"]:
        print(f"  ERROR: {err}")
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
