import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from replies_logic import most_recent_responses


def test_most_recent_responses_sorts_newest_first():
    responses = [
        {"ResponseID": "1", "ReceivedAt": "2026-08-20 10:00:00"},
        {"ResponseID": "2", "ReceivedAt": "2026-08-25 09:00:00"},
        {"ResponseID": "3", "ReceivedAt": "2026-08-22 12:00:00"},
    ]
    result = most_recent_responses(responses)
    assert [r["ResponseID"] for r in result] == ["2", "3", "1"]


def test_most_recent_responses_respects_limit():
    responses = [{"ResponseID": str(i), "ReceivedAt": f"2026-08-{i:02d} 00:00:00"} for i in range(1, 11)]
    result = most_recent_responses(responses, limit=3)
    assert len(result) == 3
    assert [r["ResponseID"] for r in result] == ["10", "9", "8"]


def test_most_recent_responses_handles_blank_or_malformed_timestamp():
    responses = [
        {"ResponseID": "good", "ReceivedAt": "2026-08-20 10:00:00"},
        {"ResponseID": "blank", "ReceivedAt": ""},
        {"ResponseID": "garbage", "ReceivedAt": "not-a-date"},
    ]
    result = most_recent_responses(responses)  # must not raise
    assert result[0]["ResponseID"] == "good"


def test_most_recent_responses_does_not_mutate_input():
    responses = [
        {"ResponseID": "1", "ReceivedAt": "2026-08-20 10:00:00"},
        {"ResponseID": "2", "ReceivedAt": "2026-08-25 09:00:00"},
    ]
    original_order = [r["ResponseID"] for r in responses]
    most_recent_responses(responses)
    assert [r["ResponseID"] for r in responses] == original_order


def test_most_recent_responses_empty_list():
    assert most_recent_responses([]) == []
