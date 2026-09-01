"""Pure logic for Launch/Pause/Resume (Phase G). Just flips the 'status'
key in the campaign's config override file — reuses settings_logic.py's
generic override read/write helpers (load_raw_override, override_to_yaml_
bytes, override_file_path), since status lives in the exact same file as
sending/schedule settings, not a separate store.

Launch/Pause/Resume are deliberately NOT gated by campaign readiness
(campaign_status_logic.compute_campaign_readiness) — that check is purely
informational (shown to the user as a heads-up), because the actual
system doesn't need it to be enforced: outreach.send_batch() already
naturally no-ops if there are no eligible leads, no sender, etc. Blocking
Launch on readiness would just be friction with no real safety benefit.
"""
from typing import Dict

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"


def build_status_override(raw_override: Dict, new_status: str) -> Dict:
    """Returns a NEW dict — never mutates raw_override. Only 'status' is
    touched; sending, schedule, and anything else pass through untouched
    — same guarantee settings_logic.build_updated_override and
    schedule_logic.build_updated_schedule_override each make for their
    own key."""
    updated = dict(raw_override)
    updated["status"] = new_status
    return updated


def build_delete_override(raw_override: Dict, current_status: str) -> Dict:
    """Temporarily Remove — sets status to 'deleted', but first remembers
    the EXACT status it's replacing (draft/active/paused) as
    previous_status, so Restore can put the campaign back exactly how it
    was — not just default to Draft regardless of whether it was
    actually Running or Paused beforehand. Everything else in the
    override (sending, schedule, ...) passes through untouched, same as
    build_status_override."""
    updated = dict(raw_override)
    updated["status"] = "deleted"
    updated["previous_status"] = current_status
    return updated


def build_restore_override(raw_override: Dict) -> Dict:
    """Restore — sets status back to whatever build_delete_override
    recorded as previous_status, then removes that now-stale key so it
    doesn't linger in the file. Falls back to 'active' if previous_status
    is somehow missing (e.g. the override file was hand-edited) rather
    than raising — a missing value here shouldn't block someone from
    getting their campaign back."""
    updated = dict(raw_override)
    updated["status"] = raw_override.get("previous_status") or "active"
    updated.pop("previous_status", None)
    return updated
