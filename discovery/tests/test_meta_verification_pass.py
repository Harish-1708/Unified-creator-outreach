"""Tests for verify_reported_followers_with_meta() — the bounded, late-stage
verification pass that upgrades follower_verification from "reported" (a
Deep Research report's unconfirmed number) to "verified" (Meta's own data)
for the small, final, RESULT_LIMIT-sized set of rows about to be written
to Master. Deliberately narrow in scope: it must NOT touch candidates that
already have real data, must NOT cascade to Serper/Tavily/Gemini on a
Meta miss, and must respect the existing circuit-breaker/rate-limit state
rather than duplicating that logic.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discover


def _candidate(handle, follower_verification="reported", platform="instagram", followers_count=100000):
    return {
        "handle": handle, "username": handle, "platform": platform,
        "follower_verification": follower_verification, "follower_source": "deep_research_report",
        "followers_count": followers_count, "data_confidence": "high",
    }


def _reset_business_discovery_state(monkeypatch):
    """The module's circuit-breaker state is shared global state across
    calls in the same run — reset it so tests don't leak into each other."""
    monkeypatch.setitem(discover._business_discovery_state, "disabled", False)
    monkeypatch.setitem(discover._business_discovery_state, "rate_limit_cooldown_until", 0)
    monkeypatch.setitem(discover._business_discovery_state, "consecutive_rate_limits", 0)


def test_skips_entirely_without_credentials():
    calls = []
    creators = [_candidate("dudedad")]
    discover.verify_reported_followers_with_meta(creators, None, None)
    assert calls == []
    assert creators[0]["follower_verification"] == "reported"  # untouched


def test_upgrades_to_verified_on_meta_success(monkeypatch):
    _reset_business_discovery_state(monkeypatch)
    monkeypatch.setattr(discover, "_call_business_discovery",
                         lambda handle, token, biz_id: ({"followers_count": 1495960, "media_count": 3117}, None))

    creators = [_candidate("dudedad", followers_count=1500000)]
    discover.verify_reported_followers_with_meta(creators, "fake-token", "fake-biz-id")

    c = creators[0]
    assert c["follower_verification"] == "verified"
    assert c["followers_count"] == 1495960  # Meta's real number, not the DR guess
    assert c["follower_source"] == "business_discovery_api"


def test_keeps_dr_data_on_meta_miss_no_cascade(monkeypatch):
    """The core design constraint: a Meta miss must NOT fall through to
    Serper/Tavily/Gemini — it must just leave the existing DR data alone."""
    _reset_business_discovery_state(monkeypatch)
    monkeypatch.setattr(discover, "_call_business_discovery",
                         lambda handle, token, biz_id: (None, ("ineligible_target", "Invalid user id", None)))

    def _fail_if_called(*a, **k):
        raise AssertionError("must not cascade to web_source_enrich on a Meta miss")

    monkeypatch.setattr(discover, "web_source_enrich", _fail_if_called)
    monkeypatch.setattr(discover, "serper_follower_snippet", _fail_if_called)
    monkeypatch.setattr(discover, "tavily_follower_snippet", _fail_if_called)

    creators = [_candidate("somepersonalaccount", followers_count=40000)]
    discover.verify_reported_followers_with_meta(creators, "fake-token", "fake-biz-id")

    c = creators[0]
    assert c["follower_verification"] == "reported"  # unchanged
    assert c["followers_count"] == 40000  # DR's number preserved


def test_never_touches_display_name_handles(monkeypatch):
    """A handle with a space can't be Meta-verified (same reasoning as the
    tavily_follower_snippet guard) — must not even attempt the call."""
    _reset_business_discovery_state(monkeypatch)

    def _fail_if_called(*a, **k):
        raise AssertionError("must not call Business Discovery for a display-name handle")

    monkeypatch.setattr(discover, "_call_business_discovery", _fail_if_called)

    creators = [_candidate("james pieratt")]
    discover.verify_reported_followers_with_meta(creators, "fake-token", "fake-biz-id")
    assert creators[0]["follower_verification"] == "reported"


def test_only_touches_reported_rows_not_already_verified_or_probable(monkeypatch):
    """Candidates that already came from Meta, Serper, or elsewhere must be
    left alone — this pass exists only to upgrade the specific 'reported'
    (Deep Research, unconfirmed) case."""
    _reset_business_discovery_state(monkeypatch)
    calls = []
    monkeypatch.setattr(discover, "_call_business_discovery",
                         lambda handle, token, biz_id: (calls.append(handle), ({"followers_count": 999}, None))[1])

    already_verified = _candidate("already_verified", follower_verification="verified")
    probable = _candidate("probable_source", follower_verification="probable")
    needs_verification = _candidate("needs_it", follower_verification="reported")
    creators = [already_verified, probable, needs_verification]

    discover.verify_reported_followers_with_meta(creators, "fake-token", "fake-biz-id")

    assert calls == ["needs_it"]
    assert already_verified["followers_count"] == 100000  # untouched
    assert probable["followers_count"] == 100000  # untouched


def test_respects_disabled_circuit_breaker(monkeypatch):
    """If Business Discovery was already disabled earlier in the same run
    (a real credential failure), this pass must not attempt any calls —
    it shares that state, not a separate check."""
    _reset_business_discovery_state(monkeypatch)
    monkeypatch.setitem(discover._business_discovery_state, "disabled", True)

    def _fail_if_called(*a, **k):
        raise AssertionError("must not call Business Discovery while the circuit breaker is tripped")

    monkeypatch.setattr(discover, "_call_business_discovery", _fail_if_called)

    creators = [_candidate("dudedad")]
    discover.verify_reported_followers_with_meta(creators, "fake-token", "fake-biz-id")
    assert creators[0]["follower_verification"] == "reported"
