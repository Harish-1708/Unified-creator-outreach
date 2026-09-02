"""Tests for promote_excluded_creator.py's pure logic. Two invariants
matter: the same compound-key matching everywhere else in this pipeline
(never confuse the same creator under two different campaigns), and never
promoting a creator that's already in Master (which would create a
duplicate rather than the intended move).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from promote_excluded_creator import find_row, build_row_for_header


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_find_row_locates_correct_row():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_row(records, "b", "X") == 3


def test_find_row_none_for_different_campaign():
    records = [_row("instagram:dudedad", "DudeRobe")]
    assert find_row(records, "instagram:dudedad", "SheRobe") is None


def test_build_row_for_header_matches_target_headers_not_source():
    """Excluded has rejection_reason, Master doesn't; Master has Niche
    columns, Excluded doesn't. The row must follow the TARGET (Master)
    header exactly, leaving anything the source lacks blank."""
    source = {"dedup_key": "instagram:dudedad", "platform": "instagram", "rejection_reason": "low fit"}
    master_header = ["dedup_key", "platform", "Niche 1", "Campaign"]
    row = build_row_for_header(source, master_header)
    assert row == ["instagram:dudedad", "instagram", "", ""]
