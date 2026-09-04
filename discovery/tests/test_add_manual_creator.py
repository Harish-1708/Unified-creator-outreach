"""Tests for add_manual_creator.py. The core invariant: a creator that
already exists under a given Campaign must never be silently duplicated —
that's the entire safety property this script exists to guarantee (a
manual add is a human action, and a human accidentally clicking twice
must not create two rows for the same creator).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from add_manual_creator import build_manual_creator_dict, find_existing_row, build_row_for_header


def test_dedup_key_follows_platform_colon_username_convention():
    creator = build_manual_creator_dict("Instagram", "DudeDad", "DudeRobe Creator Discovery")
    assert creator["dedup_key"] == "instagram:dudedad"


def test_profile_link_defaults_when_not_given():
    creator = build_manual_creator_dict("instagram", "dudedad", "X")
    assert creator["profile_link"] == "https://instagram.com/dudedad"


def test_profile_link_uses_given_value_when_provided():
    creator = build_manual_creator_dict("instagram", "dudedad", "X",
                                         profile_link="https://instagram.com/dudedad/?hl=en")
    assert creator["profile_link"] == "https://instagram.com/dudedad/?hl=en"


def test_data_source_marks_manual_add():
    creator = build_manual_creator_dict("instagram", "dudedad", "X")
    assert creator["data_source"] == "manual_add"
    assert creator["discovery_method"] == "manual_add"


def _row(dedup_key, campaign):
    return {"dedup_key": dedup_key, "Campaign": campaign}


def test_find_existing_row_detects_duplicate():
    records = [_row("instagram:dudedad", "DudeRobe")]
    assert find_existing_row(records, "instagram:dudedad", "DudeRobe") == 2


def test_find_existing_row_none_when_different_campaign():
    """The same compound-key invariant as everywhere else in this
    pipeline: the same creator under a DIFFERENT campaign is not a
    duplicate — both should be allowed to exist independently."""
    records = [_row("instagram:dudedad", "DudeRobe")]
    assert find_existing_row(records, "instagram:dudedad", "SheRobe") is None


def test_find_existing_row_none_when_no_match():
    assert find_existing_row([], "instagram:dudedad", "DudeRobe") is None


def test_build_row_for_header_matches_real_header_order_not_a_hardcoded_list():
    """The row must follow whatever the ACTUAL sheet header is — including
    columns this script knows nothing about, which must come out blank,
    not omitted (which would misalign every column after them)."""
    creator = build_manual_creator_dict("instagram", "dudedad", "DudeRobe")
    header = ["dedup_key", "platform", "some_future_column_this_script_has_never_heard_of", "Campaign"]
    row = build_row_for_header(creator, header)
    assert row == ["instagram:dudedad", "instagram", "", "DudeRobe"]


# ---------- custom_fields ----------

def test_build_manual_creator_dict_merges_custom_fields():
    creator = build_manual_creator_dict("instagram", "dudedad", "X", custom_fields={"Client": "DudeRobe"})
    assert creator["Client"] == "DudeRobe"


def test_build_manual_creator_dict_without_custom_fields_unaffected():
    creator = build_manual_creator_dict("instagram", "dudedad", "X")
    assert "Client" not in creator


def test_build_manual_creator_dict_fixed_fields_always_win_collision():
    """The real safety property: a custom field can never silently
    override dedup_key, Campaign, or any other field this function
    itself computes — even if someone (accidentally or otherwise) sends
    a custom field with a colliding name."""
    creator = build_manual_creator_dict("instagram", "dudedad", "RealCampaign",
                                         custom_fields={"Campaign": "FAKE_OVERRIDE",
                                                         "dedup_key": "tampered:value"})
    assert creator["Campaign"] == "RealCampaign"
    assert creator["dedup_key"] == "instagram:dudedad"
