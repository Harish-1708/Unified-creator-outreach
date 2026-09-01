import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from accounts_logic import (
    aggregate_sent_today_by_account, build_account_rows, build_health_lookup, merge_account_directories,
)
from datetime import datetime

TODAY = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def test_aggregate_sums_across_campaigns_for_same_account():
    send_logs = {
        "CampaignA": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
        "CampaignB": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
    }
    totals = aggregate_sent_today_by_account(send_logs)
    assert totals == {"sales1": 2}


def test_aggregate_keeps_different_accounts_separate():
    send_logs = {
        "CampaignA": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
        "CampaignB": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales2"}],
    }
    totals = aggregate_sent_today_by_account(send_logs)
    assert totals == {"sales1": 1, "sales2": 1}


def test_aggregate_ignores_non_sent_status():
    send_logs = {"CampaignA": [{"Status": "error", "Timestamp": TODAY, "SenderAccount": "sales1"}]}
    assert aggregate_sent_today_by_account(send_logs) == {}


def test_aggregate_empty_input():
    assert aggregate_sent_today_by_account({}) == {}


def test_build_account_rows_sorted_by_name():
    directory = {"sales2": "sales2@x.com", "sales1": "sales1@x.com"}
    rows = build_account_rows(directory, {}, default_account="sales1")
    assert [r["name"] for r in rows] == ["sales1", "sales2"]


def test_build_account_rows_includes_send_count_and_default_flag():
    directory = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
    rows = build_account_rows(directory, {"sales1": 5}, default_account="sales2")

    by_name = {r["name"]: r for r in rows}
    assert by_name["sales1"]["sent_today"] == 5
    assert by_name["sales1"]["is_default"] is False
    assert by_name["sales2"]["sent_today"] == 0
    assert by_name["sales2"]["is_default"] is True


def test_build_account_rows_empty_directory():
    assert build_account_rows({}, {}, default_account="sales1") == []


def test_build_health_lookup_keys_by_account_name():
    records = [{"AccountName": "sales1", "Status": "Connected", "Detail": "", "CheckedAt": "2026-08-29 09:00:00"}]
    lookup = build_health_lookup(records)
    assert lookup["sales1"] == {"status": "Connected", "detail": "", "checked_at": "2026-08-29 09:00:00"}


def test_build_health_lookup_skips_records_with_no_account_name():
    records = [{"AccountName": "", "Status": "Connected"}]
    assert build_health_lookup(records) == {}


def test_build_health_lookup_multiple_accounts():
    records = [
        {"AccountName": "sales1", "Status": "Connected", "Detail": "", "CheckedAt": "t1"},
        {"AccountName": "sales2", "Status": "Disconnected", "Detail": "auth failed", "CheckedAt": "t2"},
    ]
    lookup = build_health_lookup(records)
    assert lookup["sales1"]["status"] == "Connected"
    assert lookup["sales2"]["status"] == "Disconnected"
    assert lookup["sales2"]["detail"] == "auth failed"


def test_build_health_lookup_empty_list():
    assert build_health_lookup([]) == {}


def test_build_account_rows_includes_health_status():
    directory = {"sales1": "sales1@x.com"}
    health_lookup = {"sales1": {"status": "Connected", "detail": "", "checked_at": "2026-08-29 09:00:00"}}
    rows = build_account_rows(directory, {}, default_account="sales1", health_lookup=health_lookup)
    assert rows[0]["status"] == "Connected"
    assert rows[0]["checked_at"] == "2026-08-29 09:00:00"


def test_build_account_rows_shows_unknown_when_no_health_data():
    """A brand new deployment (before check_account_health.yml has ever
    run) shouldn't error or show blank — it should say plainly that
    status isn't known yet."""
    directory = {"sales1": "sales1@x.com"}
    rows = build_account_rows(directory, {}, default_account="sales1", health_lookup=None)
    assert rows[0]["status"] == "Unknown"


def test_build_account_rows_shows_unknown_for_account_missing_from_health_lookup():
    """An account added to the directory AFTER the last health check
    hasn't been checked yet — shouldn't silently show a stale/wrong
    status from a different account."""
    directory = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
    health_lookup = {"sales1": {"status": "Connected", "detail": "", "checked_at": "t"}}
    rows = build_account_rows(directory, {}, default_account="sales1", health_lookup=health_lookup)
    by_name = {r["name"]: r for r in rows}
    assert by_name["sales1"]["status"] == "Connected"
    assert by_name["sales2"]["status"] == "Unknown"


def test_build_account_rows_shows_disconnected_detail():
    directory = {"sales1": "sales1@x.com"}
    health_lookup = {"sales1": {"status": "Disconnected", "detail": "AUTHENTICATIONFAILED", "checked_at": "t"}}
    rows = build_account_rows(directory, {}, default_account="sales1", health_lookup=health_lookup)
    assert rows[0]["status"] == "Disconnected"
    assert rows[0]["status_detail"] == "AUTHENTICATIONFAILED"


def test_merge_account_directories_combines_both_sources():
    streamlit_secret = {"legacy_account": "legacy@x.com"}
    slot_mapping = {"new_account": {"slot": 1, "address": "new@x.com"}}
    merged = merge_account_directories(streamlit_secret, slot_mapping)
    assert merged == {"legacy_account": "legacy@x.com", "new_account": "new@x.com"}


def test_merge_account_directories_slot_mapping_wins_on_collision():
    """The real invariant: an account edited via the app (only ever
    touches the slot mapping) must show its CURRENT address, not a stale
    one Streamlit's own secret still has because nothing keeps it synced."""
    streamlit_secret = {"sales1": "old_address@x.com"}
    slot_mapping = {"sales1": {"slot": 1, "address": "new_address@x.com"}}
    merged = merge_account_directories(streamlit_secret, slot_mapping)
    assert merged["sales1"] == "new_address@x.com"


def test_merge_account_directories_empty_slot_mapping_unchanged():
    streamlit_secret = {"sales1": "a@x.com"}
    merged = merge_account_directories(streamlit_secret, {})
    assert merged == {"sales1": "a@x.com"}


def test_merge_account_directories_empty_streamlit_secret():
    slot_mapping = {"sales1": {"slot": 1, "address": "a@x.com"}}
    merged = merge_account_directories({}, slot_mapping)
    assert merged == {"sales1": "a@x.com"}


def test_merge_account_directories_both_empty():
    assert merge_account_directories({}, {}) == {}
