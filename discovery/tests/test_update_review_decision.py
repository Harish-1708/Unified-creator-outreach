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


from update_review_decision import apply_decision_to_one  # noqa: E402


class FakeMasterWorksheet:
    def __init__(self, header, rows):
        self.header = header
        self._rows = rows
        self.cells = {}

    def update_cell(self, row_num, col, value):
        self.cells[(row_num, col)] = value


HEADER = ["dedup_key", "Campaign", "review_status", "outreach_channel"]


def test_apply_decision_saves_correct_row():
    ws = FakeMasterWorksheet(HEADER, [_row("a", "X")])
    records = [_row("a", "X")]
    result = apply_decision_to_one(ws, HEADER, records, "a", "X", "Approved", "email")
    assert result["status"] == "saved"
    assert ws.cells[(2, 3)] == "Approved"  # review_status is column 3
    assert ws.cells[(2, 4)] == "email"     # outreach_channel is column 4


def test_apply_decision_missing_row_reports_failed_not_raise():
    ws = FakeMasterWorksheet(HEADER, [])
    result = apply_decision_to_one(ws, HEADER, [], "ghost", "X", "Approved", "email")
    assert result["status"] == "failed"
    assert "No Master row found" in result["error"]


def test_batch_isolation_one_bad_key_does_not_block_the_rest():
    """The core invariant of the bulk extension: a batch of
    [valid, invalid, valid] must save both valid ones and report the
    invalid one as failed — never stop partway through."""
    ws = FakeMasterWorksheet(HEADER, [_row("good1", "X"), _row("good2", "X")])
    records = [_row("good1", "X"), _row("good2", "X")]

    results = [
        apply_decision_to_one(ws, HEADER, records, key, "X", "Approved", "email")
        for key in ["good1", "does_not_exist", "good2"]
    ]

    statuses = [r["status"] for r in results]
    assert statuses == ["saved", "failed", "saved"]
    # Both real rows actually got written, not just the first one before
    # the bad key was hit.
    assert ws.cells[(2, 3)] == "Approved"
    assert ws.cells[(3, 3)] == "Approved"
