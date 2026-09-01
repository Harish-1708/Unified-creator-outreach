"""Regression test for a real bug found reviewing a live test run: a Deep
Research report candidate extracted as a display name (handle_type=
display_name, e.g. "james pieratt") was already correctly skipped for Meta
Business Discovery, but still fell through to tavily_follower_snippet(),
which built "https://www.instagram.com/james pieratt/" from the raw handle
and sent it to Tavily, which rejected it with a 400. A real handle can
never contain a space, so this is a cheap, deterministic guard — not a
parsing/extraction fix (that would mean touching the Claude-based report
extraction itself, out of scope here).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discover


def test_tavily_follower_snippet_skips_handles_with_spaces(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")

    def _fail_if_called(url, timeout=15):
        raise AssertionError(f"tavily_extract should never be called for a spaced handle, got url={url!r}")

    monkeypatch.setattr(discover, "tavily_extract", _fail_if_called)

    result = discover.tavily_follower_snippet("james pieratt", "instagram")
    assert result is None


def test_tavily_follower_snippet_still_works_for_a_real_handle(monkeypatch):
    """Guard against over-correcting: a normal handle must still go through."""
    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    calls = []

    def _record(url, timeout=15):
        calls.append(url)
        return "1,234 Followers"

    monkeypatch.setattr(discover, "tavily_extract", _record)

    discover.tavily_follower_snippet("realcreator", "instagram")
    assert calls == ["https://www.instagram.com/realcreator/"]
