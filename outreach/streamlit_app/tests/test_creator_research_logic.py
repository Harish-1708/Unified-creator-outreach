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


def test_curate_row_places_contact_email_near_username_not_at_the_end():
    row = {"dedup_key": "instagram:dudedad", "username": "dudedad", "platform": "instagram",
           "content_angle": "some angle", "contact_email": "dudedad@example.com"}
    curated = crl.curate_row(row)
    keys_in_order = list(curated.keys())
    assert keys_in_order.index("contact_email") < keys_in_order.index("platform")
    assert keys_in_order.index("contact_email") == keys_in_order.index("username") + 1


# ---------- reorder_priority_columns ----------

def test_reorder_moves_priority_columns_to_front_in_given_order():
    row = {"platform": "instagram", "dedup_key": "instagram:dudedad", "content_angle": "x",
           "username": "dudedad", "contact_email": "d@example.com"}
    reordered = crl.reorder_priority_columns(row, ["dedup_key", "username", "contact_email"])
    keys = list(reordered.keys())
    assert keys[:3] == ["dedup_key", "username", "contact_email"]


def test_reorder_preserves_every_value_nothing_lost():
    row = {"platform": "instagram", "dedup_key": "a", "username": "b", "contact_email": "c", "score": 8}
    reordered = crl.reorder_priority_columns(row, ["dedup_key", "username", "contact_email"])
    assert reordered == {"dedup_key": "a", "username": "b", "contact_email": "c",
                          "platform": "instagram", "score": 8}


def test_reorder_handles_missing_priority_column_gracefully():
    row = {"platform": "instagram", "username": "b"}  # no dedup_key, no contact_email
    reordered = crl.reorder_priority_columns(row, ["dedup_key", "username", "contact_email"])
    assert reordered == {"username": "b", "platform": "instagram"}


# ---------- campaign analytics ----------

def _run_log_row(brand, campaign, found="10", after_filters="3"):
    return {"brand_name": brand, "campaign": campaign, "total_found": found, "total_after_filters": after_filters}


def _analytics_master_row(campaign, review_status="", channel=""):
    return {"Campaign": campaign, "review_status": review_status, "outreach_channel": channel}


def test_build_campaign_analytics_one_row_per_campaign():
    run_log = [_run_log_row("DudeRobe", "DudeRobe Creator Discovery"),
               _run_log_row("SheRobe", "SheRobe Launch")]
    result = crl.build_campaign_analytics(run_log, [])
    campaigns = {r["Campaign"] for r in result}
    assert campaigns == {"DudeRobe Creator Discovery", "SheRobe Launch"}


def test_build_campaign_analytics_sums_multiple_runs_for_same_campaign():
    run_log = [_run_log_row("DudeRobe", "X", found="10", after_filters="3"),
               _run_log_row("DudeRobe", "X", found="20", after_filters="5")]
    result = crl.build_campaign_analytics(run_log, [])
    assert result[0]["Runs"] == 2
    assert result[0]["Found (all runs)"] == 30
    assert result[0]["Written to Master"] == 8


def test_build_campaign_analytics_counts_review_status_and_channel_from_master():
    run_log = [_run_log_row("DudeRobe", "X")]
    master = [
        _analytics_master_row("X", review_status="Approved", channel="email"),
        _analytics_master_row("X", review_status="Approved", channel="dm"),
        _analytics_master_row("X", review_status="Rejected"),
        _analytics_master_row("X", review_status=""),
    ]
    result = crl.build_campaign_analytics(run_log, master)
    row = result[0]
    assert row["In Master now"] == 4
    assert row["Approved"] == 2
    assert row["Rejected"] == 1
    assert row["Pending"] == 1
    assert row["Email"] == 1
    assert row["DM"] == 1


def test_build_campaign_analytics_master_rows_scoped_to_correct_campaign():
    """Master rows from a DIFFERENT campaign must never bleed into this
    one's counts — the same compound-key discipline as everywhere else."""
    run_log = [_run_log_row("DudeRobe", "CampaignA")]
    master = [_analytics_master_row("CampaignA", review_status="Approved"),
              _analytics_master_row("CampaignB", review_status="Approved")]
    result = crl.build_campaign_analytics(run_log, master)
    assert result[0]["In Master now"] == 1


def test_build_campaign_analytics_zero_master_rows_still_produces_a_row():
    """A campaign with real Run Log history but no Master rows yet (found
    nothing, or still running) should show zeros, not disappear."""
    run_log = [_run_log_row("DudeRobe", "X")]
    result = crl.build_campaign_analytics(run_log, [])
    assert result[0]["In Master now"] == 0


