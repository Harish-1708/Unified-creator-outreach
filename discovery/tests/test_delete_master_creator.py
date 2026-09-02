"""Tests for delete_master_creator.py's plan_deletions() — the pure
planning logic. Three invariants matter most: a creator not in Shortlist
is NOT a failure (most rows never get synced there), a creator not in
Master IS a failure (nothing to delete), and batch isolation (one bad key
never blocks the rest).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delete_master_creator import find_row, plan_deletions


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_find_row_locates_correct_row():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_row(records, "b", "X") == 3


def test_plan_deletions_ready_when_in_master_not_in_shortlist():
    """The common case: most Master rows never get synced to Shortlist —
    this must NOT be treated as a failure."""
    master = [_row("a", "X")]
    plan = plan_deletions(master, [], ["a"], "X")
    assert plan[0]["status"] == "ready"
    assert plan[0]["shortlist_row_num"] is None


def test_plan_deletions_ready_when_in_both():
    master = [_row("a", "X")]
    shortlist = [_row("a", "X")]
    plan = plan_deletions(master, shortlist, ["a"], "X")
    assert plan[0]["status"] == "ready"
    assert plan[0]["shortlist_row_num"] == 2


def test_plan_deletions_fails_when_not_in_master():
    """Not being in Master at all IS a real failure — there's nothing to
    delete, unlike the Shortlist case."""
    plan = plan_deletions([], [], ["ghost"], "X")
    assert plan[0]["status"] == "failed"


def test_plan_deletions_isolates_bad_key_from_good_ones():
    master = [_row("good", "X")]
    plan = plan_deletions(master, [], ["good", "ghost"], "X")
    by_key = {p["dedup_key"]: p for p in plan}
    assert by_key["good"]["status"] == "ready"
    assert by_key["ghost"]["status"] == "failed"


def test_plan_deletions_compound_key_scoped_to_campaign():
    """Same creator excluded/present under a DIFFERENT campaign must not
    be confused with the one actually requested for deletion."""
    master = [_row("a", "CampaignA")]
    plan = plan_deletions(master, [], ["a"], "CampaignB")
    assert plan[0]["status"] == "failed"


def test_plan_deletions_row_numbers_independent_between_sheets():
    """Master and Shortlist row numbers for the SAME creator can
    legitimately differ — each must be tracked and later deleted
    independently, never conflated."""
    master = [_row("a", "X"), _row("b", "X"), _row("c", "X")]
    shortlist = [_row("c", "X")]  # only 'c' made it to Shortlist, at a different row position
    plan = plan_deletions(master, shortlist, ["a", "c"], "X")
    by_key = {p["dedup_key"]: p for p in plan}
    assert by_key["a"]["master_row_num"] == 2
    assert by_key["a"]["shortlist_row_num"] is None
    assert by_key["c"]["master_row_num"] == 4
    assert by_key["c"]["shortlist_row_num"] == 2
