"""Tests for run_push.py's select_eligible_rows() — the idempotency
guarantee lives here: a row already marked "pushed" must never be
re-selected, which is what actually prevents a re-run from double-pushing
the same creator (push_creators_to_outreach itself doesn't re-check this;
it pushes whatever list it's handed)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "outreach"))

from run_push import select_eligible_rows


def _row(dedup_key, outreach_channel="email", campaign_push_status=""):
    return {"dedup_key": dedup_key, "outreach_channel": outreach_channel,
            "campaign_push_status": campaign_push_status}


def test_selects_email_channel_pending_rows():
    records = [_row("a"), _row("b")]
    eligible = select_eligible_rows(records)
    assert {r["dedup_key"] for r in eligible} == {"a", "b"}


def test_excludes_already_pushed_rows():
    """The idempotency guarantee: a "pushed" row must never come back."""
    records = [_row("a", campaign_push_status="pushed"), _row("b")]
    eligible = select_eligible_rows(records)
    assert {r["dedup_key"] for r in eligible} == {"b"}


def test_includes_previously_failed_rows_for_retry():
    records = [_row("a", campaign_push_status="failed")]
    eligible = select_eligible_rows(records)
    assert {r["dedup_key"] for r in eligible} == {"a"}


def test_excludes_dm_and_none_channels():
    records = [_row("a", outreach_channel="dm"), _row("b", outreach_channel="none"),
               _row("c", outreach_channel="email")]
    eligible = select_eligible_rows(records)
    assert {r["dedup_key"] for r in eligible} == {"c"}


def test_creator_keys_filter_restricts_selection():
    records = [_row("a"), _row("b"), _row("c")]
    eligible = select_eligible_rows(records, creator_keys={"a", "c"})
    assert {r["dedup_key"] for r in eligible} == {"a", "c"}


def test_row_numbers_reconstructed_correctly():
    """Row 1 is the header — the first record must map to sheet row 2."""
    records = [_row("a"), _row("b")]
    eligible = select_eligible_rows(records)
    rows_by_key = {r["dedup_key"]: r["_row"] for r in eligible}
    assert rows_by_key == {"a": 2, "b": 3}
