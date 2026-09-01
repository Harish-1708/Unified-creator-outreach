import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from launch_logic import (
    build_status_override, STATUS_ACTIVE, STATUS_PAUSED, build_delete_override, build_restore_override,
)


def test_build_status_override_sets_status_on_empty_override():
    updated = build_status_override({}, STATUS_ACTIVE)
    assert updated == {"status": "active"}


def test_build_status_override_preserves_sending_and_schedule():
    raw = {"sending": {"daily_limit": 100}, "schedule": {"timezone": "UTC"}}
    updated = build_status_override(raw, STATUS_PAUSED)
    assert updated["status"] == "paused"
    assert updated["sending"] == {"daily_limit": 100}
    assert updated["schedule"] == {"timezone": "UTC"}


def test_build_status_override_never_mutates_input():
    raw = {"status": "draft"}
    build_status_override(raw, STATUS_ACTIVE)
    assert raw == {"status": "draft"}


def test_build_status_override_overwrites_existing_status():
    raw = {"status": "paused"}
    updated = build_status_override(raw, STATUS_ACTIVE)
    assert updated["status"] == "active"


def test_build_status_override_can_set_draft_to_active_launch():
    raw = {"status": "draft"}
    updated = build_status_override(raw, STATUS_ACTIVE)
    assert updated["status"] == "active"


# ---------- build_delete_override / build_restore_override ----------
# The real correctness requirement: Restore must bring a campaign back
# EXACTLY as it was before Temporarily Remove — not just default to
# Draft regardless of whether it was actually Running or Paused.

def test_build_delete_override_records_current_status_as_previous():
    updated = build_delete_override({"status": "active"}, "active")
    assert updated["status"] == "deleted"
    assert updated["previous_status"] == "active"


def test_build_delete_override_preserves_sending_and_schedule():
    raw = {"status": "active", "sending": {"daily_limit": 100}, "schedule": {"timezone": "UTC"}}
    updated = build_delete_override(raw, "active")
    assert updated["sending"] == {"daily_limit": 100}
    assert updated["schedule"] == {"timezone": "UTC"}


def test_build_delete_override_never_mutates_input():
    raw = {"status": "active"}
    build_delete_override(raw, "active")
    assert raw == {"status": "active"}


def test_build_delete_override_records_paused_correctly():
    updated = build_delete_override({"status": "paused"}, "paused")
    assert updated["previous_status"] == "paused"


def test_build_restore_override_restores_active_exactly():
    deleted = build_delete_override({"status": "active"}, "active")
    restored = build_restore_override(deleted)
    assert restored["status"] == "active"


def test_build_restore_override_restores_paused_exactly_not_draft():
    """The actual bug being fixed: a campaign that was Paused before
    Temporarily Remove must come back Paused, not silently become Draft
    (which would require re-Launching it, losing the distinction)."""
    deleted = build_delete_override({"status": "paused"}, "paused")
    restored = build_restore_override(deleted)
    assert restored["status"] == "paused"


def test_build_restore_override_restores_draft_exactly():
    deleted = build_delete_override({"status": "draft"}, "draft")
    restored = build_restore_override(deleted)
    assert restored["status"] == "draft"


def test_build_restore_override_removes_previous_status_key():
    deleted = build_delete_override({"status": "active"}, "active")
    restored = build_restore_override(deleted)
    assert "previous_status" not in restored


def test_build_restore_override_preserves_sending_and_schedule():
    raw = {"status": "active", "sending": {"daily_limit": 100}, "schedule": {"timezone": "UTC"}}
    deleted = build_delete_override(raw, "active")
    restored = build_restore_override(deleted)
    assert restored["sending"] == {"daily_limit": 100}
    assert restored["schedule"] == {"timezone": "UTC"}


def test_build_restore_override_never_mutates_input():
    deleted = build_delete_override({"status": "active"}, "active")
    frozen_copy = dict(deleted)
    build_restore_override(deleted)
    assert deleted == frozen_copy


def test_build_restore_override_defaults_to_active_when_previous_status_missing():
    """Defensive fallback — a hand-edited override file with 'deleted'
    but no previous_status shouldn't block Restore entirely."""
    restored = build_restore_override({"status": "deleted"})
    assert restored["status"] == "active"


def test_delete_then_restore_round_trips_to_original_status():
    for original_status in ["draft", "active", "paused"]:
        raw = {"status": original_status, "sending": {"daily_limit": 50}}
        deleted = build_delete_override(raw, original_status)
        restored = build_restore_override(deleted)
        assert restored["status"] == original_status
        assert restored["sending"] == {"daily_limit": 50}
        assert "previous_status" not in restored
