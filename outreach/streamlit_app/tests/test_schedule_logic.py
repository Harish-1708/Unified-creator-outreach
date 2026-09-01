import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schedule_logic import (
    validate_schedule, build_updated_schedule_override, get_current_schedule,
    timezone_display_name, COMMON_TIMEZONES, WEEKDAY_DEFAULTS,
)


# ---------- validate_schedule ----------

def test_validate_schedule_valid():
    assert validate_schedule("America/Los_Angeles", "09:00", "17:00", ["mon", "tue"]) == []


def test_validate_schedule_rejects_unknown_timezone():
    errors = validate_schedule("Not/A/Zone", "09:00", "17:00", ["mon"])
    assert len(errors) == 1
    assert "recognized timezone" in errors[0]


def test_validate_schedule_rejects_bad_time_format():
    errors = validate_schedule("UTC", "9am", "5pm", ["mon"])
    assert len(errors) == 2


def test_validate_schedule_rejects_no_days_selected():
    errors = validate_schedule("UTC", "09:00", "17:00", [])
    assert len(errors) == 1
    assert "at least one day" in errors[0]


def test_validate_schedule_reports_multiple_errors_at_once():
    errors = validate_schedule("Bad/Zone", "bad", "bad", [])
    assert len(errors) == 4


def test_validate_schedule_every_common_timezone_is_actually_valid():
    """Guards against a typo in the curated COMMON_TIMEZONES list itself —
    every IANA name offered in the dropdown must actually be real."""
    for _, iana_name in COMMON_TIMEZONES:
        assert validate_schedule(iana_name, "09:00", "17:00", ["mon"]) == []


# ---------- build_updated_schedule_override ----------

def test_build_updated_schedule_override_sets_schedule_key():
    updated = build_updated_schedule_override({}, "America/Los_Angeles", "09:00", "17:00", ["mon", "tue"])
    assert updated["schedule"] == {
        "timezone": "America/Los_Angeles", "window_start": "09:00", "window_end": "17:00",
        "send_days": ["mon", "tue"],
    }


def test_build_updated_schedule_override_preserves_other_keys():
    raw = {"status": "paused", "sending": {"daily_limit": 50}}
    updated = build_updated_schedule_override(raw, "UTC", "09:00", "17:00", ["mon"])
    assert updated["status"] == "paused"
    assert updated["sending"] == {"daily_limit": 50}


def test_build_updated_schedule_override_never_mutates_input():
    raw = {"status": "active"}
    build_updated_schedule_override(raw, "UTC", "09:00", "17:00", ["mon"])
    assert raw == {"status": "active"}


def test_build_updated_schedule_override_replaces_existing_schedule_wholesale():
    raw = {"schedule": {"timezone": "UTC", "window_start": "00:00", "window_end": "23:59", "send_days": ["sun"]}}
    updated = build_updated_schedule_override(raw, "America/New_York", "10:00", "18:00", ["mon", "fri"])
    assert updated["schedule"]["timezone"] == "America/New_York"
    assert updated["schedule"]["send_days"] == ["mon", "fri"]


# ---------- get_current_schedule ----------

def test_get_current_schedule_defaults_when_none_configured():
    schedule = get_current_schedule({})
    assert schedule["timezone"] == "America/Los_Angeles"
    assert schedule["window_start"] == "09:00"
    assert schedule["window_end"] == "17:00"
    assert schedule["send_days"] == WEEKDAY_DEFAULTS


def test_get_current_schedule_reads_existing_values():
    campaign_cfg = {"schedule": {"timezone": "UTC", "window_start": "08:00", "window_end": "16:00",
                                  "send_days": ["mon"]}}
    schedule = get_current_schedule(campaign_cfg)
    assert schedule == {"timezone": "UTC", "window_start": "08:00", "window_end": "16:00", "send_days": ["mon"]}


def test_get_current_schedule_partial_config_fills_in_defaults():
    campaign_cfg = {"schedule": {"timezone": "UTC"}}
    schedule = get_current_schedule(campaign_cfg)
    assert schedule["timezone"] == "UTC"
    assert schedule["window_start"] == "09:00"  # filled in
    assert schedule["send_days"] == WEEKDAY_DEFAULTS  # filled in


# ---------- timezone_display_name ----------

def test_timezone_display_name_known():
    assert timezone_display_name("America/Los_Angeles") == "Pacific Time (US & Canada)"


def test_timezone_display_name_unknown_returns_none():
    assert timezone_display_name("Some/Other/Zone") is None
