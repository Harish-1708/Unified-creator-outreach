import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conversation_logic import (
    build_outgoing_messages_for_lead, build_incoming_messages_for_responses,
    filter_responses_for_lead, build_conversation_thread,
)

_CAMPAIGN_CFG = {"_campaign_name": "Kelson_Creators_Licensing"}


def _lead(**overrides):
    lead = {"LeadID": "1", "FirstName": "Sam", "LastName": "Smith", "Email": "sam@abc.com",
            "CompanyName": "Acme"}
    lead.update(overrides)
    return lead


# ---------- build_outgoing_messages_for_lead ----------

def test_build_outgoing_messages_includes_sent_stage():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A")
    messages = build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead)
    assert len(messages) == 1
    assert messages[0]["direction"] == "outgoing"
    assert messages[0]["timestamp"] == "2026-08-01 09:00:00"
    assert "Sam" in messages[0]["body"]  # template variable actually rendered


def test_build_outgoing_messages_includes_multiple_sent_stages():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                 FollowUp1SentAt="2026-08-05 09:00:00", FollowUp1Variant="A")
    messages = build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead)
    assert len(messages) == 2
    assert messages[0]["timestamp"] == "2026-08-01 09:00:00"
    assert messages[1]["timestamp"] == "2026-08-05 09:00:00"


def test_build_outgoing_messages_skips_unsent_stages():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A")  # no FollowUp1SentAt
    messages = build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead)
    assert len(messages) == 1


def test_build_outgoing_messages_no_stages_sent_returns_empty():
    lead = _lead()
    assert build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead) == []


def test_build_outgoing_messages_skips_stage_with_sent_at_but_no_variant():
    """Defensive — a data inconsistency (SentAt set, Variant blank)
    shouldn't crash the whole conversation view, just skip that stage."""
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="")
    assert build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead) == []


def test_build_outgoing_messages_renders_actual_template_variables():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A", FirstName="Rocky")
    messages = build_outgoing_messages_for_lead(_CAMPAIGN_CFG, lead)
    assert "Rocky" in messages[0]["body"]
    assert "{{FirstName}}" not in messages[0]["body"]


def test_build_outgoing_messages_skips_stage_that_fails_to_render():
    """A nonexistent campaign (no template files at all) must not crash —
    every stage simply fails to render and gets skipped."""
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A")
    fake_campaign_cfg = {"_campaign_name": "NonexistentCampaignXYZ"}
    assert build_outgoing_messages_for_lead(fake_campaign_cfg, lead) == []


# ---------- build_incoming_messages_for_responses ----------

def test_build_incoming_messages_uses_full_body_when_available():
    responses = [{"ReceivedAt": "2026-08-02 10:00:00", "Subject": "Re: Hi", "From": "sam@abc.com",
                  "Snippet": "short preview", "FullBody": "the complete original message, much longer"}]
    messages = build_incoming_messages_for_responses(responses)
    assert messages[0]["body"] == "the complete original message, much longer"


def test_build_incoming_messages_falls_back_to_snippet_when_full_body_missing():
    """Responses logged before FullBody existed as a column — must still
    show something, just the shorter historical Snippet."""
    responses = [{"ReceivedAt": "2026-08-02 10:00:00", "Subject": "Re: Hi", "From": "sam@abc.com",
                  "Snippet": "short preview", "FullBody": ""}]
    messages = build_incoming_messages_for_responses(responses)
    assert messages[0]["body"] == "short preview"


def test_build_incoming_messages_empty_list():
    assert build_incoming_messages_for_responses([]) == []


def test_build_incoming_messages_preserves_from_and_subject():
    responses = [{"ReceivedAt": "2026-08-02 10:00:00", "Subject": "Re: Pricing", "From": "sam@abc.com",
                  "Snippet": "x", "FullBody": "x"}]
    messages = build_incoming_messages_for_responses(responses)
    assert messages[0]["from"] == "sam@abc.com"
    assert messages[0]["subject"] == "Re: Pricing"


# ---------- filter_responses_for_lead ----------

def test_filter_responses_for_lead_matches_by_lead_id():
    responses = [{"LeadID": "1"}, {"LeadID": "2"}, {"LeadID": "1"}]
    filtered = filter_responses_for_lead(responses, "1")
    assert len(filtered) == 2


def test_filter_responses_for_lead_handles_str_int_mismatch():
    responses = [{"LeadID": 1}, {"LeadID": 2}]  # int, not str
    filtered = filter_responses_for_lead(responses, "1")
    assert len(filtered) == 1


def test_filter_responses_for_lead_no_match_returns_empty():
    responses = [{"LeadID": "1"}]
    assert filter_responses_for_lead(responses, "999") == []


def test_filter_responses_for_lead_empty_list():
    assert filter_responses_for_lead([], "1") == []


# ---------- build_conversation_thread ----------

def test_build_conversation_thread_merges_outgoing_and_incoming_chronologically():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                 FollowUp1SentAt="2026-08-10 09:00:00", FollowUp1Variant="A")
    responses = [{"LeadID": "1", "ReceivedAt": "2026-08-05 10:00:00", "Subject": "Re: Hi",
                  "From": "sam@abc.com", "Snippet": "interested", "FullBody": "I'm interested, tell me more"}]
    thread = build_conversation_thread(_CAMPAIGN_CFG, lead, responses)

    assert len(thread) == 3
    assert thread[0]["direction"] == "outgoing"  # Aug 1
    assert thread[1]["direction"] == "incoming"  # Aug 5 — reply came between the two outgoing stages
    assert thread[2]["direction"] == "outgoing"  # Aug 10


def test_build_conversation_thread_no_activity_returns_empty():
    lead = _lead()
    assert build_conversation_thread(_CAMPAIGN_CFG, lead, []) == []


def test_build_conversation_thread_only_outgoing_no_replies_yet():
    lead = _lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A")
    thread = build_conversation_thread(_CAMPAIGN_CFG, lead, [])
    assert len(thread) == 1
    assert thread[0]["direction"] == "outgoing"


def test_build_conversation_thread_only_incoming_no_outgoing_recorded():
    """Edge case — a reply logged for a lead whose own SentAt/Variant
    somehow isn't populated (e.g. imported mid-sequence) still shows the
    reply, just without a matching outgoing entry."""
    lead = _lead()
    responses = [{"LeadID": "1", "ReceivedAt": "2026-08-05 10:00:00", "Subject": "Re: Hi",
                  "From": "sam@abc.com", "Snippet": "x", "FullBody": "x"}]
    thread = build_conversation_thread(_CAMPAIGN_CFG, lead, responses)
    assert len(thread) == 1
    assert thread[0]["direction"] == "incoming"
