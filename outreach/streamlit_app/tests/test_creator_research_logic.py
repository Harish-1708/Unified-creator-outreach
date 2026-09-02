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


def test_master_view_returns_every_row_including_unreviewed():
    """The bug this replaces: Main must show pending rows too, not just
    already-approved ones — that's the whole point of a review queue."""
    rows = [_master_row("a", review_status=""), _master_row("b", review_status="Approved")]
    assert crl.filter_creator_rows(rows, "Master") == rows


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


# ---------- lifecycle stage ----------

def _master(review_status="", outreach_channel=""):
    return {"review_status": review_status, "outreach_channel": outreach_channel}


def test_stage_discovered_when_no_review_status():
    assert crl.compute_lifecycle_stage(_master()) == "Discovered"
    assert crl.compute_lifecycle_stage(_master(review_status="Pending")) == "Discovered"


def test_stage_rejected():
    assert crl.compute_lifecycle_stage(_master(review_status="Rejected")) == "Rejected"


def test_stage_approved_no_channel_yet():
    assert crl.compute_lifecycle_stage(_master(review_status="Approved", outreach_channel="none")) == "Approved"
    assert crl.compute_lifecycle_stage(_master(review_status="Approved", outreach_channel="")) == "Approved"


def test_stage_email_not_yet_pushed():
    row = _master(review_status="Approved", outreach_channel="email")
    assert crl.compute_lifecycle_stage(row) == "Approved — Email (not yet synced/pushed)"


def test_stage_email_pushed():
    row = _master(review_status="Approved", outreach_channel="email")
    shortlist_row = {"campaign_push_status": "pushed"}
    assert crl.compute_lifecycle_stage(row, shortlist_row) == "Email — Pushed to Outreach"


def test_stage_email_push_failed():
    row = _master(review_status="Approved", outreach_channel="email")
    shortlist_row = {"campaign_push_status": "failed"}
    assert crl.compute_lifecycle_stage(row, shortlist_row) == "Email — Push Failed"


def test_stage_dm_draft_pending():
    row = _master(review_status="Approved", outreach_channel="dm")
    assert crl.compute_lifecycle_stage(row) == "Approved — DM (draft pending)"
    shortlist_row = {"dm_status": "pending_reasoning"}
    assert crl.compute_lifecycle_stage(row, shortlist_row) == "Approved — DM (draft pending)"


def test_stage_dm_status_reflects_real_status():
    row = _master(review_status="Approved", outreach_channel="dm")
    shortlist_row = {"dm_status": "Interested"}
    assert crl.compute_lifecycle_stage(row, shortlist_row) == "DM — Interested"


def test_stage_rejected_takes_priority_over_channel():
    """A row could theoretically have a stale outreach_channel from before
    being rejected — rejection must win regardless."""
    row = _master(review_status="Rejected", outreach_channel="email")
    assert crl.compute_lifecycle_stage(row) == "Rejected"


def test_index_shortlist_by_key_builds_correct_lookup():
    shortlist_records = [
        {"dedup_key": "a", "Campaign": "X", "dm_status": "Sent"},
        {"dedup_key": "a", "Campaign": "Y", "dm_status": "Replied"},
    ]
    index = crl.index_shortlist_by_key(shortlist_records)
    assert index[("a", "X")]["dm_status"] == "Sent"
    assert index[("a", "Y")]["dm_status"] == "Replied"


# ---------- brand/campaign registry wrappers ----------

def test_load_brand_registry_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(crl, "_DISCOVERY_DIR", str(tmp_path))
    assert crl.load_brand_registry() == []


def test_list_all_brands_combined_unions_run_log_and_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(crl, "_DISCOVERY_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "brands.yaml").write_text("- RegistryBrand\n")
    run_log = [_run("RunLogBrand", "SomeCampaign")]
    result = crl.list_all_brands_combined(run_log, {})
    assert result == ["RegistryBrand", "RunLogBrand"]


def test_build_add_campaign_commit_raises_on_conflict():
    import pytest
    existing = {"X": {"brand_name": "BrandA", "asana_sync": False}}
    with pytest.raises(ValueError):
        crl.build_add_campaign_commit(existing, "X", "BrandB")


def test_build_add_brand_commit_shape():
    commit = crl.build_add_brand_commit(["DudeRobe"], "SheRobe")
    assert commit["path"] == "discovery/config/brands.yaml"
    assert b"SheRobe" in commit["content"]
    assert "SheRobe" in commit["commit_message"]


# ---------- outreach campaign name sanitization ----------

def test_sanitize_replaces_spaces_with_underscores():
    assert crl.sanitize_to_outreach_campaign_name("DudeRobe Creator Discovery") == "DudeRobe_Creator_Discovery"


def test_sanitize_is_deterministic():
    """Same input must always produce the same output — this is the
    entire point: a discovery Campaign always maps to one specific
    outreach campaign name, never a different one on a later call."""
    a = crl.sanitize_to_outreach_campaign_name("DudeRobe Creator Discovery")
    b = crl.sanitize_to_outreach_campaign_name("DudeRobe Creator Discovery")
    assert a == b


def test_sanitize_result_matches_allowed_character_set():
    import re
    result = crl.sanitize_to_outreach_campaign_name("Dude-Robe's Campaign! (Q4/2026)")
    assert re.match(r"^[A-Za-z0-9_]+$", result)


def test_sanitize_collapses_multiple_special_chars_into_one_underscore():
    result = crl.sanitize_to_outreach_campaign_name("Dude   Robe")
    assert result == "Dude_Robe"


def test_sanitize_strips_leading_trailing_underscores():
    result = crl.sanitize_to_outreach_campaign_name("  DudeRobe  ")
    assert result == "DudeRobe"


def test_sanitize_never_returns_empty_string():
    """An edge case worth guarding: a Campaign name that's ALL special
    characters must still produce something valid, not an empty string
    that would fail campaign_builder's own 'name is required' check."""
    assert crl.sanitize_to_outreach_campaign_name("!!!") == "Campaign"


# ---------- curated columns ----------

def test_curate_row_keeps_only_curated_columns():
    row = {"dedup_key": "instagram:dudedad", "username": "dudedad", "platform": "instagram",
           "some_huge_evidence_field": "lots of text nobody wants glancing at",
           "recent_post_captions": "very long raw caption text"}
    curated = crl.curate_row(row)
    assert "dedup_key" in curated
    assert "username" in curated
    assert "some_huge_evidence_field" not in curated
    assert "recent_post_captions" not in curated


def test_curate_row_never_raises_for_missing_columns():
    """Master and Shortlist don't share an identical column set —
    dm_status only exists on Shortlist. A row missing a curated column
    entirely must not error, just omit it."""
    row = {"dedup_key": "instagram:dudedad"}
    curated = crl.curate_row(row)
    assert curated == {"dedup_key": "instagram:dudedad"}


def test_curate_row_never_invents_values():
    row = {"dedup_key": "instagram:dudedad", "platform": "instagram"}
    curated = crl.curate_row(row)
    assert "dm_status" not in curated  # not present on input, must not appear with a blank/None value
