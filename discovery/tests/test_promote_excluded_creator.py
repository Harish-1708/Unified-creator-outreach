"""Tests for promote_excluded_creator.py. Three invariants matter:
compound-key matching (same as everywhere else), batch isolation (one
bad key never blocks the rest), and correct row-building against the
target header — not source order.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from promote_excluded_creator import find_row, build_row_for_header, plan_promotions


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_find_row_locates_correct_row():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_row(records, "b", "X") == 3


def test_find_row_none_for_different_campaign():
    records = [_row("instagram:dudedad", "DudeRobe")]
    assert find_row(records, "instagram:dudedad", "SheRobe") is None


def test_build_row_for_header_matches_target_headers_not_source():
    source = {"dedup_key": "instagram:dudedad", "platform": "instagram", "rejection_reason": "low fit"}
    master_header = ["dedup_key", "platform", "Niche 1", "Campaign"]
    row = build_row_for_header(source, master_header)
    assert row == ["instagram:dudedad", "instagram", "", ""]


# ---------- plan_promotions: batch isolation ----------

def test_plan_promotions_all_valid():
    excluded = [_row("a", "X"), _row("b", "X")]
    plan = plan_promotions(excluded, [], ["a", "b"], "X")
    assert all(p["status"] == "ready" for p in plan)


def test_plan_promotions_isolates_missing_key():
    excluded = [_row("a", "X")]
    plan = plan_promotions(excluded, [], ["a", "ghost"], "X")
    by_key = {p["dedup_key"]: p for p in plan}
    assert by_key["a"]["status"] == "ready"
    assert by_key["ghost"]["status"] == "failed"


def test_plan_promotions_rejects_already_in_master():
    excluded = [_row("a", "X")]
    master = [_row("a", "X")]
    plan = plan_promotions(excluded, master, ["a"], "X")
    assert plan[0]["status"] == "failed"
    assert "already exists in Master" in plan[0]["error"]


def test_plan_promotions_compound_key_scoped_to_campaign():
    """Same creator excluded under a DIFFERENT campaign must not block a
    promotion for the requested campaign."""
    excluded = [_row("a", "CampaignB")]
    master = [_row("a", "CampaignA")]
    plan = plan_promotions(excluded, master, ["a"], "CampaignB")
    assert plan[0]["status"] == "ready"


def test_plan_promotions_ready_items_carry_correct_row_numbers_for_reverse_deletion():
    """The actual correctness property main() depends on: row numbers
    must reflect the ORIGINAL excluded_records snapshot regardless of
    request order, so deletions (sorted descending afterward) hit the
    right rows."""
    excluded = [_row("a", "X"), _row("b", "X"), _row("c", "X")]
    plan = plan_promotions(excluded, [], ["c", "a"], "X")
    by_key = {p["dedup_key"]: p for p in plan}
    assert by_key["a"]["excluded_row_num"] == 2
    assert by_key["c"]["excluded_row_num"] == 4