def test_build_analytics_totals_sums_across_campaigns():
    campaign_rows = [
        {"Brand": "DudeRobe", "Campaign": "A", "Runs": 1, "In Master now": 5, "Approved": 2,
         "Rejected": 1, "Pending": 2, "Email": 1, "DM": 1},
        {"Brand": "SheRobe", "Campaign": "B", "Runs": 2, "In Master now": 3, "Approved": 1,
         "Rejected": 0, "Pending": 2, "Email": 1, "DM": 0},
    ]
    totals = crl.build_analytics_totals(campaign_rows)
    assert totals["brands"] == 2
    assert totals["campaigns"] == 2
    assert totals["runs"] == 3
    assert totals["in_master"] == 8
    assert totals["approved"] == 3


# ---------- single-campaign clean table ----------

def _table_master_row(review_status="", channel="", dm_status=""):
    return {"review_status": review_status, "outreach_channel": channel, "dm_status": dm_status}


def test_build_single_campaign_table_order_and_labels():
    result = crl.build_single_campaign_table([], [], [])
    labels = [row["Metric"] for row in result]
    assert labels == ["Total Found (all runs)", "Total Master", "Total Excluded", "Total Shortlisted",
                       "Total Email", "Total DM", "Total Response", "Total Final"]


def test_build_single_campaign_table_sums_found_across_runs():
    run_log = [{"total_found": "10"}, {"total_found": "20"}]
    result = crl.build_single_campaign_table(run_log, [], [])
    by_metric = {r["Metric"]: r["Count"] for r in result}
    assert by_metric["Total Found (all runs)"] == 30


def test_build_single_campaign_table_master_and_excluded_are_plain_counts():
    master = [_table_master_row(), _table_master_row()]
    excluded = [{}]
    result = crl.build_single_campaign_table([], master, excluded)
    by_metric = {r["Metric"]: r["Count"] for r in result}
    assert by_metric["Total Master"] == 2
    assert by_metric["Total Excluded"] == 1


def test_build_single_campaign_table_matches_filter_creator_rows_exactly():
    """The core guarantee: these counts must be IDENTICAL to what
    filter_creator_rows itself returns for the Data tab — never a
    separately-computed number that could disagree with what a human
    sees by actually opening that sub-tab."""
    master = [
        _table_master_row(review_status="Approved", channel="email"),
        _table_master_row(review_status="Approved", channel="dm"),
        _table_master_row(review_status="Rejected"),
        _table_master_row(dm_status="Replied"),
    ]
    result = crl.build_single_campaign_table([], master, [])
    by_metric = {r["Metric"]: r["Count"] for r in result}
    assert by_metric["Total Shortlisted"] == len(crl.filter_creator_rows(master, "Shortlisted"))
    assert by_metric["Total Email"] == len(crl.filter_creator_rows(master, "Email"))
    assert by_metric["Total DM"] == len(crl.filter_creator_rows(master, "DM"))
    assert by_metric["Total Response"] == len(crl.filter_creator_rows(master, "Response"))
    assert by_metric["Total Final"] == len(crl.filter_creator_rows(master, "Final"))


# ---------- search_creator_rows / filter_creator_rows_by_review_status ----------

def test_search_creator_rows_matches_any_field_case_insensitive():
    rows = [{"username": "DudeDad", "contact_email": "a@abc.com"}, {"username": "other", "contact_email": "b@abc.com"}]
    result = crl.search_creator_rows(rows, "dudedad")
    assert len(result) == 1
    assert result[0]["username"] == "DudeDad"


def test_search_creator_rows_matches_partial_email():
    rows = [{"username": "a", "contact_email": "sam@example.com"}, {"username": "b", "contact_email": "other@x.com"}]
    result = crl.search_creator_rows(rows, "sam@")
    assert len(result) == 1


def test_search_creator_rows_blank_query_returns_everything_unchanged():
    rows = [{"username": "a"}, {"username": "b"}]
    assert crl.search_creator_rows(rows, "") == rows
    assert crl.search_creator_rows(rows, "   ") == rows


def test_search_creator_rows_no_match_returns_empty():
    rows = [{"username": "a"}]
    assert crl.search_creator_rows(rows, "zzz_no_match") == []


def test_filter_by_review_status_all_returns_everything():
    rows = [{"review_status": "Approved"}, {"review_status": "Rejected"}]
    assert crl.filter_creator_rows_by_review_status(rows, "All") == rows


def test_filter_by_review_status_matches_case_insensitively():
    rows = [{"review_status": "Approved"}, {"review_status": "Rejected"}]
    result = crl.filter_creator_rows_by_review_status(rows, "approved")
    assert result == [{"review_status": "Approved"}]


def test_filter_by_review_status_blank_status_treated_as_pending():
    rows = [{"review_status": ""}, {"review_status": "Approved"}]
    result = crl.filter_creator_rows_by_review_status(rows, "Pending")
    assert result == [{"review_status": ""}]
