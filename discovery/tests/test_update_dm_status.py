"""Tests for update_dm_status.py — same compound-key invariant as
update_review_decision.py: the same creator under two different campaigns
must never be confused, since a DM outcome for one campaign says nothing
about the other.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from update_dm_status import find_target_row, DM_STATUS_OPTIONS


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_finds_the_correct_row_number():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_target_row(records, "b", "X") == 3


def test_returns_none_when_no_match():
    assert find_target_row([_row("a", "X")], "a", "Y") is None


def test_same_creator_two_campaigns_matches_only_the_correct_one():
    records = [_row("instagram:dudedad", "DudeRobe"), _row("instagram:dudedad", "SheRobe")]
    assert find_target_row(records, "instagram:dudedad", "DudeRobe") == 2
    assert find_target_row(records, "instagram:dudedad", "SheRobe") == 3


def test_status_vocabulary_matches_shortlist_pys_definition():
    """These two lists are deliberately duplicated (this repo's own
    self-contained-files philosophy) — this test is what keeps them from
    silently drifting apart."""
    import shortlist
    assert DM_STATUS_OPTIONS == shortlist.DM_STATUS_OPTIONS
