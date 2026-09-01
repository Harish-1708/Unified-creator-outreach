import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from campaign_analytics_logic import (
    group_rows_by_section, build_overview_summary, build_per_stage_table,
    build_per_variant_table, build_sender_table, build_error_summary,
)

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _REPO_ROOT)
import outreach  # noqa: E402

STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
]


def _campaign_cfg():
    return {"stages": STAGES, "sending": {}}


def _real_dashboard_rows():
    """Builds a realistic scenario and runs it through the REAL
    outreach.compute_campaign_dashboard — proves this module's parsing
    matches actual production output, not a hand-crafted guess at its shape."""
    leads = [
        {"Email": "a@abc.com", "IntroSentAt": "2026-08-01 09:00:00", "IntroVariant": "A",
         "FollowUp1SentAt": "", "FollowUp1Variant": "", "SenderAccount": "sales1",
         "Status": "Stopped - Replied", "CurrentStage": "intro"},
        {"Email": "b@abc.com", "IntroSentAt": "2026-08-01 09:00:00", "IntroVariant": "B",
         "FollowUp1SentAt": "2026-08-05 09:00:00", "FollowUp1Variant": "B",
         "SenderAccount": "sales1", "Status": "", "CurrentStage": "followup1"},
    ]
    send_log = [
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"},
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-05 09:00:00"},
    ]
    responses = [{"Classification": outreach.CLASSIFICATION_GENUINE}]
    error_log = [{"ErrorType": "Send Failure", "Timestamp": "2026-08-01 09:00:00", "Message": "boom"}]
    return outreach.compute_campaign_dashboard(_campaign_cfg(), leads, responses, send_log, error_log)


def test_group_rows_by_section_groups_correctly():
    rows = [("A", "m1", "v1"), ("A", "m2", "v2"), ("B", "m3", "v3")]
    grouped = group_rows_by_section(rows)
    assert grouped["A"] == [("m1", "v1"), ("m2", "v2")]
    assert grouped["B"] == [("m3", "v3")]


def test_overview_summary_against_real_dashboard():
    rows = _real_dashboard_rows()
    summary = build_overview_summary(rows)
    assert summary["Total Leads (with Email)"] == "2"
    assert summary["Total Emails Sent"] == "2"
    assert summary["Genuine Replies"] == "1"


def test_per_stage_table_against_real_dashboard():
    rows = _real_dashboard_rows()
    table = build_per_stage_table(rows)
    by_stage = {r["stage"]: r["sent"] for r in table}
    assert by_stage["intro"] == "2"
    assert by_stage["followup1"] == "1"


def test_per_variant_table_against_real_dashboard():
    rows = _real_dashboard_rows()
    table = build_per_variant_table(rows)
    keys = {(r["stage"], r["variant"]) for r in table}
    assert ("intro", "A") in keys
    assert ("intro", "B") in keys
    assert ("followup1", "B") in keys

    intro_a = next(r for r in table if r["stage"] == "intro" and r["variant"] == "A")
    assert intro_a["sent"] == "1"
    assert intro_a["replies"] == "1"  # lead A replied while on intro


def test_sender_table_against_real_dashboard():
    rows = _real_dashboard_rows()
    table = build_sender_table(rows)
    assert len(table) == 1
    assert table[0]["account"] == "sales1"
    assert table[0]["sent"] == "2"


def test_error_summary_against_real_dashboard():
    rows = _real_dashboard_rows()
    summary = build_error_summary(rows)
    assert summary == [{"error_type": "Send Failure", "count": "1"}]


def test_empty_rows_all_functions_return_empty():
    assert build_overview_summary([]) == {}
    assert build_per_stage_table([]) == []
    assert build_per_variant_table([]) == []
    assert build_sender_table([]) == []
    assert build_error_summary([]) == []
