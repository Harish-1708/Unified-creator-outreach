"""Tests for edit_master_row.py's find_row and EDITABLE_FIELDS contract.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from edit_master_row import find_row, EDITABLE_FIELDS


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_find_row_locates_correct_row():
    records = [_row("a", "X"), _row("b", "X")]
    assert find_row(records, "b", "X") == 3


def test_find_row_compound_key_scoped_to_campaign():
    records = [_row("instagram:dudedad", "DudeRobe")]
    assert find_row(records, "instagram:dudedad", "SheRobe") is None


def test_dedup_key_is_not_an_editable_field():
    """The core safety invariant: dedup_key must never be editable through
    this script — it's the stable identifier every other stage matches
    on. Changing it here would silently break Shortlist sync, the
    bridge, and DM drafting's ability to find this creator again."""
    assert "dedup_key" not in EDITABLE_FIELDS


def test_editable_fields_are_exactly_the_intended_display_fields():
    assert set(EDITABLE_FIELDS) == {"contact_email", "username", "profile_link", "content_angle"}
