"""Tests for update_review_decision.py's find_target_row() — the
compound-key matching is the important invariant: the same account can
legitimately be a candidate under two different campaigns, each with its
own independent decision, so matching on dedup_key alone (ignoring
Campaign) would silently update the wrong campaign's row."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from update_review_decision import find_target_row


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_finds_the_correct_row_number():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_target_row(records, "b", "X") == 3  # row 1 is header, so record 0 -> row 2


def test_returns_none_when_no_match():
    records = [_row("a", "X")]
    assert find_target_row(records, "a", "Y") is None


def test_same_creator_two_campaigns_matches_only_the_correct_one():
    """The core invariant: same dedup_key under two campaigns must never
    be confused with each other."""
    records = [_row("instagram:dudedad", "DudeRobe"), _row("instagram:dudedad", "SheRobe")]
    assert find_target_row(records, "instagram:dudedad", "DudeRobe") == 2
    assert find_target_row(records, "instagram:dudedad", "SheRobe") == 3
