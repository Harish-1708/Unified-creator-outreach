"""Pure logic for the Settings tab (Phase F). Persists sender selection
and daily limits into the campaign's EXISTING config override file
(config/campaigns/<name>.yaml) — the same file outreach.get_campaign
already deep-merges, not a new config surface. Only the 'sending' key's
relevant fields are ever touched; status, schedule, stages, variants, and
anything else already in that file are preserved exactly as they were.
"""
import os
import sys
from typing import Dict, List, Optional

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def load_raw_override(campaign_name: str, campaigns_dir: str) -> Dict:
    """The override file's raw content, exactly as it is on disk — NOT
    merged with defaults (unlike campaign_cfg, which is always fully
    merged). Returns {} if the file doesn't exist yet — every campaign is
    valid without one (auto-discovery covers that case)."""
    path = os.path.join(campaigns_dir, f"{campaign_name}.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_settings(daily_limit: int, per_account_daily_limit: Optional[int]) -> List[str]:
    """Mirrors outreach.apply_sending_overrides' own validation rules, so
    Settings can never persist a value the core system would itself
    reject when it later loads this same file."""
    errors = []
    if daily_limit <= 0:
        errors.append("Daily limit must be a positive number.")
    if per_account_daily_limit is not None and per_account_daily_limit <= 0:
        errors.append("Per-account daily limit must be a positive number, or left blank for no limit.")
    return errors


def build_updated_override(raw_override: Dict, daily_limit: int, per_account_daily_limit: Optional[int],
                            sender_rotation: bool, rotation_accounts: List[str]) -> Dict:
    """Returns a NEW dict — never mutates raw_override. Only 'sending' is
    touched; every other top-level key (status, schedule, stages,
    variants, reply_monitor, ...) passes through untouched."""
    updated = dict(raw_override)
    sending = dict(updated.get("sending", {}))

    sending["daily_limit"] = daily_limit
    if per_account_daily_limit is not None:
        sending["per_account_daily_limit"] = per_account_daily_limit
    elif "per_account_daily_limit" in sending:
        del sending["per_account_daily_limit"]

    sending["sender_rotation"] = sender_rotation
    if rotation_accounts:
        sending["rotation_accounts"] = rotation_accounts
    elif "rotation_accounts" in sending:
        del sending["rotation_accounts"]

    updated["sending"] = sending
    return updated


def build_asana_settings_override(raw_override: Dict, enabled: bool, project_name: str) -> Dict:
    """Returns a NEW dict — never mutates raw_override. Only the 'asana'
    key is touched; everything else passes through untouched, same
    guarantee build_updated_override makes for 'sending'."""
    updated = dict(raw_override)
    updated["asana"] = {"enabled": enabled, "project_name": project_name}
    return updated


def override_to_yaml_bytes(override: Dict) -> bytes:
    return yaml.safe_dump(override, sort_keys=False, default_flow_style=False).encode("utf-8")


def override_file_path(campaign_name: str) -> str:
    return f"outreach/config/campaigns/{campaign_name}.yaml"
