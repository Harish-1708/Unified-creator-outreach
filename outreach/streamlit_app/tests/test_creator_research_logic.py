"""Tests for creator_research_logic.py — pure logic, no Streamlit, no I/O.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import creator_research_logic as crl


# ---------- brand/campaign derivation from Run Log ----------

def _run(brand, campaign):
    return {"brand_name": brand, "campaign": campaign}


def test_list_brands_returns_distinct_sorted_names():
    run_log = [_run("DudeRobe", "A"), _run("SheRobe", "B"), _run("DudeRobe", "C")]
    assert crl.list_brands(run_log) == ["DudeRobe", "SheRobe"]


def test_list_campaigns_for_brand_scoped_correctly():
    run_log = [_run("DudeRobe", "DudeRobe Creator Discovery"), _run("SheRobe", "SheRobe Launch")]
    assert crl.list_campaigns_for_brand(run_log, "DudeRobe") == ["DudeRobe Creator Discovery"]
    assert crl.list_campaigns_for_brand(run_log, "SheRobe") == ["SheRobe Launch"]


def test_campaign_summary_sums_across_multiple_runs():
    run_log = [
        {"campaign": "DudeRobe Creator Discovery", "total_found": "52", "total_after_filters": "14"},
        {"campaign": "DudeRobe Creator Discovery", "total_found": "43", "total_after_filters": "14"},
        {"campaign": "Other Campaign", "total_found": "999", "total_after_filters": "999"},
    ]
    summary = crl.campaign_summary(run_log, "DudeRobe Creator Discovery")
    assert summary["run_count"] == 2
    assert summary["total_found"] == 95
    assert summary["total_after_filters"] == 28


def test_campaign_summary_tolerates_blank_or_non_numeric_fields():
    run_log = [{"campaign": "X", "total_found": "", "total_after_filters": "not_a_number"}]
    summary = crl.campaign_summary(run_log, "X")
    assert summary["total_found"] == 0
    assert summary["total_after_filters"] == 0


# ---------- Lead Data view filtering ----------

def _master_row(dedup_key, outreach_channel="", review_status="", dm_status=""):
    return {"dedup_key": dedup_key, "outreach_channel": outreach_channel,
            "review_status": review_status, "dm_status": dm_status}


def test_main_view_returns_every_row_including_unreviewed():
    """The bug this replaces: Main must show pending rows too, not just
    already-approved ones — that's the whole point of a review queue."""
    rows = [_master_row("a", review_status=""), _master_row("b", review_status="Approved")]
    assert crl.filter_creator_rows(rows, "Main") == rows


def test_shortlisted_view_filters_to_approved_only():
    rows = [_master_row("a", review_status="Approved"), _master_row("b", review_status="")]
    result = crl.filter_creator_rows(rows, "Shortlisted")
    assert {r["dedup_key"] for r in result} == {"a"}


def test_email_view_filters_to_email_channel_only():
    rows = [_master_row("a", outreach_channel="email"), _master_row("b", outreach_channel="dm")]
    result = crl.filter_creator_rows(rows, "Email")
    assert {r["dedup_key"] for r in result} == {"a"}


def test_dm_view_filters_to_dm_channel_only():
    rows = [_master_row("a", outreach_channel="email"), _master_row("b", outreach_channel="dm")]
    result = crl.filter_creator_rows(rows, "DM")
    assert {r["dedup_key"] for r in result} == {"b"}


def test_response_view_excludes_blank_and_pending_reasoning():
    rows = [
        _master_row("a", dm_status=""),
        _master_row("b", dm_status="pending_reasoning"),
        _master_row("c", dm_status="Sent"),
    ]
    result = crl.filter_creator_rows(rows, "Response")
    assert {r["dedup_key"] for r in result} == {"c"}


def test_final_view_only_rejected_for_now():
    rows = [_master_row("a", review_status="Rejected"), _master_row("b", review_status="Approved")]
    result = crl.filter_creator_rows(rows, "Final")
    assert {r["dedup_key"] for r in result} == {"a"}


def test_unknown_view_raises_clearly():
    import pytest
    with pytest.raises(ValueError):
        crl.filter_creator_rows([], "NotARealView")


# ---------- Asana settings wiring ----------

def test_get_asana_sync_status_defaults_off_for_unconfigured_campaign():
    assert crl.get_asana_sync_status({}, "Brand_New_Campaign") is False


def test_build_settings_commit_returns_correct_shape():
    commit = crl.build_settings_commit({}, "Kelson_Creators_Licensing", asana_sync=True)
    assert commit["path"] == "discovery/config/campaign_settings.yaml"
    assert b"asana_sync" in commit["content"]
    assert "Enable" in commit["commit_message"]
    assert "Kelson_Creators_Licensing" in commit["commit_message"]


def test_build_settings_commit_disable_wording():
    commit = crl.build_settings_commit({}, "X", asana_sync=False)
    assert "Disable" in commit["commit_message"]


def test_load_current_settings_missing_file_returns_empty_dict():
    """No campaign_settings.yaml committed yet is a legitimate, common
    starting state — must not raise."""
    assert crl.load_current_settings() == {}
