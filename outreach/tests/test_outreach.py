"""Unit tests for outreach.py (SMTP/IMAP edition)."""

import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import outreach  # noqa: E402


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates", "Kelson_Creators_Licensing")

STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
]
FMT = "%Y-%m-%d %H:%M:%S"


def make_lead(**overrides):
    lead = {
        "_row": 2, "Approval": "Yes", "Status": "", "ReplyStatus": "",
        "Email": "john@abc.com",
        "IntroSentAt": "", "IntroVariant": "",
        "FollowUp1SentAt": "", "FollowUp1Variant": "",
        "MessageID": "", "ThreadReferences": "", "SenderAccount": "",
        # Non-blank by default so tests that aren't specifically about the
        # blank-subject continuation feature don't break if a real sample
        # template (e.g. templates/Kelson_Creators_Licensing/followup1_*)
        # is later edited to use a blank Subject — a legitimate, supported
        # thing to do, and general-purpose tests shouldn't be coupled to
        # that content staying non-blank. Tests that specifically exercise
        # the blank-subject path already override this to "" explicitly.
        "ThreadSubject": "Default Test Thread Subject",
    }
    lead.update(overrides)
    return lead


# =============================================================================
# classify_message (unchanged logic, transport-independent)
# =============================================================================

def test_genuine_reply():
    result = outreach.classify_message({}, "Re: Quick idea", "Sure, let's talk next week.", "john@abc.com")
    assert result == outreach.CLASSIFICATION_GENUINE


def test_auto_submitted_header():
    result = outreach.classify_message({"auto-submitted": "auto-replied"}, "Away", "I'm away", "john@abc.com")
    assert result == outreach.CLASSIFICATION_AUTOREPLY


def test_ooo_keyword_fallback():
    result = outreach.classify_message({}, "Out of Office", "I am out of office until Monday.", "john@abc.com")
    assert result == outreach.CLASSIFICATION_OOO


def test_hard_bounce_status_code():
    result = outreach.classify_message(
        {"content-type": "multipart/report; report-type=delivery-status"},
        "Delivery Status Notification (Failure)",
        "550 5.1.1 The email account does not exist.",
        "mailer-daemon@abc.com",
    )
    assert result == outreach.CLASSIFICATION_BOUNCE_HARD


def test_soft_bounce_status_code():
    result = outreach.classify_message(
        {"content-type": "multipart/report; report-type=delivery-status"},
        "Delivery delayed",
        "451 4.2.1 mailbox temporarily full",
        "mailer-daemon@abc.com",
    )
    assert result == outreach.CLASSIFICATION_BOUNCE_SOFT


def test_precedence_bulk():
    result = outreach.classify_message({"precedence": "bulk"}, "Newsletter", "content", "list@abc.com")
    assert result == outreach.CLASSIFICATION_AUTOREPLY


def test_bounce_sender_without_status_code_defaults_hard():
    result = outreach.classify_message({}, "Mail delivery failed", "delivery has failed", "mailer-daemon@abc.com")
    assert result == outreach.CLASSIFICATION_BOUNCE_HARD


# =============================================================================
# pick_variant
# =============================================================================

def test_picks_least_used_variant():
    leads = [{"IntroVariant": "A"}, {"IntroVariant": "A"}, {"IntroVariant": "B"}, {"IntroVariant": ""}]
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"])
    assert variant in ("C", "D")


def test_respects_in_batch_counts():
    leads = [{"IntroVariant": ""} for _ in range(4)]
    batch_counts = {"A": 5, "B": 0, "C": 0, "D": 0}
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"], batch_counts)
    assert variant in ("B", "C", "D")


def test_variant_selection_stays_balanced_over_many_picks():
    variants = ["A", "B", "C", "D"]
    leads = []
    for _ in range(40):
        v = outreach.pick_variant(leads, "IntroVariant", variants)
        leads.append({"IntroVariant": v})
    counts = {v: sum(1 for l in leads if l["IntroVariant"] == v) for v in variants}
    assert max(counts.values()) - min(counts.values()) <= 1


# =============================================================================
# get_eligible_leads — Email is the only mandatory field
# =============================================================================

def test_intro_stage_new_lead_is_eligible():
    eligible = outreach.get_eligible_leads([make_lead()], STAGES, 0)
    assert len(eligible) == 1


def test_lead_without_email_is_never_eligible():
    eligible = outreach.get_eligible_leads([make_lead(Email="")], STAGES, 0)
    assert len(eligible) == 0


def test_lead_with_only_email_filled_is_eligible():
    # FirstName, LastName, Company all blank — only Email present.
    lead = make_lead(FirstName="", LastName="", Company="")
    eligible = outreach.get_eligible_leads([lead], STAGES, 0)
    assert len(eligible) == 1


def test_intro_stage_already_sent_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(IntroSentAt="2026-08-01 10:00:00")], STAGES, 0)
    assert len(eligible) == 0


def test_pending_approval_no_longer_excluded():
    """Approval was removed as an eligibility gate entirely — a lead with
    Approval='Pending' but a valid email is now just as eligible as any
    other lead. Approval stays a real column, purely informational."""
    eligible = outreach.get_eligible_leads([make_lead(Approval="Pending")], STAGES, 0)
    assert len(eligible) == 1


def test_blank_approval_no_longer_excluded():
    eligible = outreach.get_eligible_leads([make_lead(Approval="")], STAGES, 0)
    assert len(eligible) == 1


def test_followup1_requires_intro_sent_and_wait_period():
    recent = datetime.now().strftime(FMT)
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    not_yet_waited = make_lead(IntroSentAt=recent)
    waited_enough = make_lead(IntroSentAt=old)
    never_sent_intro = make_lead(IntroSentAt="")
    eligible = outreach.get_eligible_leads([not_yet_waited, waited_enough, never_sent_intro], STAGES, 1)
    assert eligible == [waited_enough]


def test_replied_lead_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(ReplyStatus="Replied")], STAGES, 0)
    assert len(eligible) == 0


def test_stopped_status_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(Status="Stopped - Bounced")], STAGES, 0)
    assert len(eligible) == 0


# =============================================================================
# get_eligible_leads / find_duplicate_email_leads — same email, two rows
# =============================================================================

def test_duplicate_email_rows_only_first_row_is_eligible():
    row1 = make_lead(_row=2, LeadID="L1", Email="same@abc.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="same@abc.com")
    eligible = outreach.get_eligible_leads([row1, row2], STAGES, 0)
    assert len(eligible) == 1
    assert eligible[0]["LeadID"] == "L1"  # the lower row number wins


def test_duplicate_email_rows_email_matching_is_case_insensitive():
    row1 = make_lead(_row=2, LeadID="L1", Email="Same@ABC.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="same@abc.com")
    eligible = outreach.get_eligible_leads([row1, row2], STAGES, 0)
    assert len(eligible) == 1
    assert eligible[0]["LeadID"] == "L1"


def test_duplicate_email_rows_does_not_affect_leads_with_unique_emails():
    row1 = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="b@abc.com")
    eligible = outreach.get_eligible_leads([row1, row2], STAGES, 0)
    assert len(eligible) == 2


def test_duplicate_email_rows_dedup_only_applies_among_leads_that_pass_other_checks():
    # Row1 fails on missing email (Approval no longer gates eligibility
    # at all, so that can't be used to demonstrate this anymore) and is
    # filtered out BEFORE the dedup step — it never "claims" the email
    # slot, so row2 (which does pass) is correctly still eligible. Dedup
    # only applies among leads that already passed every other check,
    # not globally by row order.
    row1 = make_lead(_row=2, LeadID="L1", Email="")
    row2 = make_lead(_row=3, LeadID="L2", Email="same@abc.com")
    eligible = outreach.get_eligible_leads([row1, row2], STAGES, 0)
    assert len(eligible) == 1
    assert eligible[0]["LeadID"] == "L2"


def test_find_duplicate_email_leads_finds_repeated_emails():
    row1 = make_lead(_row=2, LeadID="L1", Email="same@abc.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="same@abc.com")
    unique = make_lead(_row=4, LeadID="L3", Email="unique@abc.com")

    duplicates = outreach.find_duplicate_email_leads([row1, row2, unique])
    assert list(duplicates.keys()) == ["same@abc.com"]
    assert len(duplicates["same@abc.com"]) == 2


def test_find_duplicate_email_leads_case_insensitive_and_ignores_blank_email():
    row1 = make_lead(_row=2, LeadID="L1", Email="Same@ABC.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="same@abc.com")
    blank = make_lead(_row=4, LeadID="L4", Email="")

    duplicates = outreach.find_duplicate_email_leads([row1, row2, blank])
    assert list(duplicates.keys()) == ["same@abc.com"]


def test_find_duplicate_email_leads_empty_when_all_unique():
    row1 = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    row2 = make_lead(_row=3, LeadID="L2", Email="b@abc.com")
    assert outreach.find_duplicate_email_leads([row1, row2]) == {}


# =============================================================================
# get_eligible_leads / build_batch / send_batch — ignore_wait_days override
# =============================================================================

def test_ignore_wait_days_makes_not_yet_due_lead_eligible():
    recent = datetime.now().strftime(FMT)  # intro sent moments ago — normally NOT due for 3 more days
    not_yet_waited = make_lead(IntroSentAt=recent)
    eligible = outreach.get_eligible_leads([not_yet_waited], STAGES, 1, ignore_wait_days=True)
    assert eligible == [not_yet_waited]


def test_ignore_wait_days_still_requires_previous_stage_actually_sent():
    # The override skips the WAIT, never the requirement that the stage
    # before it actually happened — stage order is never skippable.
    never_sent_intro = make_lead(IntroSentAt="")
    eligible = outreach.get_eligible_leads([never_sent_intro], STAGES, 1, ignore_wait_days=True)
    assert eligible == []


def test_ignore_wait_days_still_respects_every_other_eligibility_rule():
    recent = datetime.now().strftime(FMT)
    already_sent_this_stage = make_lead(IntroSentAt=recent, FollowUp1SentAt=recent)
    replied = make_lead(IntroSentAt=recent, ReplyStatus="Replied")
    stopped = make_lead(IntroSentAt=recent, Status="Stopped - Bounced")
    # Approval is no longer an eligibility rule at all, so it can't be
    # used here anymore — a lead with Approval="No" but nothing else
    # disqualifying it IS now eligible, which is exactly the fix in
    # Part 5. This test covers the rules that are still real gates.

    eligible = outreach.get_eligible_leads(
        [already_sent_this_stage, replied, stopped], STAGES, 1, ignore_wait_days=True)
    assert eligible == []


def test_ignore_wait_days_defaults_to_false_unchanged_behavior():
    recent = datetime.now().strftime(FMT)
    not_yet_waited = make_lead(IntroSentAt=recent)
    eligible = outreach.get_eligible_leads([not_yet_waited], STAGES, 1)  # no ignore_wait_days passed at all
    assert eligible == []


def test_build_batch_ignore_wait_days_passthrough(monkeypatch):
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "S", "body": "B", "missing_variables": [], "thread_subject": "S", "is_continuation": False})
    recent = datetime.now().strftime(FMT)
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(_row=2, IntroSentAt=recent)

    plan_default = outreach.build_batch(campaign_cfg, [lead], "followup1", 10)
    assert plan_default == []

    plan_override = outreach.build_batch(campaign_cfg, [lead], "followup1", 10, ignore_wait_days=True)
    assert len(plan_override) == 1


def test_send_batch_ignore_wait_days_passthrough(monkeypatch):
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    recent = datetime.now().strftime(FMT)
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com", IntroSentAt=recent)
    fake_sheets = FakeSheets([lead])

    results_default = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "followup1", 10)
    assert results_default == []

    results_override = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "followup1", 10,
                                            ignore_wait_days=True)
    assert len(results_override) == 1
    assert results_override[0]["status"] == "sent"


# =============================================================================
# Template rendering — optional fields get graceful defaults
# =============================================================================

def test_render_text_substitutes_known_variables():
    lead = {"FirstName": "John", "Company": "ABC Events"}
    result = outreach.render_text("Hi {{FirstName}} from {{CompanyName}}", lead)
    assert result == "Hi John from ABC Events"


def test_render_text_unknown_variable_renders_empty_not_literal_placeholder():
    result = outreach.render_text("Hi {{NotARealVar}}", {})
    assert result == "Hi "
    assert "{{" not in result


def test_render_text_tracks_unknown_variable_as_missing():
    missing = []
    outreach.render_text("Hi {{TotallyUnknownField}}", {}, missing_out=missing)
    assert missing == ["TotallyUnknownField"]


def test_render_text_custom_column_resolves_directly():
    lead = {"Industry": "Healthcare"}
    result = outreach.render_text("Sector: {{Industry}}", lead)
    assert result == "Sector: Healthcare"


def test_render_text_blank_custom_column_renders_empty_without_flagging_missing():
    lead = {"Industry": ""}
    missing = []
    result = outreach.render_text("Sector: {{Industry}}", lead, missing_out=missing)
    assert result == "Sector: "
    assert missing == []  # blank DATA for a real column is not an error


def test_render_email_missing_variables_deduped():
    # Two different templates referencing the same unknown variable twice
    # within one file would only need de-duplication at render_email level;
    # simulate via two render_text calls sharing one missing_out list.
    missing = []
    outreach.render_text("{{Ghost}} and {{Ghost}} again", {}, missing_out=missing)
    seen = set()
    deduped = [m for m in missing if not (m in seen or seen.add(m))]
    assert deduped == ["Ghost"]


def test_render_text_blank_first_name_gets_default_not_literal_placeholder():
    result = outreach.render_text("Hi {{FirstName}},", {"FirstName": ""})
    assert result == "Hi there,"
    assert "{{" not in result


def test_render_text_blank_company_gets_default():
    result = outreach.render_text("at {{CompanyName}}", {"Company": ""})
    assert result == "at your team"


# =============================================================================
# render_email — blank Subject means "continue the existing thread"
# =============================================================================

def test_render_email_blank_subject_continues_thread_with_re_prefix(tmp_path):
    campaign_dir = tmp_path / "ThreadTest"
    campaign_dir.mkdir()
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nJust following up, {{FirstName}}.")

    lead = {"FirstName": "Sam", "ThreadSubject": "Quick question for you"}
    rendered = outreach.render_email(str(campaign_dir), "followup1", "A", lead, is_first_stage=False)

    assert rendered["subject"] == "Re: Quick question for you"
    assert rendered["is_continuation"] is True
    assert rendered["thread_subject"] == "Quick question for you"  # unchanged, still the original


def test_render_email_blank_subject_no_double_re_prefix(tmp_path):
    campaign_dir = tmp_path / "ThreadTest2"
    campaign_dir.mkdir()
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nBody.")

    lead = {"ThreadSubject": "Re: Already a reply"}
    rendered = outreach.render_email(str(campaign_dir), "followup1", "A", lead, is_first_stage=False)

    assert rendered["subject"] == "Re: Already a reply"  # not "Re: Re: Already a reply"


def test_render_email_blank_subject_on_first_stage_raises_clearly(tmp_path):
    campaign_dir = tmp_path / "ThreadTest3"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: \n\nBody.")

    with pytest.raises(outreach.TemplateError, match="no previous thread to continue"):
        outreach.render_email(str(campaign_dir), "intro", "A", {}, is_first_stage=True)


def test_render_email_blank_subject_with_no_stored_thread_subject_raises_clearly(tmp_path):
    campaign_dir = tmp_path / "ThreadTest4"
    campaign_dir.mkdir()
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nBody.")

    lead = {"LeadID": "L99", "ThreadSubject": ""}  # never set — e.g. sent before this feature existed
    with pytest.raises(outreach.TemplateError, match="no ThreadSubject recorded"):
        outreach.render_email(str(campaign_dir), "followup1", "A", lead, is_first_stage=False)


def test_render_email_non_blank_subject_resets_thread_subject(tmp_path):
    campaign_dir = tmp_path / "ThreadTest5"
    campaign_dir.mkdir()
    (campaign_dir / "followup2_A.txt").write_text("Subject: A brand new angle for {{FirstName}}\n\nBody.")

    lead = {"FirstName": "Sam", "ThreadSubject": "The old subject"}
    rendered = outreach.render_email(str(campaign_dir), "followup2", "A", lead, is_first_stage=False)

    assert rendered["subject"] == "A brand new angle for Sam"
    assert rendered["is_continuation"] is False
    assert rendered["thread_subject"] == "A brand new angle for Sam"  # reset to the new one


def test_build_batch_carries_thread_subject_and_continuation_flag(monkeypatch):
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "Re: Original", "body": "B", "missing_variables": [],
        "thread_subject": "Original", "is_continuation": True,
    })
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(_row=2)
    plan = outreach.build_batch(campaign_cfg, [lead], "intro", 10)
    assert plan[0]["thread_subject"] == "Original"
    assert plan[0]["is_continuation"] is True


def test_send_batch_writes_thread_subject_to_master_sheet(monkeypatch):
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "Quick question", "body": "B", "missing_variables": [],
        "thread_subject": "Quick question", "is_continuation": False,
    })
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert fake_sheets._leads[0]["ThreadSubject"] == "Quick question"


# =============================================================================
# backfill_thread_subjects — one-time migration for pre-existing leads
# =============================================================================

def test_backfill_skips_lead_with_thread_subject_already_set():
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(LeadID="L1", IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                      ThreadSubject="Already set")
    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])
    assert results == [{"lead_id": "L1", "status": "skipped_already_set"}]


def test_backfill_skips_lead_never_sent_to():
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(IntroSentAt="", ThreadSubject="")
    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])
    assert results[0]["status"] == "skipped_not_sent_yet"


def test_backfill_skips_lead_with_unknown_variant():
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="", ThreadSubject="")
    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])
    assert results[0]["status"] == "skipped_unknown_variant"
    assert results[0]["stage"] == "intro"


def test_backfill_uses_intro_when_only_intro_sent():
    campaign_cfg = _base_campaign_cfg()
    lead = make_lead(_row=2, LeadID="L1", FirstName="Sam",
                      IntroSentAt="2026-08-01 09:00:00", IntroVariant="A", ThreadSubject="")

    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])

    expected_tmpl = outreach.load_template(TEMPLATES_DIR, "intro", "A")
    expected_subject = outreach.render_text(expected_tmpl["subject"], lead)

    assert results[0]["status"] == "backfilled"
    assert results[0]["stage"] == "intro"
    assert results[0]["thread_subject"] == expected_subject
    assert results[0]["row"] == 2


def test_backfill_uses_most_recently_sent_stage_not_intro(tmp_path):
    # Lead has BOTH intro and followup1 sent — the correct subject to
    # backfill from is followup1's (the most recent), not intro's. This is
    # the core regression this whole function exists to get right.
    #
    # Deliberately isolated synthetic templates here, NOT the real
    # templates/Kelson_Creators_Licensing ones — those are meant to be
    # freely editable (including legitimately using a blank Subject for
    # continuation), and this test's assertion depends on a SPECIFIC
    # stage having a SPECIFIC non-blank subject, which the real templates
    # shouldn't be constrained to guarantee forever.
    campaign_dir = tmp_path / "BackfillOrderCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Intro subject\n\nBody.")
    (campaign_dir / "followup1_B.txt").write_text("Subject: FollowUp1 subject\n\nBody.")

    campaign_cfg = {
        "templates_dir": str(campaign_dir), "variants": ["A", "B"],
        "stages": [
            {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
            {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
        ],
    }
    lead = make_lead(_row=3, LeadID="L2", FirstName="Sam",
                      IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                      FollowUp1SentAt="2026-08-05 09:00:00", FollowUp1Variant="B", ThreadSubject="")

    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])

    assert results[0]["status"] == "backfilled"
    assert results[0]["stage"] == "followup1"
    assert results[0]["thread_subject"] == "FollowUp1 subject"


def test_backfill_isolates_per_lead_template_errors(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    good_lead = make_lead(_row=2, LeadID="L1", IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                           ThreadSubject="")
    bad_lead = make_lead(_row=3, LeadID="L2", IntroSentAt="2026-08-01 09:00:00", IntroVariant="Z",
                          ThreadSubject="")  # variant Z has no template file — must not exist

    results = outreach.backfill_thread_subjects(campaign_cfg, [good_lead, bad_lead])

    statuses = {r["lead_id"]: r["status"] for r in results}
    assert statuses["L1"] == "backfilled"
    assert statuses["L2"] == "error"


def test_backfill_skips_when_rerendered_subject_is_itself_blank(tmp_path):
    campaign_dir = tmp_path / "BlankNowCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: \n\nBody.")  # since migrated to blank-subject itself

    campaign_cfg = {
        "templates_dir": str(campaign_dir), "variants": ["A"],
        "stages": [{"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0}],
    }
    lead = make_lead(IntroSentAt="2026-08-01 09:00:00", IntroVariant="A", ThreadSubject="")
    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])
    assert results[0]["status"] == "skipped_template_now_blank"


def test_backfill_walks_back_past_a_blank_most_recent_stage(tmp_path):
    # The exact real-world case that motivated this: Intro was sent with a
    # real subject (before this feature existed), then followup1 was ALSO
    # already sent — but using the NEW blank-subject-continuation
    # convention. The most recent sent stage (followup1) has nothing to
    # extract, but the real subject is still recoverable from Intro.
    campaign_dir = tmp_path / "HybridCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_C.txt").write_text(
        "Subject: DudeRobe – Meta Ad Usage Collaboration\n\nHi {{FirstName}},\n\nBody.")
    (campaign_dir / "followup1_C.txt").write_text("Subject: \n\nJust following up.")

    campaign_cfg = {
        "templates_dir": str(campaign_dir), "variants": ["C"],
        "stages": [
            {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
            {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
        ],
    }
    lead = make_lead(_row=5, LeadID="L1", FirstName="Rithik",
                      IntroSentAt="2026-08-26 05:29:01", IntroVariant="C",
                      FollowUp1SentAt="2026-08-26 08:47:39", FollowUp1Variant="C", ThreadSubject="")

    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])

    assert results[0]["status"] == "backfilled"
    assert results[0]["stage"] == "intro"  # walked back PAST followup1
    assert results[0]["thread_subject"] == "DudeRobe – Meta Ad Usage Collaboration"


def test_backfill_stops_at_first_non_blank_walking_backward_not_earliest(tmp_path):
    # Three stages sent: intro (real subject A), followup1 (blank/continued),
    # followup2 (real subject, deliberately reset). The correct answer is
    # followup2's — the MOST RECENT real subject — not intro's, even though
    # intro also has a real one and would be found by walking further back.
    campaign_dir = tmp_path / "ThreeStageCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Original intro subject\n\nBody.")
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nContinuing the thread.")
    (campaign_dir / "followup2_A.txt").write_text("Subject: A completely new angle\n\nBody.")

    campaign_cfg = {
        "templates_dir": str(campaign_dir), "variants": ["A"],
        "stages": [
            {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
            {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
            {"name": "followup2", "template_prefix": "followup2", "wait_days_after_previous": 4},
        ],
    }
    lead = make_lead(_row=2, LeadID="L1",
                      IntroSentAt="2026-08-01 09:00:00", IntroVariant="A",
                      FollowUp1SentAt="2026-08-05 09:00:00", FollowUp1Variant="A",
                      FollowUp2SentAt="2026-08-10 09:00:00", FollowUp2Variant="A", ThreadSubject="")

    results = outreach.backfill_thread_subjects(campaign_cfg, [lead])

    assert results[0]["status"] == "backfilled"
    assert results[0]["stage"] == "followup2"
    assert results[0]["thread_subject"] == "A completely new angle"


def test_backfill_returns_nothing_to_write_for_empty_lead_list():
    campaign_cfg = _base_campaign_cfg()
    assert outreach.backfill_thread_subjects(campaign_cfg, []) == []


# =============================================================================
# is_within_sending_window — Phase E (Schedule). Every test passes an
# explicit now_utc rather than relying on the real clock, so these are
# fully deterministic regardless of when the suite actually runs.
# =============================================================================

def test_sending_window_empty_schedule_always_allowed():
    within, reason = outreach.is_within_sending_window({})
    assert within is True
    assert reason == ""


def test_sending_window_schedule_with_no_timezone_always_allowed():
    # A schedule dict with only, say, send_days but no timezone can't be
    # evaluated meaningfully — treat it as "no restriction" rather than
    # guessing a timezone.
    within, _ = outreach.is_within_sending_window({"send_days": ["mon"]})
    assert within is True


def test_sending_window_within_simple_window():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # a Monday, noon UTC
    schedule = {"timezone": "UTC", "window_start": "09:00", "window_end": "17:00"}
    within, reason = outreach.is_within_sending_window(schedule, now_utc=now)
    assert within is True
    assert reason == ""


def test_sending_window_before_window_start():
    now = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)  # 06:00 UTC, before 09:00 window
    schedule = {"timezone": "UTC", "window_start": "09:00", "window_end": "17:00"}
    within, reason = outreach.is_within_sending_window(schedule, now_utc=now)
    assert within is False
    assert "outside the sending window" in reason


def test_sending_window_after_window_end():
    now = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)  # 18:00 UTC, after 17:00 window
    schedule = {"timezone": "UTC", "window_start": "09:00", "window_end": "17:00"}
    within, reason = outreach.is_within_sending_window(schedule, now_utc=now)
    assert within is False


def test_sending_window_boundaries_are_inclusive():
    schedule = {"timezone": "UTC", "window_start": "09:00", "window_end": "17:00"}
    at_start = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    at_end = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)
    assert outreach.is_within_sending_window(schedule, now_utc=at_start)[0] is True
    assert outreach.is_within_sending_window(schedule, now_utc=at_end)[0] is True


def test_sending_window_midnight_crossing_window_inside():
    # A window like 22:00-06:00 (overnight) — 23:00 and 02:00 should both
    # count as "inside", even though start > end numerically.
    schedule = {"timezone": "UTC", "window_start": "22:00", "window_end": "06:00"}
    late_night = datetime(2026, 6, 15, 23, 0, tzinfo=timezone.utc)
    early_morning = datetime(2026, 6, 16, 2, 0, tzinfo=timezone.utc)
    assert outreach.is_within_sending_window(schedule, now_utc=late_night)[0] is True
    assert outreach.is_within_sending_window(schedule, now_utc=early_morning)[0] is True


def test_sending_window_midnight_crossing_window_outside():
    schedule = {"timezone": "UTC", "window_start": "22:00", "window_end": "06:00"}
    midday = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    within, reason = outreach.is_within_sending_window(schedule, now_utc=midday)
    assert within is False


def test_sending_window_send_days_restricts_correctly():
    schedule = {"timezone": "UTC", "send_days": ["mon", "tue", "wed", "thu", "fri"]}
    monday = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)  # a Monday
    saturday = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)  # a Saturday
    assert outreach.is_within_sending_window(schedule, now_utc=monday)[0] is True
    within, reason = outreach.is_within_sending_window(schedule, now_utc=saturday)
    assert within is False
    assert "sat" in reason.lower()


def test_sending_window_send_days_case_and_length_insensitive():
    schedule = {"timezone": "UTC", "send_days": ["Monday", "TUE"]}
    monday = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    assert outreach.is_within_sending_window(schedule, now_utc=monday)[0] is True


def test_sending_window_send_days_and_window_both_apply():
    schedule = {"timezone": "UTC", "send_days": ["mon"], "window_start": "09:00", "window_end": "17:00"}
    monday_in_window = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    monday_outside_window = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    tuesday_in_window = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    assert outreach.is_within_sending_window(schedule, now_utc=monday_in_window)[0] is True
    assert outreach.is_within_sending_window(schedule, now_utc=monday_outside_window)[0] is False
    assert outreach.is_within_sending_window(schedule, now_utc=tuesday_in_window)[0] is False


def test_sending_window_invalid_timezone_raises_config_error():
    with pytest.raises(outreach.ConfigError, match="Invalid timezone"):
        outreach.is_within_sending_window({"timezone": "Not/A/Real/Zone"})


def test_sending_window_invalid_time_format_raises_config_error():
    with pytest.raises(outreach.ConfigError, match="window_start/window_end"):
        outreach.is_within_sending_window({"timezone": "UTC", "window_start": "9am", "window_end": "5pm"})


def test_sending_window_dst_transition_same_utc_moment_different_local_result():
    """The actual point of using zoneinfo instead of a fixed offset: the
    SAME UTC instant of day must evaluate differently on either side of a
    DST transition, because the real local time differs. A naive
    fixed-offset implementation would get one of these two cases wrong."""
    schedule = {"timezone": "America/Los_Angeles", "window_start": "09:00", "window_end": "17:00"}
    # 16:00 UTC is 08:00 PST before the US 2026 spring-forward (Mar 8) —
    # before the window opens.
    before_dst = datetime(2026, 3, 1, 16, 0, tzinfo=timezone.utc)
    # The SAME 16:00 UTC is 09:00 PDT after spring-forward — right at the
    # window's start.
    after_dst = datetime(2026, 3, 15, 16, 0, tzinfo=timezone.utc)

    assert outreach.is_within_sending_window(schedule, now_utc=before_dst)[0] is False
    assert outreach.is_within_sending_window(schedule, now_utc=after_dst)[0] is True


def test_sending_window_naive_datetime_treated_as_utc():
    """now_utc without tzinfo (e.g. datetime.now() called naively) must
    still work correctly rather than raising or silently misbehaving —
    treated as UTC, matching the function's own default when now_utc is
    omitted entirely."""
    naive_within_window = datetime(2026, 6, 15, 12, 0)  # no tzinfo
    schedule = {"timezone": "UTC", "window_start": "09:00", "window_end": "17:00"}
    within, _ = outreach.is_within_sending_window(schedule, now_utc=naive_within_window)
    assert within is True


# =============================================================================
# import_leads / remove_leads — Data tab backend (soft-remove only, never
# a hard delete; see remove_leads' docstring)
# =============================================================================

def test_import_leads_appends_with_sequential_ids_continuing_from_max():
    existing = [make_lead(_row=2, LeadID="3", Email="existing@abc.com")]
    fake_sheets = FakeSheets(existing)
    new_leads = [{"FirstName": "Sam", "Email": "sam@abc.com"}, {"FirstName": "Alex", "Email": "alex@abc.com"}]

    summary = outreach.import_leads(fake_sheets, "TestCampaign", new_leads)

    assert summary == {"imported": 2, "skipped_duplicate": 0, "skipped_no_email": 0}
    imported = [l for l in fake_sheets._leads if l.get("FirstName") in ("Sam", "Alex")]
    assert {l["LeadID"] for l in imported} == {"4", "5"}  # continues from existing max (3)


def test_import_leads_sets_campaign_and_blank_approval_by_default():
    fake_sheets = FakeSheets([])
    outreach.import_leads(fake_sheets, "TestCampaign", [{"Email": "sam@abc.com"}])
    imported = fake_sheets._leads[0]
    assert imported["Campaign"] == "TestCampaign"
    assert imported["Approval"] == ""  # Pending — never defaults to eligible-to-send


def test_import_leads_respects_explicit_approval_if_caller_set_it():
    fake_sheets = FakeSheets([])
    outreach.import_leads(fake_sheets, "TestCampaign", [{"Email": "sam@abc.com", "Approval": "Yes"}])
    assert fake_sheets._leads[0]["Approval"] == "Yes"


def test_import_leads_skips_rows_with_no_email():
    fake_sheets = FakeSheets([])
    summary = outreach.import_leads(fake_sheets, "TestCampaign", [{"FirstName": "NoEmail"}])
    assert summary == {"imported": 0, "skipped_duplicate": 0, "skipped_no_email": 1}
    assert fake_sheets._leads == []


def test_import_leads_skips_duplicate_emails_case_insensitive():
    existing = [make_lead(_row=2, LeadID="1", Email="Sam@Abc.com")]
    fake_sheets = FakeSheets(existing)
    summary = outreach.import_leads(fake_sheets, "TestCampaign", [{"Email": "sam@abc.com"}])
    assert summary == {"imported": 0, "skipped_duplicate": 1, "skipped_no_email": 0}


def test_import_leads_skips_duplicates_within_the_same_import_batch_too():
    fake_sheets = FakeSheets([])
    new_leads = [{"Email": "sam@abc.com"}, {"Email": "SAM@ABC.COM"}]
    summary = outreach.import_leads(fake_sheets, "TestCampaign", new_leads)
    assert summary == {"imported": 1, "skipped_duplicate": 1, "skipped_no_email": 0}


def test_import_leads_first_id_is_one_when_no_existing_leads():
    fake_sheets = FakeSheets([])
    outreach.import_leads(fake_sheets, "TestCampaign", [{"Email": "sam@abc.com"}])
    assert fake_sheets._leads[0]["LeadID"] == "1"


def test_import_leads_widens_header_with_union_of_every_row_field_before_appending():
    """The actual fix: different rows in one CSV can populate DIFFERENT
    custom columns — the widen call must cover the union across the
    whole batch, not just the first row's fields, and must happen before
    any append (append_lead only fills in columns that already exist)."""
    fake_sheets = FakeSheets([])
    outreach.import_leads(fake_sheets, "TestCampaign", [
        {"Email": "a@abc.com", "Client": "DudeRobe"},
        {"Email": "b@abc.com", "Product": "Robe"},
    ])
    assert len(fake_sheets.header_widen_calls) == 1
    widened_fields = set(fake_sheets.header_widen_calls[0])
    assert "Client" in widened_fields
    assert "Product" in widened_fields


def test_import_leads_does_not_widen_header_when_batch_is_empty():
    fake_sheets = FakeSheets([])
    outreach.import_leads(fake_sheets, "TestCampaign", [])
    assert fake_sheets.header_widen_calls == []


# ---------- SheetsConnector.ensure_master_header_includes ----------

class _FakeMasterWs:
    def __init__(self, header, col_count):
        self._header = header
        self.col_count = col_count
        self.resize_calls = []
        self.batch_update_calls = []

    def row_values(self, n):
        return self._header if n == 1 else []

    def resize(self, cols):
        self.resize_calls.append(cols)
        self.col_count = cols

    def batch_update(self, updates):
        self.batch_update_calls.append(updates)


def _make_sheets_connector(master_ws):
    sheets = outreach.SheetsConnector.__new__(outreach.SheetsConnector)
    sheets.master_ws = master_ws
    sheets._gspread = type("G", (), {"utils": type("U", (), {
        "rowcol_to_a1": staticmethod(lambda r, c: f"R{r}C{c}")
    })})()
    return sheets


def test_ensure_master_header_includes_adds_only_missing_columns():
    ws = _FakeMasterWs(header=["LeadID", "Email"], col_count=38)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["Email", "Client", "Product"])
    written_values = [u["values"][0][0] for u in ws.batch_update_calls[0]]
    assert set(written_values) == {"Client", "Product"}  # Email already existed, not rewritten


def test_ensure_master_header_includes_does_nothing_when_all_columns_already_exist():
    ws = _FakeMasterWs(header=["LeadID", "Email", "Client"], col_count=38)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["Email", "Client"])
    assert ws.batch_update_calls == []
    assert ws.resize_calls == []


def test_ensure_master_header_includes_resizes_grid_when_too_narrow():
    """The exact real crash this guards against: writing beyond the
    sheet's allocated grid width raises a hard API error — this must
    grow the grid FIRST, before ever writing a header cell."""
    ws = _FakeMasterWs(header=[f"col{i}" for i in range(38)], col_count=38)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["NewField"])
    assert ws.resize_calls == [49]  # 38 existing + 1 new + 10 headroom
    assert ws.col_count == 49


def test_ensure_master_header_includes_does_not_resize_when_grid_already_wide_enough():
    ws = _FakeMasterWs(header=["LeadID", "Email"], col_count=100)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["Client"])
    assert ws.resize_calls == []


def test_ensure_master_header_includes_ignores_falsy_column_names():
    """A blank/empty column name slipping through (e.g. a CSV column
    mapped to an empty target somehow) must never become a real header
    cell — filtered out before the missing-columns check, same as the
    real, authoritative implementation does."""
    ws = _FakeMasterWs(header=["LeadID", "Email"], col_count=38)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["", "Client", None])
    written_values = [u["values"][0][0] for u in ws.batch_update_calls[0]]
    assert written_values == ["Client"]


def test_ensure_master_header_includes_new_columns_placed_after_existing_ones():
    ws = _FakeMasterWs(header=["LeadID", "Email"], col_count=38)
    sheets = _make_sheets_connector(ws)
    sheets.ensure_master_header_includes(["Client"])
    written_range = ws.batch_update_calls[0][0]["range"]
    assert written_range == "R1C3"  # column 3 — right after the 2 existing columns


def test_remove_leads_sets_status_removed_not_hard_delete():
    leads = [make_lead(_row=2, LeadID="5", Email="a@abc.com"), make_lead(_row=3, LeadID="8", Email="b@abc.com")]
    fake_sheets = FakeSheets(leads)

    summary = outreach.remove_leads(fake_sheets, ["5"])

    assert summary == {"removed": 1, "not_found": 0}
    assert len(fake_sheets._leads) == 2  # row still exists — soft remove, not delete
    removed = next(l for l in fake_sheets._leads if l["LeadID"] == "5")
    assert removed["Status"] == outreach.STATUS_REMOVED
    kept = next(l for l in fake_sheets._leads if l["LeadID"] == "8")
    assert kept["Status"] != outreach.STATUS_REMOVED


def test_remove_leads_reports_not_found_ids():
    leads = [make_lead(_row=2, LeadID="5", Email="a@abc.com")]
    fake_sheets = FakeSheets(leads)
    summary = outreach.remove_leads(fake_sheets, ["5", "999"])
    assert summary == {"removed": 1, "not_found": 1}


def test_removed_status_excludes_lead_from_eligibility():
    lead = make_lead(Status=outreach.STATUS_REMOVED)
    eligible = outreach.get_eligible_leads([lead], STAGES, 0)
    assert eligible == []


def test_all_20_templates_load_and_render_without_leftover_placeholders_even_with_blank_fields():
    # Deliberately blank FirstName/LastName/Company to prove optional fields
    # never leak "{{...}}" into an outgoing email. ThreadSubject is
    # supplied so this test isn't coupled to whether any particular real
    # template has chosen to use a blank Subject (continue-the-thread) —
    # that's a legitimate, supported per-template choice, and this test's
    # job is just "does everything render cleanly", not "does every
    # template have its own subject".
    lead = {"FirstName": "", "LastName": "", "Company": "", "Email": "john@abc.com",
            "ThreadSubject": "Placeholder Original Subject"}
    stages = ["intro", "followup1", "followup2", "followup3", "followup4"]
    variants = ["A", "B", "C", "D"]
    count = 0
    for stage in stages:
        for variant in variants:
            rendered = outreach.render_email(TEMPLATES_DIR, stage, variant, lead,
                                               is_first_stage=(stage == "intro"))
            assert rendered["subject"], f"{stage}_{variant}: empty subject"
            assert "{{" not in rendered["subject"], f"{stage}_{variant}: unrendered var in subject"
            assert "{{" not in rendered["body"], f"{stage}_{variant}: unrendered var in body"
            count += 1
    assert count == 20


def test_templates_contain_no_diaz_or_event_branding():
    stages = ["intro", "followup1", "followup2", "followup3", "followup4"]
    variants = ["A", "B", "C", "D"]
    for stage in stages:
        for variant in variants:
            path = os.path.join(TEMPLATES_DIR, f"{stage}_{variant}.txt")
            content = open(path, encoding="utf-8").read().lower()
            assert "diaz" not in content
            assert "festival" not in content
            assert "eventname" not in content


# =============================================================================
# config loader — template-folder discovery, settings.yaml, optional overrides
# =============================================================================

def _make_config_fixture(tmp_path, shared_sheet_id="real_sheet_id_123", extra_settings_yaml="",
                          campaign_name="test_campaign", create_templates=True, template_variants=("A",),
                          override_yaml=None):
    """Builds a full settings.yaml + templates/<campaign>/ + optional
    config/campaigns/<campaign>.yaml fixture under tmp_path, and returns
    the (settings_path, campaigns_dir, templates_root) tuple to pass into
    get_campaign()."""
    settings_path = tmp_path / "settings.yaml"
    campaigns_dir = tmp_path / "campaigns"
    templates_root = tmp_path / "templates"
    campaigns_dir.mkdir(exist_ok=True)
    templates_root.mkdir(exist_ok=True)

    settings_content = f"""
shared_sheet_id: "{shared_sheet_id}"
email_accounts:
  default_account: "sales1"
default_campaign_settings:
  stage_wait_days:
    intro: 0
    followup1: 3
    followup2: 4
    followup3: 5
    followup4: 5
  sending:
    timezone: "Asia/Kolkata"
    window_start: "09:00"
    window_end: "17:00"
    delay_min_minutes: 1
    delay_max_minutes: 2
    daily_limit: 10
{extra_settings_yaml}
"""
    settings_path.write_text(settings_content)

    if create_templates:
        campaign_templates_dir = templates_root / campaign_name
        campaign_templates_dir.mkdir(exist_ok=True)
        for variant in template_variants:
            (campaign_templates_dir / f"intro_{variant}.txt").write_text("Subject: Hi\n\nBody")

    if override_yaml is not None:
        (campaigns_dir / f"{campaign_name}.yaml").write_text(override_yaml)

    return str(settings_path), str(campaigns_dir), str(templates_root)


def test_get_campaign_auto_derives_tab_names_from_campaign_key(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["sheet_id"] == "real_sheet_id_123"
    assert cfg["master_tab"] == "test_campaign Master Sheet"
    assert cfg["responses_tab"] == "test_campaign Response Sheet"
    assert cfg["send_log_tab"] == "test_campaign Custom Log Sheet"
    assert cfg["error_log_tab"] == "test_campaign Error Log"
    assert cfg["dashboard_tab"] == "test_campaign Dashboard"
    assert cfg["_global_default_account"] == "sales1"


def test_get_campaign_rejects_missing_shared_sheet_id(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, shared_sheet_id="")
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


def test_get_campaign_missing_campaign_raises_and_lists_available(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path)
    try:
        outreach.get_campaign("does_not_exist", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError as exc:
        assert "does_not_exist" in str(exc)
        assert "test_campaign" in str(exc)  # lists what IS available


def test_get_campaign_works_with_zero_override_files(tmp_path):
    # The core promise: a campaign with templates but NO override file at
    # all must just work, using pure defaults.
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["sending"]["daily_limit"] == 10
    assert cfg["variants"] == ["A"]


def test_get_campaign_override_file_changes_only_what_it_specifies(tmp_path):
    override = """
sending:
  daily_limit: 999
"""
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["sending"]["daily_limit"] == 999          # overridden
    assert cfg["sending"]["window_start"] == "09:00"      # still inherited
    assert cfg["variants"] == ["A"]                       # still inherited


def test_get_campaign_status_defaults_to_active_when_unset(tmp_path):
    # Critical backward-compat guarantee: every campaign that existed
    # before "status" was introduced has no override for it at all, and
    # must keep behaving exactly as it always did — never silently paused.
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["status"] == "active"


def test_get_campaign_status_respects_explicit_override(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml="status: paused\n")
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["status"] == "paused"


def test_get_campaign_status_draft_override(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml="status: draft\n")
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["status"] == "draft"


def test_get_campaign_explicit_tab_override_wins_over_auto_derivation(tmp_path):
    override = 'master_tab: "CustomMaster"\n'
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["master_tab"] == "CustomMaster"
    assert cfg["responses_tab"] == "test_campaign Response Sheet"  # still auto-derived


# =============================================================================
# Template-folder discovery IS campaign existence (the actual safety gate)
# =============================================================================

def test_get_campaign_raises_clearly_when_templates_folder_missing(tmp_path):
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, create_templates=False)
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError as exc:
        assert "No templates found" in str(exc)
        assert "test_campaign" in str(exc)


def test_get_campaign_auto_discovers_fewer_variants_without_erroring(tmp_path):
    # No explicit override: a campaign with just intro_A.txt (missing the
    # B/C/D that a bigger campaign might have) is a perfectly valid,
    # smaller campaign — must NOT raise, per the flexible-by-default design.
    settings_path, campaigns_dir, templates_root = _make_config_fixture(
        tmp_path, template_variants=("A",))
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["variants"] == ["A"]
    assert len(cfg["stages"]) == 1
    assert cfg["stages"][0]["name"] == "intro"


def test_get_campaign_explicit_override_still_raises_on_missing_file(tmp_path):
    # Only when stages/variants are EXPLICITLY declared together in an
    # override file does a missing template file raise an error —
    # auto-discovery (no override) never requires more than what's there.
    override = (
        "stages:\n"
        "  - name: intro\n"
        "    template_prefix: intro\n"
        "    wait_days_after_previous: 0\n"
        'variants: ["A", "B"]\n'
    )
    settings_path, campaigns_dir, templates_root = _make_config_fixture(
        tmp_path, template_variants=("A",), override_yaml=override)  # only A exists, B declared
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError as exc:
        assert "intro_B.txt" in str(exc)


def test_get_campaign_override_specifying_only_stages_not_variants_raises(tmp_path):
    override = (
        "stages:\n"
        "  - name: intro\n"
        "    template_prefix: intro\n"
        "    wait_days_after_previous: 0\n"
    )
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError as exc:
        assert "only one of" in str(exc)


def test_discover_campaign_names_lists_template_subfolders(tmp_path):
    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    (templates_root / "CampaignA").mkdir()
    (templates_root / "CampaignB").mkdir()
    (templates_root / ".hidden").mkdir()  # must be excluded
    (templates_root / "not_a_dir.txt").write_text("x")  # must be excluded — not a directory
    names = outreach.discover_campaign_names(str(templates_root))
    assert names == ["CampaignA", "CampaignB"]


def test_discover_campaign_names_empty_when_no_templates_root():
    names = outreach.discover_campaign_names("/tmp/definitely_does_not_exist_xyz")
    assert names == []


# =============================================================================
# _deep_merge
# =============================================================================

def test_deep_merge_overrides_only_specified_keys():
    base = {"a": 1, "sending": {"daily_limit": 100, "timezone": "UTC"}}
    override = {"sending": {"daily_limit": 5}}
    result = outreach._deep_merge(base, override)
    assert result == {"a": 1, "sending": {"daily_limit": 5, "timezone": "UTC"}}


def test_deep_merge_replaces_lists_wholesale_not_elementwise():
    base = {"stages": [{"name": "intro"}, {"name": "followup1"}]}
    override = {"stages": [{"name": "intro"}]}
    result = outreach._deep_merge(base, override)
    assert result["stages"] == [{"name": "intro"}]  # fully replaced, not merged


def test_deep_merge_does_not_mutate_base():
    base = {"sending": {"daily_limit": 100}}
    override = {"sending": {"daily_limit": 5}}
    outreach._deep_merge(base, override)
    assert base["sending"]["daily_limit"] == 100  # untouched


# =============================================================================
# resolve_sender_account — lead override > campaign default > global default
# =============================================================================

ACCOUNTS = {
    "sales1": {"address": "sales1@gmail.com", "app_password": "aaaa bbbb cccc dddd"},
    "sales2": {"address": "sales2@gmail.com", "app_password": "eeee ffff gggg hhhh"},
}


def test_resolve_uses_lead_override_first():
    lead = make_lead(SenderAccount="sales2")
    campaign_cfg = {"_global_default_account": "sales1"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales2"


def test_resolve_falls_back_to_campaign_default():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1", "default_sender_account": "sales2"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales2"


def test_resolve_falls_back_to_global_default():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales1"


def test_resolve_raises_for_unknown_lead_override():
    lead = make_lead(SenderAccount="does_not_exist")
    campaign_cfg = {"_global_default_account": "sales1"}
    try:
        outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_resolve_raises_when_nothing_configured():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": ""}
    try:
        outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


# =============================================================================
# SMTP message construction (pure — no real network)
# =============================================================================

def test_build_outbound_message_sets_core_headers():
    msg, message_id = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body text"
    )
    assert msg["From"] == "me@work.com"
    assert msg["To"] == "lead@abc.com"
    assert msg["Subject"] == "Hello"
    assert msg["Message-ID"] == message_id
    assert message_id  # non-empty
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_build_outbound_message_sets_threading_headers_when_provided():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Re: Hello", "Body",
        in_reply_to="<abc@mail.gmail.com>", references="<abc@mail.gmail.com>",
    )
    assert msg["In-Reply-To"] == "<abc@mail.gmail.com>"
    assert msg["References"] == "<abc@mail.gmail.com>"


def test_build_outbound_message_sets_cc_header_when_provided():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body", cc=["a@abc.com", "b@abc.com"],
    )
    assert msg["Cc"] == "a@abc.com, b@abc.com"


def test_build_outbound_message_omits_cc_header_when_not_provided():
    msg, _ = outreach._build_outbound_message("me@work.com", "lead@abc.com", "Hello", "Body")
    assert msg["Cc"] is None


def test_build_outbound_message_never_adds_a_bcc_header():
    """Bcc must never appear in the message itself — that's what makes it
    blind. build_outbound_message doesn't even accept a bcc parameter;
    only smtp_send does, where it can only ever affect the SMTP envelope
    recipient list, never the message content."""
    import inspect
    sig = inspect.signature(outreach._build_outbound_message)
    assert "bcc" not in sig.parameters


def test_smtp_send_cc_recipients_included_in_envelope(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, address, password):
            pass

        def sendmail(self, from_addr, to_addrs, msg_string):
            captured["to_addrs"] = to_addrs
            captured["msg_string"] = msg_string

    monkeypatch.setattr(outreach.smtplib, "SMTP_SSL", FakeSMTP)
    outreach.smtp_send("me@work.com", "app-pass", to="lead@abc.com", subject="Hi", body_text="Body",
                        cc=["cc1@abc.com", "cc2@abc.com"])

    assert captured["to_addrs"] == ["lead@abc.com", "cc1@abc.com", "cc2@abc.com"]
    assert "Cc: cc1@abc.com, cc2@abc.com" in captured["msg_string"]


def test_smtp_send_bcc_recipients_in_envelope_but_never_in_message_text(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, address, password):
            pass

        def sendmail(self, from_addr, to_addrs, msg_string):
            captured["to_addrs"] = to_addrs
            captured["msg_string"] = msg_string

    monkeypatch.setattr(outreach.smtplib, "SMTP_SSL", FakeSMTP)
    outreach.smtp_send("me@work.com", "app-pass", to="lead@abc.com", subject="Hi", body_text="Body",
                        bcc=["secret@abc.com"])

    assert "secret@abc.com" in captured["to_addrs"]  # gets the mail
    assert "secret@abc.com" not in captured["msg_string"]  # but invisible to every other recipient
    assert "Bcc" not in captured["msg_string"]


def test_smtp_send_cc_and_bcc_together(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, address, password):
            pass

        def sendmail(self, from_addr, to_addrs, msg_string):
            captured["to_addrs"] = to_addrs

    monkeypatch.setattr(outreach.smtplib, "SMTP_SSL", FakeSMTP)
    outreach.smtp_send("me@work.com", "app-pass", to="lead@abc.com", subject="Hi", body_text="Body",
                        cc=["cc@abc.com"], bcc=["bcc@abc.com"])

    assert captured["to_addrs"] == ["lead@abc.com", "cc@abc.com", "bcc@abc.com"]


def test_smtp_send_with_no_cc_or_bcc_envelope_unchanged():
    """Backward-compat guarantee — every existing call site (build_batch's
    normal sends) doesn't pass cc/bcc at all; the envelope must stay
    exactly [to], matching behavior before this feature existed."""
    import inspect
    sig = inspect.signature(outreach.smtp_send)
    assert sig.parameters["cc"].default is None
    assert sig.parameters["bcc"].default is None


# =============================================================================
# send_manual_reply — Phase H1. Deliberately never looks anything up
# itself (which thread, which lead) — the caller is expected to have
# already resolved to/subject/in_reply_to/references; this only sends
# and validates what it's given.
# =============================================================================

ACCOUNTS_FOR_REPLY = {"sales1": {"address": "sales1@gmail.com", "app_password": "aaaa bbbb cccc dddd"}}


def test_smtp_connection_settings_defaults_to_gmail():
    account = {"address": "sales1@gmail.com", "app_password": "x"}
    host, port, username = outreach.smtp_connection_settings(account)
    assert host == outreach.DEFAULT_SMTP_HOST
    assert port == outreach.DEFAULT_SMTP_PORT
    assert username == "sales1@gmail.com"  # defaults to the address itself


def test_smtp_connection_settings_uses_custom_provider_fields():
    account = {
        "address": "sales@example.com", "app_password": "x",
        "smtp_host": "smtp.hostinger.com", "smtp_port": 587, "smtp_username": "sales-login@example.com",
    }
    host, port, username = outreach.smtp_connection_settings(account)
    assert host == "smtp.hostinger.com"
    assert port == 587
    assert username == "sales-login@example.com"  # can differ from the address


def test_smtp_connection_settings_coerces_string_port_to_int():
    """A CSV import or a hand-typed form field commonly yields a string
    port ('587') — smtplib requires an actual int, not a string."""
    account = {"address": "a@b.com", "app_password": "x", "smtp_port": "587"}
    _, port, _ = outreach.smtp_connection_settings(account)
    assert port == 587
    assert isinstance(port, int)


def test_imap_connection_settings_defaults_to_gmail():
    account = {"address": "sales1@gmail.com", "app_password": "x"}
    host, port, username = outreach.imap_connection_settings(account)
    assert host == outreach.DEFAULT_IMAP_HOST
    assert port == outreach.DEFAULT_IMAP_PORT
    assert username == "sales1@gmail.com"


def test_imap_connection_settings_uses_custom_provider_fields():
    account = {
        "address": "sales@example.com", "app_password": "x",
        "imap_host": "imap.hostinger.com", "imap_port": 143, "imap_username": "sales-login@example.com",
    }
    host, port, username = outreach.imap_connection_settings(account)
    assert host == "imap.hostinger.com"
    assert port == 143
    assert username == "sales-login@example.com"


def test_get_imap_password_defaults_to_app_password():
    account = {"address": "a@b.com", "app_password": "smtp-pass"}
    assert outreach.get_imap_password(account) == "smtp-pass"


def test_get_imap_password_uses_distinct_imap_password_when_set():
    account = {"address": "a@b.com", "app_password": "smtp-pass", "imap_password": "imap-only-pass"}
    assert outreach.get_imap_password(account) == "imap-only-pass"


def test_slot_loader_preserves_custom_provider_fields():
    """The actual bug found and fixed: the slot loader used to keep only
    address/app_password, silently dropping smtp_host etc. even after
    a user carefully filled them in on the Add Account form."""
    import json as _json
    slot_json = _json.dumps({
        "name": "hostinger1", "address": "sales@example.com", "app_password": "x",
        "smtp_host": "smtp.hostinger.com", "smtp_port": 587,
        "imap_host": "imap.hostinger.com", "imap_port": 993,
    })
    import os as _os
    _os.environ["EMAIL_ACCOUNT_SLOT_1"] = slot_json
    try:
        accounts = outreach._load_email_accounts_from_slots()
    finally:
        del _os.environ["EMAIL_ACCOUNT_SLOT_1"]
    assert accounts["hostinger1"]["smtp_host"] == "smtp.hostinger.com"
    assert accounts["hostinger1"]["imap_host"] == "imap.hostinger.com"


def test_smtp_send_uses_custom_host_and_port(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, username, password):
            captured["login_username"] = username

        def sendmail(self, from_addr, to_addrs, msg_string):
            pass

    monkeypatch.setattr(outreach.smtplib, "SMTP_SSL", FakeSMTP)
    outreach.smtp_send("sales@example.com", "app-pass", to="lead@abc.com", subject="Hi", body_text="Body",
                        smtp_host="smtp.hostinger.com", smtp_port=587, smtp_username="login@example.com")

    assert captured["host"] == "smtp.hostinger.com"
    assert captured["port"] == 587
    assert captured["login_username"] == "login@example.com"  # NOT the From address


def test_smtp_send_no_custom_settings_uses_gmail_defaults(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None):
            captured["host"] = host
            captured["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, username, password):
            captured["login_username"] = username

        def sendmail(self, from_addr, to_addrs, msg_string):
            pass

    monkeypatch.setattr(outreach.smtplib, "SMTP_SSL", FakeSMTP)
    outreach.smtp_send("sales1@gmail.com", "app-pass", to="lead@abc.com", subject="Hi", body_text="Body")

    assert captured["host"] == "smtp.gmail.com"
    assert captured["port"] == 465
    assert captured["login_username"] == "sales1@gmail.com"  # defaults to the address


def test_check_single_account_health_uses_custom_imap_host(monkeypatch):
    captured = {}

    class FakeIMAP:
        def __init__(self, host, port):
            captured["host"] = host
            captured["port"] = port

        def login(self, username, password):
            captured["login_username"] = username

        def logout(self):
            pass

    monkeypatch.setattr(outreach.imaplib, "IMAP4_SSL", FakeIMAP)
    outreach.check_single_account_health("sales@example.com", "app-pass", imap_host="imap.hostinger.com",
                                          imap_port=143, imap_username="login@example.com")

    assert captured["host"] == "imap.hostinger.com"
    assert captured["port"] == 143
    assert captured["login_username"] == "login@example.com"



    captured = {}

    def fake_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None,
                        cc=None, bcc=None, attachments=None, smtp_host=None, smtp_port=None, smtp_username=None):
        captured.update(locals())
        return {"message_id": "<reply1@mail.gmail.com>"}

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    result = outreach.send_manual_reply(
        ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Re: Hi", "Thanks for your interest!",
        in_reply_to="<inbound1@mail.gmail.com>", references="<orig@mail.gmail.com> <inbound1@mail.gmail.com>",
    )

    assert result == {"message_id": "<reply1@mail.gmail.com>"}
    assert captured["to"] == "lead@abc.com"
    assert captured["in_reply_to"] == "<inbound1@mail.gmail.com>"
    assert captured["references"] == "<orig@mail.gmail.com> <inbound1@mail.gmail.com>"
    assert captured["address"] == "sales1@gmail.com"


def test_send_manual_reply_passes_through_cc_and_bcc(monkeypatch):
    captured = {}
    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: captured.update(kw) or {"message_id": "<m@mail.gmail.com>"})

    outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                cc=["cc@abc.com"], bcc=["bcc@abc.com"])

    assert captured["cc"] == ["cc@abc.com"]
    assert captured["bcc"] == ["bcc@abc.com"]


def test_send_manual_reply_raises_for_unknown_sender_account():
    with pytest.raises(outreach.MissingSenderAccountError, match="ghost_account"):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "ghost_account", "lead@abc.com", "Hi", "Body")


def test_send_manual_reply_raises_for_invalid_to_email():
    with pytest.raises(outreach.InvalidEmailFormatError):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "not-an-email", "Hi", "Body")


def test_send_manual_reply_raises_for_invalid_cc_email():
    with pytest.raises(outreach.InvalidEmailFormatError, match="Cc/Bcc"):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                    cc=["not-an-email"])


def test_send_manual_reply_raises_for_invalid_bcc_email():
    with pytest.raises(outreach.InvalidEmailFormatError, match="Cc/Bcc"):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                    bcc=["not-an-email"])


def test_send_manual_reply_never_calls_smtp_for_invalid_recipient(monkeypatch):
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1))
    with pytest.raises(outreach.InvalidEmailFormatError):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "not-an-email", "Hi", "Body")
    assert smtp_called == []


def test_send_manual_reply_works_with_no_threading_info():
    """A reply doesn't strictly require in_reply_to/references — while
    every real use case from the Responses tab will supply them, the
    function itself shouldn't require it (matches smtp_send's own
    Optional parameters)."""
    import inspect
    sig = inspect.signature(outreach.send_manual_reply)
    assert sig.parameters["in_reply_to"].default is None
    assert sig.parameters["references"].default is None


# =============================================================================
# Attachments — Phase H2
# =============================================================================

def test_build_outbound_message_no_attachments_produces_plain_mimetext():
    """Backward-compat guarantee — every automated send (build_batch's
    normal path) never passes attachments; the message shape must stay
    EXACTLY the plain MIMEText it always was, not a MIMEMultipart wrapper
    around a single part."""
    msg, _ = outreach._build_outbound_message("me@work.com", "lead@abc.com", "Hello", "Body")
    assert msg.get_content_maintype() == "text"
    assert not msg.is_multipart()


def test_build_outbound_message_with_attachments_is_multipart():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body",
        attachments=[{"filename": "photo.png", "content": b"fake-png-bytes"}],
    )
    assert msg.is_multipart()
    parts = msg.get_payload()
    assert len(parts) == 2  # body + one attachment


def test_build_outbound_message_attachment_filename_and_content_preserved():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body",
        attachments=[{"filename": "photo.png", "content": b"fake-png-bytes"}],
    )
    attachment_part = msg.get_payload()[1]
    assert attachment_part.get_filename() == "photo.png"
    import base64
    assert base64.b64decode(attachment_part.get_payload()) == b"fake-png-bytes"


def test_build_outbound_message_multiple_attachments():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body",
        attachments=[
            {"filename": "a.png", "content": b"aaa"},
            {"filename": "b.png", "content": b"bbb"},
        ],
    )
    parts = msg.get_payload()
    assert len(parts) == 3  # body + two attachments
    filenames = {p.get_filename() for p in parts[1:]}
    assert filenames == {"a.png", "b.png"}


def test_send_manual_reply_rejects_attachments_over_size_cap():
    oversized = [{"filename": "huge.png", "content": b"x" * (outreach.MAX_TOTAL_ATTACHMENT_BYTES + 1)}]
    with pytest.raises(outreach.AttachmentTooLargeError, match="MB"):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                    attachments=oversized)


def test_send_manual_reply_accepts_attachments_under_size_cap(monkeypatch):
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    small = [{"filename": "small.png", "content": b"x" * 100}]
    result = outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                         attachments=small)
    assert result == {"message_id": "<m@mail.gmail.com>"}


def test_send_manual_reply_size_check_sums_multiple_attachments():
    half = outreach.MAX_TOTAL_ATTACHMENT_BYTES // 2 + 1
    oversized = [{"filename": "a.png", "content": b"x" * half}, {"filename": "b.png", "content": b"x" * half}]
    with pytest.raises(outreach.AttachmentTooLargeError):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                    attachments=oversized)


def test_send_manual_reply_never_calls_smtp_when_attachments_too_large(monkeypatch):
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1))
    oversized = [{"filename": "huge.png", "content": b"x" * (outreach.MAX_TOTAL_ATTACHMENT_BYTES + 1)}]
    with pytest.raises(outreach.AttachmentTooLargeError):
        outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                    attachments=oversized)
    assert smtp_called == []


def test_send_manual_reply_passes_attachments_through_to_smtp_send(monkeypatch):
    captured = {}
    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: captured.update(kw) or {"message_id": "<m@mail.gmail.com>"})
    attachments = [{"filename": "photo.png", "content": b"data"}]
    outreach.send_manual_reply(ACCOUNTS_FOR_REPLY, "sales1", "lead@abc.com", "Hi", "Body",
                                attachments=attachments)
    assert captured["attachments"] == attachments


def test_smtp_send_no_attachments_backward_compatible():
    """Same backward-compat guarantee at the smtp_send layer — no
    attachments means attachments=None flows through unchanged, matching
    every call site before this feature existed."""
    import inspect
    sig = inspect.signature(outreach.smtp_send)
    assert sig.parameters["attachments"].default is None


# =============================================================================
# Account health check — connection status without ever exposing passwords
# =============================================================================

def test_check_single_account_health_success(monkeypatch):
    class FakeIMAP:
        def __init__(self, *a, **kw):
            pass

        def login(self, address, password):
            pass

        def logout(self):
            pass

    monkeypatch.setattr(outreach.imaplib, "IMAP4_SSL", FakeIMAP)
    status, detail = outreach.check_single_account_health("sales1@gmail.com", "app-pass")
    assert status == "Connected"
    assert detail == ""


def test_check_single_account_health_failure_never_includes_password(monkeypatch):
    class FakeIMAP:
        def __init__(self, *a, **kw):
            pass

        def login(self, address, password):
            raise outreach.imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")

        def logout(self):
            pass

    monkeypatch.setattr(outreach.imaplib, "IMAP4_SSL", FakeIMAP)
    status, detail = outreach.check_single_account_health("sales1@gmail.com", "super-secret-app-password")
    assert status == "Disconnected"
    assert "super-secret-app-password" not in detail
    assert "super-secret-app-password" not in status


def test_check_single_account_health_logs_out_even_on_failure(monkeypatch):
    logout_called = []

    class FakeIMAP:
        def __init__(self, *a, **kw):
            pass

        def login(self, address, password):
            raise RuntimeError("connection refused")

        def logout(self):
            logout_called.append(1)

    monkeypatch.setattr(outreach.imaplib, "IMAP4_SSL", FakeIMAP)
    outreach.check_single_account_health("sales1@gmail.com", "app-pass")
    assert logout_called == [1]


def test_check_single_account_health_logout_failure_does_not_mask_result(monkeypatch):
    class FakeIMAP:
        def __init__(self, *a, **kw):
            pass

        def login(self, address, password):
            pass

        def logout(self):
            raise RuntimeError("logout also broken")

    monkeypatch.setattr(outreach.imaplib, "IMAP4_SSL", FakeIMAP)
    status, detail = outreach.check_single_account_health("sales1@gmail.com", "app-pass")
    assert status == "Connected"  # the real result, not swallowed by the logout failure


def test_check_account_health_isolates_broken_account_from_working_ones(monkeypatch):
    accounts = {
        "sales1": {"address": "sales1@gmail.com", "app_password": "good-pass"},
        "sales2": {"address": "sales2@gmail.com", "app_password": "bad-pass"},
    }

    def fake_check(address, app_password, imap_host=None, imap_port=None, imap_username=None):
        if app_password == "bad-pass":
            return "Disconnected", "auth failed"
        return "Connected", ""

    monkeypatch.setattr(outreach, "check_single_account_health", fake_check)
    results = outreach.check_account_health(accounts)

    assert len(results) == 2
    by_name = {r["AccountName"]: r for r in results}
    assert by_name["sales1"]["Status"] == "Connected"
    assert by_name["sales2"]["Status"] == "Disconnected"


def test_check_account_health_never_includes_app_password_field(monkeypatch):
    accounts = {"sales1": {"address": "sales1@gmail.com", "app_password": "super-secret"}}
    monkeypatch.setattr(outreach, "check_single_account_health", lambda a, p, **kw: ("Connected", ""))
    results = outreach.check_account_health(accounts)
    assert "app_password" not in results[0]
    assert "super-secret" not in json.dumps(results[0])


def test_check_account_health_matches_column_schema():
    accounts = {"sales1": {"address": "sales1@gmail.com", "app_password": "x"}}
    with pytest.MonkeyPatch.context() as m:
        m.setattr(outreach, "check_single_account_health", lambda a, p, **kw: ("Connected", ""))
        results = outreach.check_account_health(accounts)
    assert set(results[0].keys()) == set(outreach.ACCOUNT_HEALTH_COLUMNS)


def test_check_account_health_empty_accounts_dict():
    assert outreach.check_account_health({}) == []


def test_write_account_health_clears_existing_rows_before_writing(monkeypatch):
    class FakeWorksheet:
        def __init__(self):
            self.cleared_ranges = []
            self.appended_rows = []
            self._values = [outreach.ACCOUNT_HEALTH_COLUMNS, ["old_account", "old@x.com", "Connected", "", "t"]]

        def get_all_values(self):
            return self._values

        def row_values(self, n):
            return self._values[n - 1] if n <= len(self._values) else []

        def batch_clear(self, ranges):
            self.cleared_ranges.extend(ranges)

        def append_rows(self, rows):
            self.appended_rows.extend(rows)

    fake_ws = FakeWorksheet()

    class FakeSpreadsheet:
        def worksheet(self, title):
            return fake_ws

    class FakeGspreadModule:
        class exceptions:
            class WorksheetNotFound(Exception):
                pass

    monkeypatch.setattr(outreach, "_build_gspread_client",
                         lambda: (type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet()})(),
                                  FakeGspreadModule))

    results = [{"AccountName": "sales1", "Address": "sales1@x.com", "Status": "Connected",
                "Detail": "", "CheckedAt": "2026-08-29 09:00:00"}]
    outreach.write_account_health("fake-sheet-id", results)

    assert fake_ws.cleared_ranges == ["A2:E2"]
    assert fake_ws.appended_rows == [["sales1", "sales1@x.com", "Connected", "", "2026-08-29 09:00:00"]]


def test_write_account_health_skips_clear_when_tab_was_empty(monkeypatch):
    class FakeWorksheet:
        def __init__(self):
            self.cleared_ranges = []
            self.appended_rows = []
            self._values = [outreach.ACCOUNT_HEALTH_COLUMNS]  # header only, no data rows yet

        def get_all_values(self):
            return self._values

        def row_values(self, n):
            return self._values[n - 1] if n <= len(self._values) else []

        def batch_clear(self, ranges):
            self.cleared_ranges.extend(ranges)

        def append_rows(self, rows):
            self.appended_rows.extend(rows)

    fake_ws = FakeWorksheet()

    class FakeSpreadsheet:
        def worksheet(self, title):
            return fake_ws

    class FakeGspreadModule:
        class exceptions:
            class WorksheetNotFound(Exception):
                pass

    monkeypatch.setattr(outreach, "_build_gspread_client",
                         lambda: (type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet()})(),
                                  FakeGspreadModule))

    outreach.write_account_health("fake-sheet-id", [{"AccountName": "sales1", "Address": "a@x.com",
                                                       "Status": "Connected", "Detail": "", "CheckedAt": "t"}])
    assert fake_ws.cleared_ranges == []  # nothing to clear


def test_cmd_check_account_health_writes_and_prints(monkeypatch, capsys):
    monkeypatch.setattr(outreach, "load_email_accounts",
                         lambda: {"sales1": {"address": "sales1@gmail.com", "app_password": "x"}})
    monkeypatch.setattr(outreach, "load_settings", lambda: {"shared_sheet_id": "fake-id"})
    monkeypatch.setattr(outreach, "check_account_health",
                         lambda accounts: [{"AccountName": "sales1", "Address": "sales1@gmail.com",
                                             "Status": "Connected", "Detail": "", "CheckedAt": "t"}])
    write_calls = []
    monkeypatch.setattr(outreach, "write_account_health", lambda sheet_id, results: write_calls.append((sheet_id, results)))

    outreach.cmd_check_account_health(argparse.Namespace())

    assert write_calls[0][0] == "fake-id"
    out = capsys.readouterr().out
    assert "sales1" in out
    assert "Connected" in out


# =============================================================================
# cmd_send_reply — the CLI/workflow entry point
# =============================================================================

def _write_reply_payload(tmp_path, **overrides):
    import json as _json
    payload = {
        "sender_account": "sales1", "to": "lead@abc.com", "subject": "Re: Hi",
        "body": "Thanks!", "in_reply_to": "<inbound1@mail.gmail.com>",
        "references": "<orig@mail.gmail.com> <inbound1@mail.gmail.com>",
        "lead_id": "5",
    }
    payload.update(overrides)
    path = tmp_path / "reply_payload.json"
    path.write_text(_json.dumps(payload))
    return str(path)


def test_cmd_send_reply_success_logs_sent_to_send_log(monkeypatch, tmp_path, capsys):
    fake_sheets = FakeSheets([])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<reply1@mail.gmail.com>"})

    payload_path = _write_reply_payload(tmp_path)
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)
    outreach.cmd_send_reply(args)

    assert len(fake_sheets.send_log) == 1
    entry = fake_sheets.send_log[0]
    assert entry["Status"] == "sent"
    assert entry["Stage"] == "manual_reply"
    assert entry["MessageID"] == "<reply1@mail.gmail.com>"
    assert entry["LeadID"] == "5"
    assert "Sent." in capsys.readouterr().out


def test_cmd_send_reply_failure_logs_error_and_exits_nonzero(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)

    def failing_smtp_send(*a, **kw):
        raise RuntimeError("SMTP boom")

    monkeypatch.setattr(outreach, "smtp_send", failing_smtp_send)

    payload_path = _write_reply_payload(tmp_path)
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)

    with pytest.raises(SystemExit) as exc_info:
        outreach.cmd_send_reply(args)
    assert exc_info.value.code == 1

    assert len(fake_sheets.send_log) == 1
    assert fake_sheets.send_log[0]["Status"] == "error"
    assert len(fake_sheets.error_log) == 1


def test_cmd_send_reply_unknown_sender_account_isolated(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)

    payload_path = _write_reply_payload(tmp_path, sender_account="ghost_account")
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)

    with pytest.raises(SystemExit):
        outreach.cmd_send_reply(args)

    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_MISSING_SENDER_ACCOUNT


def test_cmd_send_reply_passes_cc_bcc_through_to_send(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    captured = {}
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)
    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: captured.update(kw) or {"message_id": "<m@mail.gmail.com>"})

    payload_path = _write_reply_payload(tmp_path, cc=["cc@abc.com"], bcc=["bcc@abc.com"])
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)
    outreach.cmd_send_reply(args)

    assert captured["cc"] == ["cc@abc.com"]
    assert captured["bcc"] == ["bcc@abc.com"]


def test_cmd_send_reply_decodes_base64_attachments(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    captured = {}
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)
    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: captured.update(kw) or {"message_id": "<m@mail.gmail.com>"})

    import base64 as _b64
    encoded = _b64.b64encode(b"fake-png-bytes").decode("ascii")
    payload_path = _write_reply_payload(tmp_path, attachments=[{"filename": "photo.png", "content_base64": encoded}])
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)
    outreach.cmd_send_reply(args)

    assert captured["attachments"] == [{"filename": "photo.png", "content": b"fake-png-bytes"}]


def test_cmd_send_reply_no_attachments_key_passes_none(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    captured = {}
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)
    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: captured.update(kw) or {"message_id": "<m@mail.gmail.com>"})

    payload_path = _write_reply_payload(tmp_path)  # no "attachments" key at all
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)
    outreach.cmd_send_reply(args)

    assert captured["attachments"] is None


def test_cmd_send_reply_attachment_too_large_logs_and_exits_nonzero(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)
    monkeypatch.setattr(outreach, "load_email_accounts", lambda: ACCOUNTS_FOR_REPLY)

    import base64 as _b64
    huge_encoded = _b64.b64encode(b"x" * (outreach.MAX_TOTAL_ATTACHMENT_BYTES + 1)).decode("ascii")
    payload_path = _write_reply_payload(tmp_path,
                                         attachments=[{"filename": "huge.png", "content_base64": huge_encoded}])
    args = argparse.Namespace(campaign="test_campaign", file=payload_path)

    with pytest.raises(SystemExit):
        outreach.cmd_send_reply(args)

    assert fake_sheets.send_log[0]["Status"] == "error"
    assert len(fake_sheets.error_log) == 1


# =============================================================================
# mark_responses_read / cmd_mark_responses_read — persistent unread tracking
# =============================================================================

def test_mark_responses_read_builds_correct_batch_update(monkeypatch):
    captured = {}

    class FakeResponsesWs:
        def batch_update(self, updates):
            captured["updates"] = updates

    class FakeGspreadUtils:
        @staticmethod
        def rowcol_to_a1(row, col):
            return f"R{row}C{col}"

    sheets = outreach.SheetsConnector.__new__(outreach.SheetsConnector)
    sheets.responses_ws = FakeResponsesWs()
    sheets._gspread = type("G", (), {"utils": FakeGspreadUtils})()

    result = sheets.mark_responses_read({"r1": 2, "r2": 5})

    assert result == 2
    assert len(captured["updates"]) == 2
    assert {"range": "R2C14", "values": [["Yes"]]} in captured["updates"]
    assert {"range": "R5C14", "values": [["Yes"]]} in captured["updates"]


def test_mark_responses_read_empty_mapping_no_api_call(monkeypatch):
    call_count = []

    class FakeResponsesWs:
        def batch_update(self, updates):
            call_count.append(1)

    sheets = outreach.SheetsConnector.__new__(outreach.SheetsConnector)
    sheets.responses_ws = FakeResponsesWs()
    sheets._gspread = type("G", (), {"utils": type("U", (), {"rowcol_to_a1": staticmethod(lambda r, c: "")})})()

    result = sheets.mark_responses_read({})
    assert result == 0
    assert call_count == []  # never even attempted an empty batch update


def test_master_columns_includes_asanataskgid():
    """The exact real bug this guards against: sync_campaign_to_asana
    writes the newly-created task's GID back via
    update_lead_fields(lead["_row"], {"AsanaTaskGID": new_gid}) —
    update_lead_fields validates every field name against MASTER_COLUMNS
    and raises ValueError for anything not in that list. Without
    AsanaTaskGID in MASTER_COLUMNS, EVERY successful task creation would
    fail on this exact write-back, meaning the no-duplicate-tasks
    guarantee silently never actually worked: every sync run would try
    to create a fresh task for every lead, over and over, since the GID
    never successfully got stored anywhere."""
    assert "AsanaTaskGID" in outreach.MASTER_COLUMNS


def test_update_lead_fields_accepts_asanataskgid_without_raising():
    """Direct proof the write-back path itself succeeds, not just that
    the column name is present in the list somewhere."""
    class FakeMasterWs:
        def __init__(self):
            self.batch_update_calls = []

        def batch_update(self, updates):
            self.batch_update_calls.append(updates)

    sheets = outreach.SheetsConnector.__new__(outreach.SheetsConnector)
    sheets.master_ws = FakeMasterWs()
    sheets._gspread = type("G", (), {"utils": type("U", (), {
        "rowcol_to_a1": staticmethod(lambda r, c: f"R{r}C{c}")
    })})()

    sheets.update_lead_fields(2, {"AsanaTaskGID": "1218101643744828"})
    assert len(sheets.master_ws.batch_update_calls) == 1


def test_get_all_responses_tracks_row_numbers():
    fake_sheets = FakeSheets([])
    fake_sheets.append_response({"ResponseID": "r1"})
    fake_sheets.append_response({"ResponseID": "r2"})
    responses = fake_sheets.get_all_responses()
    assert responses[0]["_row"] == 2
    assert responses[1]["_row"] == 3


def test_cmd_mark_responses_read_marks_matching_ids(monkeypatch, tmp_path):
    fake_sheets = FakeSheets([])
    fake_sheets.append_response({"ResponseID": "r1"})
    fake_sheets.append_response({"ResponseID": "r2"})
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)

    payload_path = tmp_path / "mark_read.json"
    payload_path.write_text(json.dumps({"response_ids": ["r1", "r2"]}))
    args = argparse.Namespace(campaign="test_campaign", file=str(payload_path))
    outreach.cmd_mark_responses_read(args)

    assert len(fake_sheets.marked_read_calls) == 1
    assert fake_sheets.marked_read_calls[0] == {"r1": 2, "r2": 3}


def test_cmd_mark_responses_read_skips_ids_no_longer_present(monkeypatch, tmp_path, capsys):
    fake_sheets = FakeSheets([])
    fake_sheets.append_response({"ResponseID": "r1"})
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)

    payload_path = tmp_path / "mark_read.json"
    payload_path.write_text(json.dumps({"response_ids": ["r1", "r_gone"]}))
    args = argparse.Namespace(campaign="test_campaign", file=str(payload_path))
    outreach.cmd_mark_responses_read(args)

    assert fake_sheets.marked_read_calls[0] == {"r1": 2}
    out = capsys.readouterr().out
    assert "Marked 1 of 2" in out
    assert "r_gone" in out


def test_cmd_mark_responses_read_empty_list_is_a_noop(monkeypatch, tmp_path, capsys):
    fake_sheets = FakeSheets([])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: _base_campaign_cfg())
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: fake_sheets)

    payload_path = tmp_path / "mark_read.json"
    payload_path.write_text(json.dumps({"response_ids": []}))
    args = argparse.Namespace(campaign="test_campaign", file=str(payload_path))
    outreach.cmd_mark_responses_read(args)

    assert fake_sheets.marked_read_calls == []


# =============================================================================
# IMAP message parsing (pure — no real network)
# =============================================================================

def _make_raw_email(subject="Re: Hello", from_addr="john@abc.com", body="Sure, let's talk.",
                     in_reply_to=None, references=None):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "me@work.com"
    msg["Message-ID"] = "<reply123@mail.gmail.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg.as_bytes()


def test_parse_email_message_extracts_core_fields():
    raw = _make_raw_email(in_reply_to="<intro1@mail.gmail.com>", references="<intro1@mail.gmail.com>")
    parsed = outreach._parse_email_message(raw)
    assert parsed["subject"] == "Re: Hello"
    assert parsed["from"] == "john@abc.com"
    assert parsed["message_id"] == "<reply123@mail.gmail.com>"
    assert parsed["in_reply_to"] == "<intro1@mail.gmail.com>"
    assert parsed["references"] == "<intro1@mail.gmail.com>"
    assert "Sure, let's talk" in parsed["body"]


def test_parse_email_message_handles_missing_threading_headers():
    raw = _make_raw_email()
    parsed = outreach._parse_email_message(raw)
    assert parsed["in_reply_to"] == ""
    assert parsed["references"] == ""


# =============================================================================
# NextEligibleAt / BatchID
# =============================================================================

def test_next_eligible_at_computed_for_next_stage():
    now = datetime(2026, 8, 19, 10, 0, 0)
    result = outreach._compute_next_eligible_at(STAGES, 0, now)
    expected = (now + timedelta(days=STAGES[1]["wait_days_after_previous"])).strftime(outreach.DATETIME_FMT)
    assert result == expected


def test_next_eligible_at_blank_for_last_stage():
    now = datetime(2026, 8, 19, 10, 0, 0)
    result = outreach._compute_next_eligible_at(STAGES, len(STAGES) - 1, now)
    assert result == ""


def test_make_batch_id_format():
    batch_id = outreach.make_batch_id()
    assert batch_id.startswith("BATCH-")
    assert len(batch_id) == len("BATCH-20260819-103000")


# =============================================================================
# build_batch — variant override + thread continuation
# =============================================================================

def test_build_batch_forced_variant_applies_to_all():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com"),
             make_lead(_row=3, LeadID="L2", Email="jane@xyz.com")]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="B")
    assert len(plan) == 2
    assert all(item["variant"] == "B" for item in plan)


def test_build_batch_rejects_invalid_forced_variant():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    try:
        outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="Z")
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_intro_stage_never_sets_in_reply_to():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<old@mail.gmail.com>")]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10)
    assert plan[0]["in_reply_to"] is None


def test_followup_continues_thread_via_in_reply_to_and_references():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=old,
                        MessageID="<intro1@mail.gmail.com>", ThreadReferences="")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup1", 10)
    assert len(plan) == 1
    assert plan[0]["in_reply_to"] == "<intro1@mail.gmail.com>"
    assert plan[0]["references"] == "<intro1@mail.gmail.com>"


def test_followup_accumulates_references_chain():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"],
                     "stages": STAGES + [{"name": "followup2", "template_prefix": "followup2",
                                           "wait_days_after_previous": 4}]}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=old, FollowUp1SentAt=old,
                        MessageID="<fu1@mail.gmail.com>", ThreadReferences="<intro1@mail.gmail.com>")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup2", 10)
    assert plan[0]["in_reply_to"] == "<fu1@mail.gmail.com>"
    assert plan[0]["references"] == "<intro1@mail.gmail.com> <fu1@mail.gmail.com>"


# =============================================================================
# Full send_batch integration (fake Sheets + monkeypatched smtp_send)
# =============================================================================

class FakeSheets:
    def __init__(self, leads):
        self._leads = leads
        self.send_log = []
        self.responses = []
        self.error_log = []
        self._logged_ids = set()
        self.marked_read_calls = []
        self.header_widen_calls = []

    def get_all_leads(self):
        return [dict(lead) for lead in self._leads]

    def ensure_master_header_includes(self, column_names):
        self.header_widen_calls.append(list(column_names))

    def update_lead_fields(self, row_number, fields):
        for lead in self._leads:
            if lead["_row"] == row_number:
                lead.update(fields)

    def append_lead(self, fields):
        next_row = max((l["_row"] for l in self._leads), default=1) + 1
        new_lead = dict(fields)
        new_lead["_row"] = next_row
        self._leads.append(new_lead)

    def update_lead_statuses(self, row_numbers_to_status):
        for lead in self._leads:
            if lead["_row"] in row_numbers_to_status:
                lead["Status"] = row_numbers_to_status[lead["_row"]]
                lead["LastActionAt"] = "2026-08-01 09:00:00"  # fixed stub — tests don't assert this value

    def append_send_log(self, fields):
        self.send_log.append(fields)

    def append_response(self, fields):
        self.responses.append(fields)
        self._logged_ids.add(fields.get("MessageID", ""))

    def get_all_responses(self):
        # Row numbers assigned in append order, matching the real
        # SheetsConnector's "row 1 is header, data starts at row 2" rule.
        return [dict(r, _row=i) for i, r in enumerate(self.responses, start=2)]

    def mark_responses_read(self, response_id_to_row):
        self.marked_read_calls.append(dict(response_id_to_row))
        marked_rows = set(response_id_to_row.values())
        for i, r in enumerate(self.responses, start=2):
            if i in marked_rows:
                r["IsRead"] = "Yes"
        return len(response_id_to_row)

    def get_logged_message_ids(self):
        return set(self._logged_ids)

    def append_error_log(self, fields):
        self.error_log.append(fields)

    def get_all_send_log(self):
        return list(self.send_log)

    def get_all_error_log(self):
        return list(self.error_log)


def _base_campaign_cfg():
    return {
        "templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES,
        "sending": {"daily_limit": 100, "delay_min_minutes": 0, "delay_max_minutes": 0},
        "_campaign_name": "test_campaign", "_global_default_account": "sales1",
    }


def test_send_batch_raises_campaign_paused_error_before_touching_anything(monkeypatch):
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1) or {"message_id": "<m>"})

    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "paused"
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    with pytest.raises(outreach.CampaignPausedError, match="paused"):
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert smtp_called == []  # never even got as far as resolving eligible leads


def test_send_batch_raises_for_draft_status_before_touching_anything(monkeypatch):
    """The real gap this covers: a Draft campaign's stored status is
    "draft", not "paused" — the original guard only checked for "paused"
    literally, so a Draft campaign could technically be sent if
    send_batch.yml were triggered directly, bypassing the UI's own
    Launch/Pause/Resume gating entirely."""
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1) or {"message_id": "<m>"})

    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "draft"
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    with pytest.raises(outreach.CampaignPausedError, match="draft"):
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert smtp_called == []


def test_send_batch_raises_for_deleted_status_before_touching_anything(monkeypatch):
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1) or {"message_id": "<m>"})

    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "deleted"
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    with pytest.raises(outreach.CampaignPausedError, match="deleted"):
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert smtp_called == []


def test_build_batch_ignores_draft_status_preview_still_works(monkeypatch):
    # Preview/build_batch must stay usable for a Draft campaign too —
    # only send_batch is gated, same principle as Paused.
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "S", "body": "B", "missing_variables": [], "thread_subject": "S", "is_continuation": False})
    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "draft"
    lead = make_lead(_row=2)
    plan = outreach.build_batch(campaign_cfg, [lead], "intro", 10)
    assert len(plan) == 1


def test_send_batch_active_status_sends_normally(monkeypatch):
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "active"
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent"


def test_send_batch_missing_status_key_sends_normally(monkeypatch):
    # _base_campaign_cfg() fixtures predate the "status" field entirely —
    # send_batch must not KeyError on campaign_cfg.get("status") being absent.
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    campaign_cfg = _base_campaign_cfg()
    assert "status" not in campaign_cfg
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])
    # Doesn't raise CampaignPausedError just because "status" key is missing.
    try:
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    except outreach.CampaignPausedError:
        pytest.fail("send_batch treated a missing 'status' key as paused")


def test_build_batch_ignores_paused_status_preview_still_works(monkeypatch):
    # Preview/build_batch must stay usable for a paused campaign — only
    # send_batch is gated.
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "S", "body": "B", "missing_variables": [], "thread_subject": "S", "is_continuation": False})
    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["status"] = "paused"
    lead = make_lead(_row=2)
    plan = outreach.build_batch(campaign_cfg, [lead], "intro", 10)
    assert len(plan) == 1


def test_send_batch_raises_outside_sending_window_error_before_touching_anything(monkeypatch):
    smtp_called = []
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: smtp_called.append(1) or {"message_id": "<m>"})

    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["schedule"] = {"timezone": "UTC"}  # content irrelevant — is_within_sending_window is mocked below
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    # Mocked rather than relying on a real "always closed" window, so this
    # test's outcome never depends on the real wall-clock time it happens
    # to run at — is_within_sending_window's own correctness is covered
    # exhaustively above; this test is specifically about send_batch's
    # handling of a False result.
    monkeypatch.setattr(outreach, "is_within_sending_window",
                         lambda schedule: (False, "outside for this test"))

    with pytest.raises(outreach.OutsideSendingWindowError, match="outside its configured sending window"):
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert smtp_called == []


def test_send_batch_within_sending_window_sends_normally(monkeypatch):
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["schedule"] = {"timezone": "UTC", "window_start": "00:00", "window_end": "23:59"}
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent"


def test_send_batch_no_schedule_key_sends_normally(monkeypatch):
    # _base_campaign_cfg() fixtures predate the "schedule" field entirely.
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})
    campaign_cfg = _base_campaign_cfg()
    assert "schedule" not in campaign_cfg
    lead = make_lead(_row=2, LeadID="L1", Email="a@abc.com")
    fake_sheets = FakeSheets([lead])

    try:
        outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    except outreach.OutsideSendingWindowError:
        pytest.fail("send_batch treated a missing 'schedule' key as a restriction")


def test_build_batch_ignores_schedule_restriction_preview_still_works(monkeypatch):
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "S", "body": "B", "missing_variables": [], "thread_subject": "S", "is_continuation": False})
    campaign_cfg = _base_campaign_cfg()
    campaign_cfg["schedule"] = {"timezone": "UTC", "window_start": "00:00", "window_end": "00:01"}
    lead = make_lead(_row=2)
    plan = outreach.build_batch(campaign_cfg, [lead], "intro", 10)
    assert len(plan) == 1



    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    def fake_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None,
                        smtp_host=None, smtp_port=None, smtp_username=None):
        return {"message_id": "<msg1@mail.gmail.com>"}

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert len(results) == 1
    assert results[0]["status"] == "sent"
    assert results[0]["batch_id"].startswith("BATCH-")
    assert results[0]["account"] == "sales1"

    assert len(fake_sheets.send_log) == 1
    assert fake_sheets.send_log[0]["Status"] == "sent"
    assert fake_sheets.send_log[0]["SenderAccount"] == "sales1"

    updated = fake_sheets._leads[0]
    assert updated["IntroSentAt"]
    assert updated["NextEligibleAt"]
    assert updated["LastActionAt"]
    assert updated["MessageID"] == "<msg1@mail.gmail.com>"
    assert updated["SenderAccount"] == "sales1"  # locked in for future stages


def test_send_batch_locks_in_resolved_account_for_reuse():
    # After a lead's SenderAccount is written back, resolve_sender_account
    # should use it directly rather than re-resolving the default.
    lead = make_lead(SenderAccount="sales1")
    campaign_cfg = {"_global_default_account": "sales2"}  # different default, deliberately
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales1"


def test_send_batch_error_isolation_does_not_write_send_at(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    def failing_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None):
        raise RuntimeError("simulated SMTP failure")

    monkeypatch.setattr(outreach, "smtp_send", failing_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert fake_sheets.send_log[0]["Status"] == "error"
    assert fake_sheets._leads[0]["IntroSentAt"] == ""
    assert fake_sheets._leads[0]["Error"]


def test_send_batch_unknown_sender_account_is_isolated_per_lead(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", SenderAccount="ghost_account")]
    fake_sheets = FakeSheets(leads)

    def fake_smtp_send(*a, **kw):
        raise AssertionError("smtp_send should never be called for an unresolvable account")

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert "ghost_account" in results[0]["error"]


# =============================================================================
# check_replies — header-based match preferred over email-address match
# =============================================================================

def test_check_replies_matches_by_message_id_header_first(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply1@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello",
            "from": "someone-else@notjohn.com",  # deliberately NOT john's address
            "headers": {}, "body": "sure, let's talk", "snippet": "sure, let's talk",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                                      campaign_name="test_campaign")
    assert len(actions) == 1
    assert actions[0]["lead_id"] == "L1"
    assert actions[0]["match_method"] == "Header"
    assert actions[0]["classification"] == outreach.CLASSIFICATION_GENUINE
    assert fake_sheets.responses[0]["Campaign"] == "test_campaign"
    assert fake_sheets.responses[0]["MatchMethod"] == "Header"


def test_check_replies_falls_back_to_email_when_no_header_match(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply2@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "sounds good", "snippet": "sounds good", "date": None,
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    assert len(actions) == 1
    assert actions[0]["match_method"] == "Email"
    # An Email-only match must NEVER stop a sequence, even when the content
    # would otherwise classify as a genuine reply — this is the exact
    # production bug this suite guards against (see the tests below).
    assert actions[0]["classification"] == outreach.CLASSIFICATION_GENUINE
    assert actions[0]["action"] == outreach.ACTION_LOGGED_UNVERIFIED
    updated_lead = fake_sheets._leads[0]
    assert updated_lead["ReplyStatus"] != "Replied"
    assert updated_lead.get("Status", "") != outreach.STATUS_STOPPED_REPLIED


def test_check_replies_genuine_reply_stops_sequence(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "yes let's talk", "snippet": "yes let's talk",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    updated = fake_sheets._leads[0]
    assert updated["Status"] == outreach.STATUS_STOPPED_REPLIED
    assert updated["ReplyStatus"] == "Replied"
    assert updated["LastActionAt"]


def test_check_replies_logs_full_untruncated_body_alongside_snippet(monkeypatch):
    """The real point of storing FullBody: a conversation view needs the
    complete message, not the 500-char preview Snippet has always been."""
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)
    long_body = "This is a much longer reply. " * 50  # well over 500 chars

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": long_body, "snippet": long_body[:500],
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)
    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)

    assert len(fake_sheets.responses) == 1
    logged = fake_sheets.responses[0]
    assert logged["FullBody"] == long_body  # complete, not truncated
    assert logged["Snippet"] == long_body[:500]  # unchanged, still a short preview
    assert len(logged["FullBody"]) > len(logged["Snippet"])


def test_check_replies_full_body_capped_at_sheets_cell_limit(monkeypatch):
    """A single Google Sheets cell caps at 50,000 characters — an
    unusually long email must be capped, not sent as a request Google
    would reject outright."""
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)
    huge_body = "x" * 100_000

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": huge_body, "snippet": huge_body[:500],
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)
    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)

    assert len(fake_sheets.responses[0]["FullBody"]) <= 49000


# =============================================================================
# classify_reply_intent — a SEPARATE layer from mechanical classification,
# only ever meaningful for a Genuine Reply. Every test here mocks the
# actual HTTP call — no real Anthropic API traffic in the test suite.
# =============================================================================

def _fake_anthropic_response(intent, confidence, status_code=200):
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} error")

        def json(self):
            return {"content": [{"text": json.dumps({"intent": intent, "confidence": confidence})}]}

    return FakeResponse()


def test_classify_reply_intent_no_api_key_returns_unclear_without_calling_out(monkeypatch):
    call_count = []
    monkeypatch.setattr(outreach.requests, "post", lambda *a, **kw: call_count.append(1))
    result = outreach.classify_reply_intent("Yes I'm interested!", api_key="")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}
    assert call_count == []  # never even attempted a call with no key configured


def test_classify_reply_intent_interested(monkeypatch):
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Interested", "High"))
    result = outreach.classify_reply_intent("Yes, I'd love to hear more. What are the next steps?",
                                             api_key="fake-key")
    assert result == {"intent": outreach.INTENT_INTERESTED, "confidence": "High"}


def test_classify_reply_intent_not_interested(monkeypatch):
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Not Interested", "High"))
    result = outreach.classify_reply_intent("Thanks, but we're not looking for this.", api_key="fake-key")
    assert result["intent"] == outreach.INTENT_NOT_INTERESTED


def test_classify_reply_intent_defer_later_is_lead_followup_not_not_interested(monkeypatch):
    """The exact nuance this whole feature exists to capture — a
    'not now, later' reply is a real lead, not a lost one."""
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Lead / Needs Follow-up", "Medium"))
    result = outreach.classify_reply_intent(
        "Not interested right now, but reach out again in a few months.", api_key="fake-key")
    assert result["intent"] == outreach.INTENT_LEAD_FOLLOWUP
    assert result["intent"] != outreach.INTENT_NOT_INTERESTED


def test_classify_reply_intent_low_confidence_downgrades_to_unclear(monkeypatch):
    """Never trust a low-confidence result at face value — even if the
    model's raw guess was a real category, Low confidence means the
    safe, honest answer is 'we don't know', not that specific guess."""
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Interested", "Low"))
    result = outreach.classify_reply_intent("hmm maybe idk", api_key="fake-key")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}


def test_classify_reply_intent_invalid_category_from_model_falls_back_to_unclear(monkeypatch):
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Something Weird", "High"))
    result = outreach.classify_reply_intent("...", api_key="fake-key")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}


def test_classify_reply_intent_api_error_falls_back_to_unclear_never_raises(monkeypatch):
    monkeypatch.setattr(outreach.requests, "post",
                         lambda *a, **kw: _fake_anthropic_response("Interested", "High", status_code=500))
    result = outreach.classify_reply_intent("test", api_key="fake-key")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}


def test_classify_reply_intent_network_exception_falls_back_to_unclear_never_raises(monkeypatch):
    def raise_network_error(*a, **kw):
        raise requests.ConnectionError("simulated network failure")

    monkeypatch.setattr(outreach.requests, "post", raise_network_error)
    result = outreach.classify_reply_intent("test", api_key="fake-key")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}


def test_classify_reply_intent_malformed_json_response_falls_back_to_unclear(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": "not valid json at all"}]}

    monkeypatch.setattr(outreach.requests, "post", lambda *a, **kw: FakeResponse())
    result = outreach.classify_reply_intent("test", api_key="fake-key")
    assert result == {"intent": outreach.INTENT_UNCLEAR, "confidence": "Low"}


def test_classify_reply_intent_truncates_very_long_body_before_sending():
    """A real cost/latency guard — a 3000-char cap on what's actually
    sent to the model, regardless of how long the original email is."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": json.dumps({"intent": "Unclear", "confidence": "Low"})}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["prompt"] = json["messages"][0]["content"]
        return FakeResponse()

    import unittest.mock as mock
    with mock.patch.object(outreach.requests, "post", fake_post):
        outreach.classify_reply_intent("x" * 10_000, api_key="fake-key")

    # The prompt overhead itself is well under 3000 chars, so a truncated
    # body keeps the whole prompt well short of the full 10,000 chars.
    assert len(captured["prompt"]) < 4000


# =============================================================================
# check_replies' actual wiring of intent classification
# =============================================================================

def test_check_replies_classifies_intent_for_genuine_reply_when_key_provided(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "Yes I'm very interested, let's talk!", "snippet": "Yes I'm very interested",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)
    monkeypatch.setattr(outreach, "classify_reply_intent",
                         lambda body, api_key: {"intent": outreach.INTENT_INTERESTED, "confidence": "High"})

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                            anthropic_api_key="fake-key")

    logged = fake_sheets.responses[0]
    assert logged["Intent"] == outreach.INTENT_INTERESTED
    assert logged["IntentConfidence"] == "High"
    assert logged["IntentClassifiedAt"]  # non-empty


def test_check_replies_no_api_key_leaves_intent_fields_blank(monkeypatch):
    """The opt-in guarantee — check_replies behaves exactly as it always
    has when this feature isn't configured, not just 'roughly the same'."""
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)
    intent_calls = []

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "Yes interested!", "snippet": "Yes interested!",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)
    monkeypatch.setattr(outreach, "classify_reply_intent", lambda body, api_key: intent_calls.append(1))

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                            anthropic_api_key=None)

    assert intent_calls == []  # never even called
    logged = fake_sheets.responses[0]
    assert logged["Intent"] == ""
    assert logged["IntentConfidence"] == ""
    assert logged["IntentClassifiedAt"] == ""


def test_check_replies_never_classifies_intent_for_a_bounce(monkeypatch):
    """Intent is meaningless for a bounce/auto-reply/OOO — classifying
    one would waste an API call on something with no "sales intent" at
    all, and could produce a nonsensical result."""
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)
    intent_calls = []

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<bounce1@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Mail Delivery Failed", "from": "mailer-daemon@abc.com",
            "headers": {"content-type": "multipart/report; report-type=delivery-status"},
            "body": "550 no such user", "snippet": "550 no such user",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)
    monkeypatch.setattr(outreach, "classify_reply_intent", lambda body, api_key: intent_calls.append(1))

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                            anthropic_api_key="fake-key")

    assert intent_calls == []


def test_check_replies_one_account_imap_failure_does_not_block_others(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def flaky_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        if address == "sales1@gmail.com":
            raise RuntimeError("simulated IMAP outage")
        return [{
            "message_id": "<reply4@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "interested", "snippet": "interested",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", flaky_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, ACCOUNTS, lookback_hours=24)
    assert len(actions) == 1  # sales2's message still got processed despite sales1 failing
    assert actions[0]["account"] == "sales2"


# =============================================================================
# load_email_accounts
# =============================================================================

def test_load_email_accounts_parses_json(monkeypatch):
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON",
                        '{"sales1": {"address": "a@b.com", "app_password": "xxxx"}}')
    accounts = outreach.load_email_accounts()
    assert accounts["sales1"]["address"] == "a@b.com"


def test_load_email_accounts_missing_env_raises(monkeypatch):
    monkeypatch.delenv("EMAIL_ACCOUNTS_JSON", raising=False)
    try:
        outreach.load_email_accounts()
        assert False, "should have raised RuntimeError"
    except RuntimeError:
        pass


def test_load_email_accounts_rejects_incomplete_entry(monkeypatch):
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", '{"sales1": {"address": "a@b.com"}}')  # missing app_password
    try:
        outreach.load_email_accounts()
        assert False, "should have raised RuntimeError"
    except RuntimeError:
        pass


# =============================================================================
# Email account slots — the safe per-account secret model. Every test
# explicitly clears all slot env vars first/via monkeypatch's own
# isolation, so these never depend on what's left over from another test.
# =============================================================================

def _clear_all_slots(monkeypatch):
    for i in range(1, outreach.EMAIL_ACCOUNT_SLOT_COUNT + 1):
        monkeypatch.delenv(f"EMAIL_ACCOUNT_SLOT_{i}", raising=False)


def test_load_accounts_from_slots_empty_when_none_set(monkeypatch):
    _clear_all_slots(monkeypatch)
    assert outreach._load_email_accounts_from_slots() == {}


def test_load_accounts_from_slots_single_slot(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1",
                        '{"name": "sales1", "address": "sales1@gmail.com", "app_password": "xxxx"}')
    accounts = outreach._load_email_accounts_from_slots()
    assert accounts == {"sales1": {"address": "sales1@gmail.com", "app_password": "xxxx"}}


def test_load_accounts_from_slots_multiple_slots(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1",
                        '{"name": "sales1", "address": "sales1@gmail.com", "app_password": "xxxx"}')
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_3",
                        '{"name": "sales2", "address": "sales2@gmail.com", "app_password": "yyyy"}')
    accounts = outreach._load_email_accounts_from_slots()
    assert set(accounts.keys()) == {"sales1", "sales2"}


def test_load_accounts_from_slots_skips_empty_string_slots(monkeypatch):
    """A slot set to an empty string (as opposed to fully unset) — e.g.
    a secret that was cleared but the env var mechanism still defines it
    as "" — must be silently skipped, not treated as a populated account."""
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1", "")
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_2",
                        '{"name": "sales1", "address": "sales1@gmail.com", "app_password": "xxxx"}')
    accounts = outreach._load_email_accounts_from_slots()
    assert accounts == {"sales1": {"address": "sales1@gmail.com", "app_password": "xxxx"}}


def test_load_accounts_from_slots_invalid_json_raises_clear_error(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1", "not valid json{{{")
    with pytest.raises(RuntimeError, match="EMAIL_ACCOUNT_SLOT_1"):
        outreach._load_email_accounts_from_slots()


def test_load_accounts_from_slots_missing_field_raises(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1", '{"name": "sales1", "address": "sales1@gmail.com"}')  # no app_password
    with pytest.raises(RuntimeError, match="missing 'app_password'"):
        outreach._load_email_accounts_from_slots()


def test_load_email_accounts_slots_only_no_json_blob(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.delenv("EMAIL_ACCOUNTS_JSON", raising=False)
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1",
                        '{"name": "sales1", "address": "sales1@gmail.com", "app_password": "xxxx"}')
    accounts = outreach.load_email_accounts()
    assert accounts == {"sales1": {"address": "sales1@gmail.com", "app_password": "xxxx"}}


def test_load_email_accounts_json_only_no_slots_unchanged_behavior(monkeypatch):
    """The exact backward-compat guarantee — zero slots set, only the
    legacy blob, must behave identically to before this feature existed."""
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", '{"sales1": {"address": "a@b.com", "app_password": "xxxx"}}')
    accounts = outreach.load_email_accounts()
    assert accounts == {"sales1": {"address": "a@b.com", "app_password": "xxxx"}}


def test_load_email_accounts_merges_slots_and_json_blob(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", '{"sales_legacy": {"address": "legacy@b.com", "app_password": "old"}}')
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1",
                        '{"name": "sales_new", "address": "new@gmail.com", "app_password": "new_pass"}')
    accounts = outreach.load_email_accounts()
    assert set(accounts.keys()) == {"sales_legacy", "sales_new"}


def test_load_email_accounts_slot_wins_over_same_named_json_entry(monkeypatch):
    """The migration invariant that matters most: editing an account via
    a slot must take effect immediately, even while the same account name
    still lingers (stale) in EMAIL_ACCOUNTS_JSON during the transition."""
    _clear_all_slots(monkeypatch)
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON",
                        '{"sales1": {"address": "sales1@gmail.com", "app_password": "OLD_PASSWORD"}}')
    monkeypatch.setenv("EMAIL_ACCOUNT_SLOT_1",
                        '{"name": "sales1", "address": "sales1@gmail.com", "app_password": "NEW_PASSWORD"}')
    accounts = outreach.load_email_accounts()
    assert accounts["sales1"]["app_password"] == "NEW_PASSWORD"


def test_load_email_accounts_raises_when_neither_slots_nor_json_set(monkeypatch):
    _clear_all_slots(monkeypatch)
    monkeypatch.delenv("EMAIL_ACCOUNTS_JSON", raising=False)
    with pytest.raises(RuntimeError, match="No email accounts configured"):
        outreach.load_email_accounts()


# =============================================================================
# is_valid_email_format
# =============================================================================

def test_valid_email_formats():
    for addr in ["a@b.com", "john.doe+tag@sub.domain.co", "x@y.io"]:
        assert outreach.is_valid_email_format(addr), addr


def test_invalid_email_formats():
    for addr in ["", "no-at-sign", "a@b", "a @b.com", "a@b .com", "justtext"]:
        assert not outreach.is_valid_email_format(addr), addr


# =============================================================================
# classify_send_exception — error monitoring categories
# =============================================================================

def test_classify_missing_sender_account_error():
    exc = outreach.MissingSenderAccountError("nope")
    assert outreach.classify_send_exception(exc) == outreach.ERR_MISSING_SENDER_ACCOUNT


def test_classify_invalid_email_format_error():
    exc = outreach.InvalidEmailFormatError("bad format")
    assert outreach.classify_send_exception(exc) == outreach.ERR_INVALID_EMAIL


def test_classify_smtp_authentication_error():
    import smtplib
    exc = smtplib.SMTPAuthenticationError(535, b"Authentication failed")
    assert outreach.classify_send_exception(exc) == outreach.ERR_AUTH_FAILURE


def test_classify_smtp_recipients_refused():
    import smtplib
    exc = smtplib.SMTPRecipientsRefused({"bad@x.com": (550, b"No such user")})
    assert outreach.classify_send_exception(exc) == outreach.ERR_INVALID_EMAIL


def test_classify_smtp_rate_limit_by_code():
    import smtplib
    exc = smtplib.SMTPResponseException(454, b"4.7.0 Too many login attempts")
    assert outreach.classify_send_exception(exc) == outreach.ERR_RATE_LIMIT


def test_classify_smtp_rate_limit_by_keyword():
    import smtplib
    exc = smtplib.SMTPResponseException(552, b"rate limited, try again later")
    assert outreach.classify_send_exception(exc) == outreach.ERR_RATE_LIMIT


def test_classify_smtp_generic_response_exception_is_send_failure():
    import smtplib
    exc = smtplib.SMTPResponseException(552, b"message too large")
    assert outreach.classify_send_exception(exc) == outreach.ERR_SEND_FAILURE


def test_classify_generic_os_error_is_send_failure():
    exc = OSError("connection reset")
    assert outreach.classify_send_exception(exc) == outreach.ERR_SEND_FAILURE


# =============================================================================
# classify_imap_exception
# =============================================================================

def test_classify_imap_auth_failure_by_message():
    exc = Exception("b'AUTHENTICATIONFAILED Invalid credentials'")
    assert outreach.classify_imap_exception(exc) == outreach.ERR_AUTH_FAILURE


def test_classify_imap_generic_failure():
    exc = Exception("connection timed out")
    assert outreach.classify_imap_exception(exc) == outreach.ERR_REPLY_CHECK


# =============================================================================
# log_error
# =============================================================================

def test_log_error_appends_structured_entry():
    fake_sheets = FakeSheets([])
    outreach.log_error(fake_sheets, "camp1", outreach.ERR_SEND_FAILURE, "boom",
                        lead_id="L1", email_addr="a@b.com", stage="intro", batch_id="BATCH-1")
    assert len(fake_sheets.error_log) == 1
    entry = fake_sheets.error_log[0]
    assert entry["ErrorType"] == outreach.ERR_SEND_FAILURE
    assert entry["Message"] == "boom"
    assert entry["LeadID"] == "L1"
    assert entry["Campaign"] == "camp1"
    assert entry["BatchID"] == "BATCH-1"


def test_log_error_never_raises_even_if_sheet_write_fails():
    class BrokenSheets:
        def append_error_log(self, fields):
            raise RuntimeError("sheets down")

    # Must not raise — this is the whole point of log_error's own try/except.
    outreach.log_error(BrokenSheets(), "camp1", outreach.ERR_SEND_FAILURE, "boom")


# =============================================================================
# _get_or_create_ws — relaxed prefix-based header validation
# =============================================================================

class FakeGspreadExceptions:
    class WorksheetNotFound(Exception):
        pass


class FakeGspreadModule:
    exceptions = FakeGspreadExceptions


class FakeWs:
    def __init__(self, header=None):
        self._header = header if header is not None else []
        self.appended_rows = []
        self.updated_cells = []  # [(row, col, value), ...]

    def row_values(self, n):
        return self._header

    def append_row(self, row):
        self.appended_rows.append(row)
        if not self._header:
            self._header = row

    def update_cell(self, row, col, value):
        self.updated_cells.append((row, col, value))
        if row == 1:
            while len(self._header) < col:
                self._header.append("")
            self._header[col - 1] = value


class FakeSpreadsheet:
    def __init__(self, existing=None):
        self._existing = existing or {}
        self.added = {}

    def worksheet(self, title):
        if title in self._existing:
            return self._existing[title]
        raise FakeGspreadExceptions.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWs()
        self.added[title] = ws
        self._existing[title] = ws
        return ws


def test_get_or_create_ws_creates_new_tab_with_header():
    spreadsheet = FakeSpreadsheet()
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws.appended_rows == [["A", "B"]]
    assert "MyTab" in spreadsheet.added


def test_get_or_create_ws_auto_widens_header_when_new_required_column_added():
    """The actual migration case: a tab created before some new required
    column existed must get that column appended to its header, not
    raise an error — existing rows keep their values in their existing
    positions untouched, and only the header row changes."""
    existing_ws = FakeWs(header=["A", "B"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B", "C"])
    assert ws.row_values(1) == ["A", "B", "C"]
    assert existing_ws.updated_cells == [(1, 3, "C")]


def test_get_or_create_ws_auto_widens_multiple_missing_columns():
    existing_ws = FakeWs(header=["A"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B", "C"])
    assert existing_ws.row_values(1) == ["A", "B", "C"]
    assert existing_ws.updated_cells == [(1, 2, "B"), (1, 3, "C")]


def test_get_or_create_ws_auto_widen_never_touches_existing_column_names():
    """The existing columns' actual names must be preserved exactly —
    only cells STRICTLY AFTER the existing header length are written."""
    existing_ws = FakeWs(header=["A", "B"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B", "C"])
    assert all(row != 1 or col > 2 for row, col, _ in existing_ws.updated_cells)


def test_get_or_create_ws_reproduces_the_exact_reported_production_scenario():
    """The literal reported case: a real Master Sheet whose header
    already has MASTER_COLUMNS through AsanaTaskGID, followed by
    several real custom columns from earlier CSV imports (Ad Ready,
    Client, Content Score, etc.) — then ManualAsanaStage gets added as
    a new required column in the code. Must widen cleanly, appending
    ManualAsanaStage at the very end, never touching the custom columns
    already there."""
    custom_columns = ["Ad Ready", "Client", "Content Score", "Creator", "Last Contact Date",
                       "Name", "Product", "Refunnel Link", "Rights Expiration", "Usage Rights",
                       "Video File"]
    original_header = list(outreach.MASTER_COLUMNS[:-1]) + custom_columns  # everything but ManualAsanaStage
    existing_ws = FakeWs(header=list(original_header))  # a copy — FakeWs keeps a reference, not a copy,
                                                          # and widening mutates it in place
    spreadsheet = FakeSpreadsheet(existing={"Kelson_Creators_Licensing_03SEP Master Sheet": existing_ws})

    outreach._get_or_create_ws(spreadsheet, FakeGspreadModule,
                                "Kelson_Creators_Licensing_03SEP Master Sheet", outreach.MASTER_COLUMNS)

    assert existing_ws.row_values(1) == original_header + ["ManualAsanaStage"]
    # Every one of the 11 real custom columns must still be there, in the same order.
    for col in custom_columns:
        assert col in existing_ws.row_values(1)


def test_get_or_create_ws_widens_even_when_an_existing_column_is_out_of_position():
    """The actual reported production bug: a custom column (from an
    earlier CSV import) already sits exactly where a newly-added
    required column is now expected. Since every read/write in this
    system goes by column NAME, not position, this must still widen
    correctly by appending the missing required column — never raise,
    since position never actually mattered."""
    existing_ws = FakeWs(header=["A", "SomeCustomColumn"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B", "C"])
    assert existing_ws.row_values(1) == ["A", "SomeCustomColumn", "B", "C"]
    assert existing_ws.updated_cells == [(1, 3, "B"), (1, 4, "C")]


def test_get_or_create_ws_raises_only_when_header_shares_nothing_with_required():
    """The one case that still fails loudly — a tab that shares NONE of
    the expected columns at all almost certainly isn't the right tab
    for this campaign (e.g. a config pointed at the wrong sheet/tab
    entirely), not just one that's picked up a few custom columns."""
    existing_ws = FakeWs(header=["Completely", "Unrelated", "Columns"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    with pytest.raises(RuntimeError, match="shares none of the expected columns"):
        outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B", "C"])


def test_get_or_create_ws_accepts_a_custom_column_that_happens_to_precede_a_required_one():
    """Same scenario as the reported bug, phrased the other way: even a
    single matching required column ('A' here) plus one differently-
    named custom column must be enough to widen safely, not raise."""
    existing_ws = FakeWs(header=["A", "WrongColumn"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert existing_ws.row_values(1) == ["A", "WrongColumn", "B"]


def test_get_or_create_ws_accepts_extra_trailing_custom_columns():
    existing_ws = FakeWs(header=["A", "B", "Industry", "JobTitle"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws._real is existing_ws  # accepted as-is, no error, custom columns preserved
    # ...and wrapped for retry protection, not the raw object directly.
    assert isinstance(ws, outreach._RetryingWorksheet)


# =============================================================================
# gspread retry logic — fixes a real production issue: a transient 503
# from Google's own service (or a 429 rate limit) was previously fatal to
# the whole workflow run, even though it's exactly the kind of error
# worth simply waiting out and retrying.
# =============================================================================

def _make_gspread_api_error(status_code):
    class _FakeResponse:
        def __init__(self, code):
            self.status_code = code

        def json(self):
            return {"error": {"code": self.status_code, "message": "test error"}}

    return outreach.gspread.exceptions.APIError(_FakeResponse(status_code))


def test_call_with_gspread_retries_succeeds_immediately_no_retry_needed(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(outreach.time, "sleep", lambda s: sleep_calls.append(s))
    result = outreach._call_with_gspread_retries(lambda: "ok")
    assert result == "ok"
    assert sleep_calls == []


def test_call_with_gspread_retries_retries_on_503_then_succeeds(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr(outreach.time, "sleep", lambda s: sleep_calls.append(s))
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise _make_gspread_api_error(503)
        return "finally worked"

    result = outreach._call_with_gspread_retries(flaky)
    assert result == "finally worked"
    assert attempts["count"] == 3
    assert sleep_calls == [2, 4]  # exponential backoff: 2s, then 4s


def test_call_with_gspread_retries_retries_on_429_rate_limit(monkeypatch):
    monkeypatch.setattr(outreach.time, "sleep", lambda s: None)
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise _make_gspread_api_error(429)
        return "ok"

    assert outreach._call_with_gspread_retries(flaky) == "ok"


def test_call_with_gspread_retries_raises_after_max_retries_exhausted(monkeypatch):
    monkeypatch.setattr(outreach.time, "sleep", lambda s: None)
    attempts = {"count": 0}

    def always_fails():
        attempts["count"] += 1
        raise _make_gspread_api_error(503)

    with pytest.raises(outreach.gspread.exceptions.APIError):
        outreach._call_with_gspread_retries(always_fails)
    assert attempts["count"] == outreach.GSPREAD_MAX_RETRIES + 1  # every attempt actually happened


def test_call_with_gspread_retries_never_retries_non_retryable_error(monkeypatch):
    """A 403 Forbidden is a real permissions problem — retrying it would
    just waste up to 14 seconds on something that will never succeed."""
    sleep_calls = []
    monkeypatch.setattr(outreach.time, "sleep", lambda s: sleep_calls.append(s))
    attempts = {"count": 0}

    def fails_with_403():
        attempts["count"] += 1
        raise _make_gspread_api_error(403)

    with pytest.raises(outreach.gspread.exceptions.APIError):
        outreach._call_with_gspread_retries(fails_with_403)
    assert attempts["count"] == 1  # never retried
    assert sleep_calls == []


def test_retrying_worksheet_retries_a_flaky_method_call(monkeypatch):
    monkeypatch.setattr(outreach.time, "sleep", lambda s: None)

    class FlakyRealWorksheet:
        def __init__(self):
            self.attempts = 0

        def get_all_records(self):
            self.attempts += 1
            if self.attempts < 2:
                raise _make_gspread_api_error(503)
            return [{"a": 1}]

    real = FlakyRealWorksheet()
    wrapped = outreach._RetryingWorksheet(real)
    assert wrapped.get_all_records() == [{"a": 1}]
    assert real.attempts == 2


def test_retrying_worksheet_passes_through_non_callable_attributes():
    class RealWorksheet:
        title = "My Tab"

    wrapped = outreach._RetryingWorksheet(RealWorksheet())
    assert wrapped.title == "My Tab"  # plain attribute, not wrapped into a callable


def test_retrying_worksheet_propagates_non_retryable_error_immediately():
    class RealWorksheet:
        def get_all_records(self):
            raise _make_gspread_api_error(404)

    wrapped = outreach._RetryingWorksheet(RealWorksheet())
    with pytest.raises(outreach.gspread.exceptions.APIError):
        wrapped.get_all_records()


def test_get_or_create_ws_zero_overlap_still_raises_for_a_totally_wrong_tab():
    existing_ws = FakeWs(header=["Nothing", "Matches"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    with pytest.raises(RuntimeError, match="shares none of the expected columns"):
        outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])


def test_get_or_create_ws_fills_header_on_blank_existing_tab():
    existing_ws = FakeWs(header=[])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws.appended_rows == [["A", "B"]]


# =============================================================================
# write_dashboard / write_all_campaigns_dashboard
# =============================================================================

class FakeDashboardWs:
    def __init__(self):
        self.cleared = False
        self.updated_values = None
        self.updated_range = None

    def clear(self):
        self.cleared = True

    def update(self, values, range_name=None):
        self.updated_values = values
        self.updated_range = range_name


def test_write_dashboard_clears_then_writes_header_and_rows():
    ws = FakeDashboardWs()
    rows = [("Overview", "Total Leads", "5"), ("Overview", "Total Sent", "3")]
    outreach.write_dashboard(ws, rows)
    assert ws.cleared is True
    assert ws.updated_values[0] == outreach.DASHBOARD_COLUMNS
    assert ws.updated_values[1] == ["Overview", "Total Leads", "5"]
    assert ws.updated_values[2] == ["Overview", "Total Sent", "3"]


def test_write_all_campaigns_dashboard_clears_then_writes():
    ws = FakeDashboardWs()
    rows = [["camp1", "10", "8", "8", "7", "1", "0", "2", "25.0%", "50.0%"]]
    outreach.write_all_campaigns_dashboard(ws, rows)
    assert ws.cleared is True
    assert ws.updated_values[0] == outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS
    assert ws.updated_values[1] == rows[0]


# =============================================================================
# compute_campaign_dashboard — full synthetic scenario
# =============================================================================

DASH_STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
    {"name": "followup2", "template_prefix": "followup2", "wait_days_after_previous": 4},
]


def _dash_lead(**overrides):
    lead = {
        "_row": 2, "LeadID": "", "Email": "", "Approval": "Yes", "Status": "", "ReplyStatus": "",
        "CurrentStage": "", "SenderAccount": "",
        "IntroSentAt": "", "IntroVariant": "",
        "FollowUp1SentAt": "", "FollowUp1Variant": "",
        "FollowUp2SentAt": "", "FollowUp2Variant": "",
    }
    lead.update(overrides)
    return lead


def _rows_to_dict(rows):
    return {(r[0], r[1]): r[2] for r in rows}


def test_compute_campaign_dashboard_full_scenario():
    ts = "2026-08-20 10:00:00"
    leads = [
        _dash_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=ts, IntroVariant="A",
                   SenderAccount="sales1", CurrentStage="intro", Status="intro Sent"),
        _dash_lead(_row=3, LeadID="L2", Email="jane@abc.com", IntroSentAt=ts, IntroVariant="B",
                   FollowUp1SentAt=ts, FollowUp1Variant="A", FollowUp2SentAt=ts, FollowUp2Variant="A",
                   SenderAccount="sales1", CurrentStage="followup2", Status="followup2 Sent"),
        _dash_lead(_row=4, LeadID="L3", Email="bob@xyz.com", IntroSentAt=ts, IntroVariant="A",
                   SenderAccount="sales2", CurrentStage="intro", Status=outreach.STATUS_STOPPED_REPLIED,
                   ReplyStatus="Replied"),
        _dash_lead(_row=5, LeadID="L4", Email=""),  # no email — excluded from total_leads
    ]
    send_log = [
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales2"},
        {"Status": "error", "SenderAccount": "sales1"},  # must NOT count toward total_sent
    ]
    responses = [
        {"Classification": outreach.CLASSIFICATION_GENUINE},
        {"Classification": outreach.CLASSIFICATION_BOUNCE_HARD},
        {"Classification": outreach.CLASSIFICATION_BOUNCE_SOFT},
    ]
    error_log = [
        {"Timestamp": "t1", "ErrorType": outreach.ERR_SEND_FAILURE, "Message": "boom1"},
        {"Timestamp": "t2", "ErrorType": outreach.ERR_SEND_FAILURE, "Message": "boom2"},
        {"Timestamp": "t3", "ErrorType": outreach.ERR_INVALID_EMAIL, "Message": "bad@"},
    ]
    campaign_cfg = {"stages": DASH_STAGES}

    rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)
    d = _rows_to_dict(rows)

    assert d[("Overview", "Total Leads (with Email)")] == "3"
    assert d[("Overview", "Unique Leads Contacted")] == "3"
    assert d[("Overview", "Total Emails Sent")] == "4"
    assert d[("Overview", "Delivered (est. = Sent minus Hard Bounces)")] == "3"
    assert d[("Overview", "Bounced (Hard)")] == "1"
    assert d[("Overview", "Bounced (Soft)")] == "1"
    assert d[("Overview", "Genuine Replies")] == "1"
    assert d[("Overview", "Reply Rate (Replies / Unique Contacted)")] == "33.3%"
    assert d[("Overview", "Sequence Completion (Reached Final Stage / Unique Contacted)")] == "33.3%"

    assert d[("Per-Stage", "intro - Sent")] == "3"
    assert d[("Per-Stage", "followup1 - Sent")] == "1"
    assert d[("Per-Stage", "followup2 - Sent")] == "1"

    assert d[("Sender Performance", "sales1 - Sent")] == "3"
    assert d[("Sender Performance", "sales1 - Replies")] == "0"
    assert d[("Sender Performance", "sales2 - Sent")] == "1"
    assert d[("Sender Performance", "sales2 - Replies")] == "1"
    assert d[("Sender Performance", "sales2 - Reply Rate")] == "100.0%"

    assert d[("Variant Performance", "intro-A - Sent")] == "2"
    assert d[("Variant Performance", "intro-A - Replies (approx.)")] == "1"
    assert d[("Variant Performance", "intro-B - Sent")] == "1"
    assert d[("Variant Performance", "intro-B - Replies (approx.)")] == "0"

    assert d[("Errors (All Time)", outreach.ERR_SEND_FAILURE)] == "2"
    assert d[("Errors (All Time)", outreach.ERR_INVALID_EMAIL)] == "1"


def test_compute_all_campaigns_row_matches_column_order():
    ts = "2026-08-20 10:00:00"
    leads = [_dash_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=ts)]
    send_log = [{"Status": "sent", "SenderAccount": "sales1"}]
    responses = []
    row = outreach.compute_all_campaigns_row("mycamp", leads, responses, send_log, DASH_STAGES)
    assert len(row) == len(outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS)
    assert row[0] == "mycamp"
    assert row[1] == "1"  # Total Leads
    assert row[2] == "1"  # Unique Contacted
    assert row[3] == "1"  # Total Sent


# =============================================================================
# send_batch — new error-monitoring integration paths
# =============================================================================

def test_send_batch_invalid_email_format_never_calls_smtp(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="not-a-valid-email")]
    fake_sheets = FakeSheets(leads)

    def should_not_be_called(*a, **kw):
        raise AssertionError("smtp_send should never be called for an invalid email format")

    monkeypatch.setattr(outreach, "smtp_send", should_not_be_called)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == outreach.ERR_INVALID_EMAIL
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_INVALID_EMAIL


def test_send_batch_unknown_sender_account_classified_correctly(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", SenderAccount="ghost_account")]
    fake_sheets = FakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")))

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == outreach.ERR_MISSING_SENDER_ACCOUNT
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_MISSING_SENDER_ACCOUNT


def test_send_batch_logs_missing_template_variable_after_successful_send(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<msg1@mail.gmail.com>"})
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "Hi", "body": "Body", "missing_variables": ["Industry"],
        "thread_subject": "Hi", "is_continuation": False,
    })

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent"
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_MISSING_VARIABLE
    assert "Industry" in fake_sheets.error_log[0]["Message"]


class RaisingUpdateFakeSheets(FakeSheets):
    def update_lead_fields(self, row_number, fields):
        raise RuntimeError("sheets down")


def test_send_batch_sent_but_sheet_error_when_sheet_write_fails_after_send(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = RaisingUpdateFakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<msg1@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent_but_sheet_error"
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_SHEETS_API
    assert "sent successfully" in fake_sheets.error_log[0]["Message"].lower()


# =============================================================================
# check_replies — IMAP failures now also logged to Error Log
# =============================================================================

def test_check_replies_imap_failure_logs_to_error_log(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def flaky_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        raise RuntimeError("simulated IMAP outage")

    monkeypatch.setattr(outreach, "imap_fetch_recent", flaky_imap_fetch_recent)

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                            campaign_name="test_campaign")
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_REPLY_CHECK
    assert fake_sheets.error_log[0]["Campaign"] == "test_campaign"


# =============================================================================
# get_rotation_accounts
# =============================================================================

ROTATION_ACCOUNTS = {
    "sales1": {"address": "sales1@gmail.com", "app_password": "aaaa"},
    "sales2": {"address": "sales2@gmail.com", "app_password": "bbbb"},
    "sales3": {"address": "sales3@gmail.com", "app_password": "cccc"},
}


def test_get_rotation_accounts_defaults_to_all_accounts():
    campaign_cfg = {"sending": {}}
    result = outreach.get_rotation_accounts(campaign_cfg, ROTATION_ACCOUNTS)
    assert set(result) == {"sales1", "sales2", "sales3"}


def test_get_rotation_accounts_respects_explicit_subset():
    campaign_cfg = {"sending": {"rotation_accounts": ["sales1", "sales3"]}}
    result = outreach.get_rotation_accounts(campaign_cfg, ROTATION_ACCOUNTS)
    assert set(result) == {"sales1", "sales3"}


def test_get_rotation_accounts_filters_out_unknown_names():
    campaign_cfg = {"sending": {"rotation_accounts": ["sales1", "does_not_exist"]}}
    result = outreach.get_rotation_accounts(campaign_cfg, ROTATION_ACCOUNTS)
    assert result == ["sales1"]


# =============================================================================
# pick_rotation_account
# =============================================================================

def test_pick_rotation_account_picks_least_used():
    result = outreach.pick_rotation_account(
        ["sales1", "sales2", "sales3"], None, {"sales1": 5, "sales2": 1, "sales3": 5}, {})
    assert result == "sales2"


def test_pick_rotation_account_excludes_accounts_at_capacity():
    result = outreach.pick_rotation_account(
        ["sales1", "sales2"], 3, {"sales1": 3, "sales2": 1}, {})
    assert result == "sales2"  # sales1 is at its cap of 3


def test_pick_rotation_account_returns_none_when_all_at_capacity():
    result = outreach.pick_rotation_account(
        ["sales1", "sales2"], 3, {"sales1": 3, "sales2": 3}, {})
    assert result is None


def test_pick_rotation_account_counts_in_batch_assignments_too():
    # sales1 has 0 from SendLog but already got 3 assigned earlier THIS batch
    result = outreach.pick_rotation_account(
        ["sales1", "sales2"], 3, {"sales1": 0, "sales2": 0}, {"sales1": 3})
    assert result == "sales2"


def test_pick_rotation_account_no_limit_never_excludes():
    result = outreach.pick_rotation_account(["sales1", "sales2"], None, {"sales1": 100}, {})
    assert result == "sales2"  # still picks least-used even with huge counts, since no cap


# =============================================================================
# _check_account_capacity
# =============================================================================

def test_check_account_capacity_no_limit_never_raises():
    outreach._check_account_capacity("sales1", None, {"sales1": 999}, {})  # should not raise


def test_check_account_capacity_under_limit_does_not_raise():
    outreach._check_account_capacity("sales1", 5, {"sales1": 3}, {})  # should not raise


def test_check_account_capacity_at_limit_raises():
    try:
        outreach._check_account_capacity("sales1", 5, {"sales1": 5}, {})
        assert False, "should have raised SenderCapacityReachedError"
    except outreach.SenderCapacityReachedError:
        pass


# =============================================================================
# resolve_sender_account_for_send
# =============================================================================

def test_resolve_for_send_manual_override_wins_even_with_rotation_on():
    lead = make_lead(SenderAccount="sales3")
    campaign_cfg = {"_global_default_account": "sales1", "sending": {"sender_rotation": True}}
    result = outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, {}, {})
    assert result == "sales3"


def test_resolve_for_send_manual_override_respects_capacity_not_silently_rerouted():
    lead = make_lead(SenderAccount="sales1")
    campaign_cfg = {"_global_default_account": "sales2", "sending": {"per_account_daily_limit": 2}}
    sent_today = {"sales1": 2}  # sales1 already at its cap
    try:
        outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, sent_today, {})
        assert False, "should have raised SenderCapacityReachedError"
    except outreach.SenderCapacityReachedError:
        pass  # must NOT silently fall back to sales2


def test_resolve_for_send_rotation_picks_least_used_when_no_override():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1", "sending": {"sender_rotation": True}}
    sent_today = {"sales1": 10, "sales2": 0, "sales3": 5}
    result = outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, sent_today, {})
    assert result == "sales2"


def test_resolve_for_send_rotation_all_at_capacity_raises_capacity_error():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1",
                     "sending": {"sender_rotation": True, "per_account_daily_limit": 2}}
    sent_today = {"sales1": 2, "sales2": 2, "sales3": 2}
    try:
        outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, sent_today, {})
        assert False, "should have raised SenderCapacityReachedError"
    except outreach.SenderCapacityReachedError:
        pass


def test_resolve_for_send_rotation_off_falls_back_to_single_default():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales2", "sending": {}}
    result = outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, {}, {})
    assert result == "sales2"


def test_resolve_for_send_rotation_respects_whitelist():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1",
                     "sending": {"sender_rotation": True, "rotation_accounts": ["sales2", "sales3"]}}
    sent_today = {"sales1": 0, "sales2": 5, "sales3": 5}  # sales1 has lowest usage but isn't in the whitelist
    result = outreach.resolve_sender_account_for_send(lead, campaign_cfg, ROTATION_ACCOUNTS, sent_today, {})
    assert result in ("sales2", "sales3")


# =============================================================================
# _count_sent_today_by_account
# =============================================================================

def test_count_sent_today_by_account_counts_only_sent_status_today():
    today = datetime.now().strftime(outreach.DATETIME_FMT)
    yesterday = (datetime.now() - timedelta(days=1)).strftime(outreach.DATETIME_FMT)
    send_log = [
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": today},
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": today},
        {"Status": "sent", "SenderAccount": "sales2", "Timestamp": today},
        {"Status": "error", "SenderAccount": "sales1", "Timestamp": today},   # excluded — not "sent"
        {"Status": "skipped", "SenderAccount": "sales1", "Timestamp": today},  # excluded — not "sent"
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": yesterday},  # excluded — not today
    ]
    counts = outreach._count_sent_today_by_account(send_log)
    assert counts == {"sales1": 2, "sales2": 1}


# =============================================================================
# classify_send_exception — new category
# =============================================================================

def test_classify_sender_capacity_reached_error():
    exc = outreach.SenderCapacityReachedError("all full")
    assert outreach.classify_send_exception(exc) == outreach.ERR_SENDER_CAPACITY


# =============================================================================
# Config validation — new sending keys
# =============================================================================

def test_validate_campaign_rejects_non_positive_per_account_daily_limit(tmp_path):
    override = "sending:\n  per_account_daily_limit: 0\n"
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


def test_validate_campaign_accepts_valid_rotation_config(tmp_path):
    override = (
        "sending:\n"
        "  per_account_daily_limit: 5\n"
        "  sender_rotation: true\n"
        "  rotation_accounts: [\"sales1\", \"sales2\"]\n"
    )
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    cfg = outreach.get_campaign("test_campaign", settings_path=settings_path,
                                 campaigns_dir=campaigns_dir, templates_root=templates_root)
    assert cfg["sending"]["per_account_daily_limit"] == 5
    assert cfg["sending"]["sender_rotation"] is True
    assert cfg["sending"]["rotation_accounts"] == ["sales1", "sales2"]


def test_validate_campaign_rejects_non_bool_sender_rotation(tmp_path):
    override = 'sending:\n  sender_rotation: "yes please"\n'
    settings_path, campaigns_dir, templates_root = _make_config_fixture(tmp_path, override_yaml=override)
    try:
        outreach.get_campaign("test_campaign", settings_path=settings_path,
                               campaigns_dir=campaigns_dir, templates_root=templates_root)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


# =============================================================================
# send_batch — rotation and per-account limit integration
# =============================================================================

def _rotation_campaign_cfg(**sending_overrides):
    sending = {"daily_limit": 100, "delay_min_minutes": 0, "delay_max_minutes": 0}
    sending.update(sending_overrides)
    return {
        "templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES,
        "sending": sending, "_campaign_name": "test_campaign", "_global_default_account": "sales1",
    }


def test_send_batch_rotates_across_accounts_with_no_manual_assignment(monkeypatch):
    campaign_cfg = _rotation_campaign_cfg(sender_rotation=True)
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
        make_lead(_row=5, LeadID="L4", Email="d@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    sent = [r for r in results if r["status"] == "sent"]
    assert len(sent) == 4
    accounts_used = [r["account"] for r in sent]
    # Balanced rotation across 3 accounts for 4 sends: no account should be
    # used more than twice (ceil(4/3) = 2).
    for acct in ROTATION_ACCOUNTS:
        assert accounts_used.count(acct) <= 2
    # Every lead's SenderAccount must have been written back (locked in).
    for lead in fake_sheets._leads:
        assert lead["SenderAccount"] in ROTATION_ACCOUNTS


def test_send_batch_skips_lead_when_all_rotation_accounts_at_capacity(monkeypatch):
    campaign_cfg = _rotation_campaign_cfg(sender_rotation=True, per_account_daily_limit=1,
                                           rotation_accounts=["sales1", "sales2"])
    today = datetime.now().strftime(outreach.DATETIME_FMT)
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    # Pre-seed today's SendLog as if sales1 and sales2 already each sent once today.
    fake_sheets.send_log = [
        {"Status": "sent", "SenderAccount": "sales1", "Timestamp": today},
        {"Status": "sent", "SenderAccount": "sales2", "Timestamp": today},
    ]
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    # All 3 leads should be skipped since both accounts are already at their
    # cap of 1 for today (sales3 isn't in the whitelist).
    assert all(r["status"] == "skipped" for r in results)
    assert all(r["error_type"] == outreach.ERR_SENDER_CAPACITY for r in results)


def test_send_batch_manual_assignment_skipped_not_rerouted_when_at_capacity(monkeypatch):
    campaign_cfg = _rotation_campaign_cfg(per_account_daily_limit=1)
    today = datetime.now().strftime(outreach.DATETIME_FMT)
    leads = [make_lead(_row=2, LeadID="L1", Email="a@abc.com", SenderAccount="sales1")]
    fake_sheets = FakeSheets(leads)
    fake_sheets.send_log = [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": today}]

    def should_not_be_called(*a, **kw):
        raise AssertionError("smtp_send should not be called — sales1 is at capacity")

    monkeypatch.setattr(outreach, "smtp_send", should_not_be_called)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "skipped"
    assert results[0]["error_type"] == outreach.ERR_SENDER_CAPACITY


def test_send_batch_third_lead_in_same_batch_respects_in_batch_rotation_count(monkeypatch):
    # per_account_daily_limit=1, 2 accounts, 3 leads, no prior SendLog history:
    # first two leads consume sales1 and sales2's capacity WITHIN this batch,
    # so the third lead must be skipped even though SendLog itself is empty.
    campaign_cfg = _rotation_campaign_cfg(sender_rotation=True, per_account_daily_limit=1,
                                           rotation_accounts=["sales1", "sales2"])
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    sent = [r for r in results if r["status"] == "sent"]
    skipped = [r for r in results if r["status"] == "skipped"]
    assert len(sent) == 2
    assert len(skipped) == 1
    assert {r["account"] for r in sent} == {"sales1", "sales2"}


# =============================================================================
# send_batch — concurrent rounds (multi-account "send at the same time")
# =============================================================================

def test_send_batch_sends_full_round_concurrently_not_sequentially(monkeypatch):
    """The actual point of the round-based redesign: with N distinct
    accounts and N leads, all N sends should be IN FLIGHT AT THE SAME TIME
    (real overlap), not one after another. We prove this by having each
    fake smtp_send briefly sleep and recording wall-clock start/end times —
    sequential execution could never produce overlapping intervals."""
    campaign_cfg = _rotation_campaign_cfg(sender_rotation=True)
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    intervals = []

    def slow_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None,
                        smtp_host=None, smtp_port=None, smtp_username=None):
        start = time.monotonic()
        time.sleep(0.2)
        intervals.append((start, time.monotonic()))
        return {"message_id": f"<{to}@mail.gmail.com>"}

    monkeypatch.setattr(outreach, "smtp_send", slow_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    sent = [r for r in results if r["status"] == "sent"]
    assert len(sent) == 3

    # With 3 distinct accounts, all 3 sends belong to one round and should
    # overlap: every interval's start must fall before some other
    # interval's end (impossible if they ran one at a time with a 0.2s
    # sleep each, which would produce non-overlapping back-to-back windows).
    overlap_found = any(
        a_start < b_end and b_start < a_end
        for i, (a_start, a_end) in enumerate(intervals)
        for j, (b_start, b_end) in enumerate(intervals)
        if i != j
    )
    assert overlap_found, f"Expected overlapping (concurrent) send windows, got {intervals}"


def test_send_batch_only_sleeps_between_rounds_not_between_every_send(monkeypatch):
    """2 accounts, 4 leads => 2 rounds of 2. time.sleep should be called
    exactly once (between the two rounds), not three times (which is what
    the old one-delay-per-email design would have done)."""
    campaign_cfg = _rotation_campaign_cfg(sender_rotation=True,
                                           rotation_accounts=["sales1", "sales2"],
                                           delay_min_minutes=1, delay_max_minutes=1)
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
        make_lead(_row=5, LeadID="L4", Email="d@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})

    sleep_calls = []
    monkeypatch.setattr(outreach.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    results = outreach.send_batch(campaign_cfg, fake_sheets, ROTATION_ACCOUNTS, "intro", 10)
    assert len([r for r in results if r["status"] == "sent"]) == 4
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == pytest.approx(60.0, rel=0.01)  # 1 minute, since min==max==1


def test_send_batch_two_leads_pinned_to_same_account_split_across_rounds(monkeypatch):
    """Two leads manually pinned to the SAME SenderAccount can't be in the
    same concurrent round (one account can't be counted as sending two
    'simultaneous' emails for pacing purposes) — the second is deferred to
    round 2, not treated as an error."""
    campaign_cfg = _base_campaign_cfg()
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com", SenderAccount="sales1"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com", SenderAccount="sales1"),
    ]
    fake_sheets = FakeSheets(leads)
    call_order = []

    def fake_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None,
                        smtp_host=None, smtp_port=None, smtp_username=None):
        call_order.append(to)
        return {"message_id": f"<{to}@mail.gmail.com>"}

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    sent = [r for r in results if r["status"] == "sent"]
    assert len(sent) == 2
    assert call_order == ["a@abc.com", "b@abc.com"]  # round 1 then round 2, in original order


def test_send_batch_round_grouping_leaves_single_account_setup_unchanged(monkeypatch):
    """No rotation, no manual pin — every lead resolves to the same single
    default account, so every round has exactly 1 job. This is the
    single-account backward-compatibility guarantee: behavior is identical
    to the old fully-sequential design."""
    campaign_cfg = _base_campaign_cfg()
    leads = [
        make_lead(_row=2, LeadID="L1", Email="a@abc.com"),
        make_lead(_row=3, LeadID="L2", Email="b@abc.com"),
        make_lead(_row=4, LeadID="L3", Email="c@abc.com"),
    ]
    fake_sheets = FakeSheets(leads)
    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<m@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    sent = [r for r in results if r["status"] == "sent"]
    assert len(sent) == 3
    assert all(r["account"] == "sales1" for r in sent)


# =============================================================================
# Dashboard — Sender Usage Today section
# =============================================================================

def test_dashboard_shows_sender_usage_today_with_cap():
    today = datetime.now().strftime(outreach.DATETIME_FMT)
    leads = [_dash_lead(_row=2, LeadID="L1", Email="a@abc.com", IntroSentAt=today, SenderAccount="sales1")]
    send_log = [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": today}]
    campaign_cfg = {"stages": DASH_STAGES, "sending": {"per_account_daily_limit": 5, "sender_rotation": True}}
    rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, [], send_log, [])
    d = _rows_to_dict(rows)
    assert d[("Sender Usage Today", "Sender Rotation Enabled")] == "Yes"
    assert d[("Sender Usage Today", "sales1 - Sent Today")] == "1 / 5"


def test_dashboard_shows_sender_usage_today_without_cap():
    today = datetime.now().strftime(outreach.DATETIME_FMT)
    leads = [_dash_lead(_row=2, LeadID="L1", Email="a@abc.com", IntroSentAt=today, SenderAccount="sales1")]
    send_log = [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": today}]
    campaign_cfg = {"stages": DASH_STAGES, "sending": {}}
    rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, [], send_log, [])
    d = _rows_to_dict(rows)
    assert d[("Sender Usage Today", "sales1 - Sent Today")] == "1 (no per-account cap set)"


# =============================================================================
# apply_sending_overrides — per-run CLI overrides for send
# =============================================================================

def test_apply_sending_overrides_no_args_changes_nothing():
    original_sending = {"daily_limit": 100, "sender_rotation": False}
    campaign_cfg = {"sending": original_sending}
    overridden = outreach.apply_sending_overrides(campaign_cfg)
    assert overridden == []
    assert campaign_cfg["sending"]["daily_limit"] == 100
    assert campaign_cfg["sending"]["sender_rotation"] is False
    # The dict itself must be a NEW object — never the same one that was
    # passed in — so nothing shared/cached elsewhere gets mutated.
    assert campaign_cfg["sending"] is not original_sending


def test_apply_sending_overrides_daily_limit():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    overridden = outreach.apply_sending_overrides(campaign_cfg, daily_limit=25)
    assert overridden == ["daily_limit"]
    assert campaign_cfg["sending"]["daily_limit"] == 25


def test_apply_sending_overrides_per_account_daily_limit():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    overridden = outreach.apply_sending_overrides(campaign_cfg, per_account_daily_limit=5)
    assert overridden == ["per_account_daily_limit"]
    assert campaign_cfg["sending"]["per_account_daily_limit"] == 5


def test_apply_sending_overrides_sender_rotation_true():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    overridden = outreach.apply_sending_overrides(campaign_cfg, sender_rotation="true")
    assert overridden == ["sender_rotation"]
    assert campaign_cfg["sending"]["sender_rotation"] is True


def test_apply_sending_overrides_sender_rotation_false():
    campaign_cfg = {"sending": {"daily_limit": 100, "sender_rotation": True}}
    overridden = outreach.apply_sending_overrides(campaign_cfg, sender_rotation="false")
    assert overridden == ["sender_rotation"]
    assert campaign_cfg["sending"]["sender_rotation"] is False


def test_apply_sending_overrides_all_three_at_once():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    overridden = outreach.apply_sending_overrides(
        campaign_cfg, daily_limit=10, per_account_daily_limit=2, sender_rotation="true")
    assert set(overridden) == {"daily_limit", "per_account_daily_limit", "sender_rotation"}
    assert campaign_cfg["sending"]["daily_limit"] == 10
    assert campaign_cfg["sending"]["per_account_daily_limit"] == 2
    assert campaign_cfg["sending"]["sender_rotation"] is True


def test_apply_sending_overrides_rejects_non_positive_daily_limit():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    try:
        outreach.apply_sending_overrides(campaign_cfg, daily_limit=0)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_apply_sending_overrides_rejects_negative_per_account_daily_limit():
    campaign_cfg = {"sending": {"daily_limit": 100}}
    try:
        outreach.apply_sending_overrides(campaign_cfg, per_account_daily_limit=-3)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_apply_sending_overrides_never_mutates_yaml_source_dict():
    # Simulates the real scenario: campaign_cfg['sending'] initially IS the
    # same dict object loaded from yaml. After calling the override
    # function, that ORIGINAL dict must be untouched.
    yaml_loaded_sending = {"daily_limit": 100}
    campaign_cfg = {"sending": yaml_loaded_sending}
    outreach.apply_sending_overrides(campaign_cfg, daily_limit=5)
    assert yaml_loaded_sending["daily_limit"] == 100  # untouched
    assert campaign_cfg["sending"]["daily_limit"] == 5  # only the copy changed


# =============================================================================
# check_replies — reply-matching safety fix (production incident regression)
#
# Reported scenario: a lead's email address was reused across campaigns
# (an old/deleted campaign, then a fresh "Kelson_Creators_Licensing" style
# campaign with the same lead list). An old reply — genuinely In-Reply-To a
# DIFFERENT, unrelated Message-ID — was incorrectly matched to the new
# campaign's lead via sender-email fallback alone, and incorrectly stopped
# the new sequence. These tests reproduce that exact shape and assert it's
# now impossible.
# =============================================================================

def _most_recent_send_at_lead(**overrides):
    lead = make_lead(**overrides)
    return lead


def test_most_recent_send_at_picks_latest_across_stages():
    old = "2026-08-01 10:00:00"
    newer = "2026-08-10 10:00:00"
    lead = _most_recent_send_at_lead(IntroSentAt=old, FollowUp1SentAt=newer, FollowUp2SentAt="")
    result = outreach._most_recent_send_at(lead)
    assert result == outreach._parse_dt(newer)


def test_most_recent_send_at_none_when_nothing_sent():
    lead = _most_recent_send_at_lead()
    assert outreach._most_recent_send_at(lead) is None


def test_check_replies_old_campaign_reply_never_stops_new_campaign_sequence(monkeypatch):
    # This is the reported bug, reproduced directly: the lead's current
    # (new campaign) intro was sent recently; the inbound message is an old
    # reply whose In-Reply-To points at a DIFFERENT, unrelated Message-ID
    # (simulating a deleted/previous campaign's thread) and whose Date
    # predates the new campaign's intro entirely.
    new_intro_sent_at = datetime.now().strftime(outreach.DATETIME_FMT)
    old_message_date = datetime.now() - timedelta(days=10)  # before the new intro was ever sent

    leads = [make_lead(_row=2, LeadID="L3", Email="creator@example.com",
                        MessageID="<new_campaign_intro@mail.gmail.com>",  # current campaign's own message id
                        IntroSentAt=new_intro_sent_at)]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<old_reply@mail.gmail.com>",
            "in_reply_to": "<old_unrelated_campaign_intro@mail.gmail.com>",  # NOT the current campaign's id
            "references": "<old_unrelated_campaign_intro@mail.gmail.com>",
            "subject": "Re: old campaign", "from": "creator@example.com",  # same lead address, reused
            "headers": {}, "body": "sounds good, let's talk", "snippet": "sounds good, let's talk",
            "date": old_message_date,
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                                      campaign_name="Kelson_Creators_Licensing")

    assert len(actions) == 1
    assert actions[0]["match_method"] == "Email"  # header match correctly fails — different message id
    assert actions[0]["action"] == outreach.ACTION_LOGGED_UNRELATED  # date predates the new intro

    updated_lead = fake_sheets._leads[0]
    # The critical assertion: the new campaign's sequence must NOT be stopped.
    assert updated_lead["ReplyStatus"] != "Replied"
    assert updated_lead.get("Status", "") != outreach.STATUS_STOPPED_REPLIED
    assert updated_lead.get("ReplyAt", "") == ""


def test_check_replies_email_match_after_contact_logged_unverified_not_stopped(monkeypatch):
    # Sender-only match where the message date IS plausible (after the
    # lead's most recent send) — still must not auto-stop, just logged
    # as unverified for human review.
    intro_sent_at = (datetime.now() - timedelta(days=2)).strftime(outreach.DATETIME_FMT)
    plausible_date = datetime.now() - timedelta(hours=1)  # after intro was sent

    leads = [make_lead(_row=2, LeadID="L1", Email="creator@example.com",
                        MessageID="<intro@mail.gmail.com>", IntroSentAt=intro_sent_at)]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<new_thread_reply@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "New conversation", "from": "creator@example.com",
            "headers": {}, "body": "hey, reaching out separately", "snippet": "hey, reaching out separately",
            "date": plausible_date,
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    assert actions[0]["match_method"] == "Email"
    assert actions[0]["action"] == outreach.ACTION_LOGGED_UNVERIFIED

    updated_lead = fake_sheets._leads[0]
    assert updated_lead["ReplyStatus"] != "Replied"
    assert updated_lead.get("Status", "") != outreach.STATUS_STOPPED_REPLIED
    # Still visible for a human to notice, even though it didn't auto-stop:
    assert updated_lead["LastInboundClassification"] == outreach.CLASSIFICATION_GENUINE


def test_check_replies_header_match_genuine_reply_still_stops_sequence_after_fix(monkeypatch):
    # Regression guard: the FIX must not have broken the legitimate,
    # trusted path — a real reply to the current campaign's own thread
    # must still stop the sequence exactly as before.
    leads = [make_lead(_row=2, LeadID="L1", Email="creator@example.com",
                        MessageID="<current_campaign_intro@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<real_reply@mail.gmail.com>",
            "in_reply_to": "<current_campaign_intro@mail.gmail.com>",  # matches THIS lead's tracked id
            "references": "<current_campaign_intro@mail.gmail.com>",
            "subject": "Re: intro", "from": "creator@example.com",
            "headers": {}, "body": "yes, interested", "snippet": "yes, interested",
            "date": datetime.now(),
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    assert actions[0]["match_method"] == "Header"
    assert actions[0]["action"] == outreach.ACTION_STOPPED

    updated_lead = fake_sheets._leads[0]
    assert updated_lead["ReplyStatus"] == "Replied"
    assert updated_lead["Status"] == outreach.STATUS_STOPPED_REPLIED


def test_check_replies_email_match_hard_bounce_never_stops_sequence_either(monkeypatch):
    # The uniform rule applies regardless of classification: sender-only
    # match never stops the sequence, even if content looks like a bounce.
    leads = [make_lead(_row=2, LeadID="L1", Email="creator@example.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt, imap_host=None, imap_port=None, imap_username=None):
        return [{
            "message_id": "<weird@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "undeliverable", "from": "creator@example.com",  # unusual, but same lead address
            "headers": {}, "body": "this address does not exist", "snippet": "this address does not exist",
            "date": datetime.now(),
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    assert actions[0]["match_method"] == "Email"
    assert actions[0]["action"] != outreach.ACTION_STOPPED

    updated_lead = fake_sheets._leads[0]
    assert updated_lead.get("Status", "") != outreach.STATUS_STOPPED_BOUNCED


# =============================================================================
# _message_to_dict — parsed date field
# =============================================================================

def test_message_to_dict_parses_date_header():
    from email.utils import formatdate
    msg = MIMEText("body")
    msg["Subject"] = "has a date"
    msg["From"] = "a@b.com"
    msg["Message-ID"] = "<x@mail.gmail.com>"
    msg["Date"] = formatdate(localtime=True)
    parsed = outreach._parse_email_message(msg.as_bytes())
    assert parsed["date"] is not None
    assert isinstance(parsed["date"], datetime)


def test_message_to_dict_handles_missing_date_header():
    from email.mime.text import MIMEText
    msg = MIMEText("body")
    msg["Subject"] = "no date header"
    msg["From"] = "a@b.com"
    msg["Message-ID"] = "<x@mail.gmail.com>"
    parsed = outreach._parse_email_message(msg.as_bytes())
    assert parsed["date"] is None


# =============================================================================
# discover_stages_and_variants — direct unit tests of the auto-discovery
# algorithm itself, independent of the full get_campaign() plumbing.
# =============================================================================

def _write_template(dir_path, filename):
    (pathlib.Path(dir_path) / filename).write_text("Subject: Hi\n\nBody")


def test_discover_minimal_single_file(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    _write_template(d, "intro_A.txt")
    stages, variants = outreach.discover_stages_and_variants(str(d), {})
    assert len(stages) == 1
    assert stages[0]["name"] == "intro"
    assert stages[0]["template_prefix"] == "intro"
    assert stages[0]["wait_days_after_previous"] == 0
    assert variants == ["A"]


def test_discover_two_stages_two_variants_uses_stage_wait_days(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    for stage in ["intro", "followup1"]:
        for v in ["A", "B"]:
            _write_template(d, f"{stage}_{v}.txt")
    stages, variants = outreach.discover_stages_and_variants(str(d), {"intro": 0, "followup1": 3})
    assert [s["name"] for s in stages] == ["intro", "followup1"]
    assert stages[0]["wait_days_after_previous"] == 0
    assert stages[1]["wait_days_after_previous"] == 3
    assert variants == ["A", "B"]


def test_discover_missing_stage_wait_days_entry_defaults_to_zero(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    for stage in ["intro", "followup1"]:
        _write_template(d, f"{stage}_A.txt")
    stages, variants = outreach.discover_stages_and_variants(str(d), {})  # no wait_days configured at all
    assert stages[1]["wait_days_after_previous"] == 0


def test_discover_stops_at_first_gap(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    _write_template(d, "intro_A.txt")
    # No followup1 files at all — followup2 existing anyway must be ignored.
    _write_template(d, "followup2_A.txt")
    stages, variants = outreach.discover_stages_and_variants(str(d), {})
    assert [s["name"] for s in stages] == ["intro"]


def test_discover_all_five_stages_four_variants(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    for stage in outreach.CANONICAL_STAGE_ORDER:
        for v in outreach.ALL_VARIANT_LETTERS:
            _write_template(d, f"{stage}_{v}.txt")
    stages, variants = outreach.discover_stages_and_variants(str(d), {})
    assert len(stages) == 5
    assert variants == ["A", "B", "C", "D"]


def test_discover_inconsistent_variants_raises_clear_error(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    for v in ["A", "B", "C", "D"]:
        _write_template(d, f"intro_{v}.txt")
    for v in ["A", "B", "C"]:  # D missing for followup1
        _write_template(d, f"followup1_{v}.txt")
    try:
        outreach.discover_stages_and_variants(str(d), {})
        assert False, "should have raised ConfigError"
    except outreach.ConfigError as exc:
        assert "followup1" in str(exc)
        assert "D" in str(exc)


def test_discover_no_templates_at_all_raises(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    try:
        outreach.discover_stages_and_variants(str(d), {})
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


def test_discover_nonexistent_directory_raises(tmp_path):
    try:
        outreach.discover_stages_and_variants(str(tmp_path / "does_not_exist"), {})
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


# =============================================================================
# parse_stages_and_variants_from_filenames / parse_template_content —
# pure, filesystem-free versions used for LIVE (GitHub API) reads, so a
# stale local checkout is never the only thing standing between a
# destructive action and a corrupted repo state.
# =============================================================================

def test_parse_stages_from_filenames_basic_single_stage():
    stages, variants = outreach.parse_stages_and_variants_from_filenames(
        ["intro_A.txt"], {})
    assert len(stages) == 1
    assert stages[0]["template_prefix"] == "intro"
    assert variants == ["A"]


def test_parse_stages_from_filenames_multi_stage_multi_variant():
    files = ["intro_A.txt", "intro_B.txt", "followup1_A.txt", "followup1_B.txt"]
    stages, variants = outreach.parse_stages_and_variants_from_filenames(files, {})
    assert [s["template_prefix"] for s in stages] == ["intro", "followup1"]
    assert variants == ["A", "B"]


def test_parse_stages_from_filenames_stops_at_first_gap():
    """The exact contiguity check that caught the real corruption case:
    followup1 missing but followup2 present must never be silently
    treated as 'skip a stage'."""
    files = ["intro_A.txt", "followup2_A.txt"]
    stages, variants = outreach.parse_stages_and_variants_from_filenames(files, {})
    assert [s["template_prefix"] for s in stages] == ["intro"]


def test_parse_stages_from_filenames_rejects_inconsistent_variants():
    """The exact scenario that caused real, permanent corruption: a stage
    missing some of the variants an earlier stage has must raise, not
    silently shrink that stage's variant set."""
    files = ["intro_A.txt", "intro_B.txt",
             "followup1_A.txt", "followup1_B.txt",
             "followup2_A.txt", "followup2_B.txt",
             "followup3_A.txt"]  # missing B — inconsistent with every earlier stage
    with pytest.raises(outreach.ConfigError, match="missing variant"):
        outreach.parse_stages_and_variants_from_filenames(files, {})


def test_parse_stages_from_filenames_raises_when_empty():
    with pytest.raises(outreach.ConfigError, match="No template files found"):
        outreach.parse_stages_and_variants_from_filenames([], {})


def test_parse_stages_from_filenames_applies_wait_days():
    stages, _ = outreach.parse_stages_and_variants_from_filenames(
        ["intro_A.txt", "followup1_A.txt"], {"followup1": 3})
    by_prefix = {s["template_prefix"]: s for s in stages}
    assert by_prefix["intro"]["wait_days_after_previous"] == 0
    assert by_prefix["followup1"]["wait_days_after_previous"] == 3


def test_discover_stages_and_variants_still_works_against_a_real_directory(tmp_path):
    """The filesystem wrapper must still behave identically to before —
    this refactor must be purely additive, not a behavior change for
    existing callers (GitHub Actions workflows that only ever run
    against a definitely-current checkout)."""
    campaign_dir = tmp_path / "TestCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")
    stages, variants = outreach.discover_stages_and_variants(str(campaign_dir), {})
    assert len(stages) == 1
    assert variants == ["A"]


def test_parse_template_content_basic():
    result = outreach.parse_template_content("Subject: Hello there\n\nBody line 1\nBody line 2")
    assert result == {"subject": "Hello there", "body": "Body line 1\nBody line 2"}


def test_parse_template_content_rejects_missing_subject_line():
    with pytest.raises(outreach.TemplateError, match="must start with"):
        outreach.parse_template_content("No subject line here\n\nBody")


def test_load_template_still_works_against_a_real_file(tmp_path):
    """Same behavior-preservation guarantee as the stages/variants
    wrapper above."""
    campaign_dir = tmp_path / "TestCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi there\n\nBody text")
    result = outreach.load_template(str(campaign_dir), "intro", "A")
    assert result == {"subject": "Hi there", "body": "Body text"}



# =============================================================================
# Asana sync — the pure decision logic first, since this is what actually
# guarantees no duplicates and no overriding a human's stage decision.
# =============================================================================

def _lead_for_asana(**overrides):
    lead = {"LeadID": "1", "Email": "creator@abc.com", "IntroSentAt": "", "FollowUp1SentAt": "",
            "FollowUp2SentAt": "", "FollowUp3SentAt": "", "FollowUp4SentAt": "", "ReplyStatus": "",
            "AsanaTaskGID": ""}
    lead.update(overrides)
    return lead


# ---------- compute_lead_asana_stage ----------

def test_compute_lead_asana_stage_no_activity_is_sourced():
    assert outreach.compute_lead_asana_stage(_lead_for_asana()) == outreach.ASANA_STAGE_SOURCED


def test_compute_lead_asana_stage_intro_sent_is_outreach_sent():
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_OUTREACH_SENT


def test_compute_lead_asana_stage_any_followup_sent_is_followup():
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00", FollowUp1SentAt="2026-08-05 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_FOLLOWUP


def test_compute_lead_asana_stage_later_followup_alone_is_still_followup():
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00", FollowUp3SentAt="2026-08-20 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_FOLLOWUP


def test_compute_lead_asana_stage_replied_is_negotiating():
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00", ReplyStatus="Replied")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_lead_asana_stage_reply_wins_over_followup_sent():
    """Most-advanced-state-first — a lead with both a follow-up sent AND
    a reply logged is Negotiating, not stuck at Follow-up."""
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00", FollowUp2SentAt="2026-08-10 09:00:00",
                            ReplyStatus="Replied")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_lead_asana_stage_never_returns_a_manual_only_stage():
    """No combination of send/reply data should ever produce Rights
    Secured or Declined / Dead — those are human decisions only."""
    for reply_status in ("", "Replied"):
        for followup in ("", "2026-08-10 09:00:00"):
            lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00", FollowUp1SentAt=followup,
                                    ReplyStatus=reply_status)
            stage = outreach.compute_lead_asana_stage(lead)
            assert stage not in outreach.ASANA_MANUAL_ONLY_STAGES


def test_compute_lead_asana_stage_manual_override_reaches_rights_secured():
    """The actual feature request — a lead handled entirely outside the
    automated pipeline (e.g. manually negotiated) needs a real way to
    reach Rights Secured, which normal send/reply-derived logic can
    never produce on its own."""
    lead = _lead_for_asana(ManualAsanaStage="Rights Secured")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_RIGHTS_SECURED


def test_compute_lead_asana_stage_manual_override_reaches_declined_dead():
    lead = _lead_for_asana(ManualAsanaStage="Declined / Dead")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_DECLINED_DEAD


def test_compute_lead_asana_stage_manual_override_is_case_insensitive():
    lead = _lead_for_asana(ManualAsanaStage="rights secured")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_RIGHTS_SECURED


def test_compute_lead_asana_stage_manual_override_wins_over_send_data():
    """The override takes priority even over a lead that's genuinely
    replied and would otherwise compute to Negotiating."""
    lead = _lead_for_asana(ManualAsanaStage="Declined / Dead", ReplyStatus="Replied",
                            IntroSentAt="2026-08-01 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_DECLINED_DEAD


def test_compute_lead_asana_stage_unrecognized_override_falls_back_to_auto():
    lead = _lead_for_asana(ManualAsanaStage="Finalized!!", IntroSentAt="2026-08-01 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_OUTREACH_SENT


def test_compute_lead_asana_stage_blank_override_is_a_no_op():
    lead = _lead_for_asana(ManualAsanaStage="", IntroSentAt="2026-08-01 09:00:00")
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_OUTREACH_SENT


def test_compute_lead_asana_stage_manual_override_handles_integer_type_safely():
    """Same class of Sheet-typing risk as AsanaTaskGID — must never crash
    even if this cell somehow comes back as a non-string type."""
    lead = _lead_for_asana(ManualAsanaStage=12345)
    # Not a recognized stage name — falls through to auto-derivation, doesn't crash.
    assert outreach.compute_lead_asana_stage(lead) == outreach.ASANA_STAGE_SOURCED


# ---------- compute_dm_asana_stage ----------

def test_compute_dm_asana_stage_not_contacted_is_sourced():
    assert outreach.compute_dm_asana_stage("Not Contacted") == outreach.ASANA_STAGE_SOURCED


def test_compute_dm_asana_stage_draft_ready_is_still_sourced():
    """A draft existing doesn't mean anything was actually SENT yet."""
    assert outreach.compute_dm_asana_stage("Draft Ready") == outreach.ASANA_STAGE_SOURCED


def test_compute_dm_asana_stage_sent_is_outreach_sent():
    assert outreach.compute_dm_asana_stage("Sent") == outreach.ASANA_STAGE_OUTREACH_SENT


def test_compute_dm_asana_stage_follow_up_needed_is_followup():
    assert outreach.compute_dm_asana_stage("Follow-up Needed") == outreach.ASANA_STAGE_FOLLOWUP


def test_compute_dm_asana_stage_no_response_is_followup():
    assert outreach.compute_dm_asana_stage("No Response") == outreach.ASANA_STAGE_FOLLOWUP


def test_compute_dm_asana_stage_replied_is_negotiating():
    assert outreach.compute_dm_asana_stage("Replied") == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_dm_asana_stage_interested_is_negotiating():
    assert outreach.compute_dm_asana_stage("Interested") == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_dm_asana_stage_not_interested_is_negotiating_not_declined():
    """The deliberate design choice: Declined/Dead stays a human decision
    made directly in Asana, same rule as email — a DM status alone,
    even a clearly negative one, must never auto-assign it."""
    assert outreach.compute_dm_asana_stage("Not Interested") == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_dm_asana_stage_closed_is_negotiating():
    assert outreach.compute_dm_asana_stage("Closed") == outreach.ASANA_STAGE_NEGOTIATING


def test_compute_dm_asana_stage_blank_defaults_to_sourced():
    assert outreach.compute_dm_asana_stage("") == outreach.ASANA_STAGE_SOURCED


def test_compute_dm_asana_stage_unrecognized_defaults_to_sourced_not_guessed():
    assert outreach.compute_dm_asana_stage("SomeFutureStatusNotYetMapped") == outreach.ASANA_STAGE_SOURCED


def test_compute_dm_asana_stage_never_returns_a_manual_only_stage():
    """No DM status, across the entire real vocabulary, should ever
    produce Rights Secured or Declined / Dead automatically."""
    for status in ["Not Contacted", "Draft Ready", "Sent", "Follow-up Needed",
                   "Replied", "Interested", "Not Interested", "No Response", "Closed", ""]:
        assert outreach.compute_dm_asana_stage(status) not in outreach.ASANA_MANUAL_ONLY_STAGES


# ---------- decide_asana_sync_action ----------

def test_safe_lead_str_coerces_int_without_crashing():
    """The actual reported bug: gspread returns a numeric-looking cell
    (an Asana task GID is a long run of digits) as a Python int, not a
    str — every already-synced lead hit this on the very next sync."""
    assert outreach._safe_lead_str(1218101643744828) == "1218101643744828"


def test_safe_lead_str_handles_none_and_blank():
    assert outreach._safe_lead_str(None) == ""
    assert outreach._safe_lead_str("  ") == ""


def test_decide_asana_sync_action_handles_integer_asana_task_gid():
    """Reproduces the exact production crash: a real Sheet read via
    gspread returns AsanaTaskGID as an int for every already-synced
    lead, not a str — this must never raise."""
    lead = _lead_for_asana(AsanaTaskGID=1218101643744828)
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Outreach Sent")
    assert decision["action"] == "update"


def test_build_asana_task_name_handles_integer_fields_without_crashing():
    lead = _lead_for_asana(Client=12345, Product="DudeRobe")
    assert outreach.build_asana_task_name(lead) == "12345 | DudeRobe"


def test_decide_asana_sync_action_no_existing_task_creates():
    lead = _lead_for_asana()
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name=None)
    assert decision["action"] == "create"
    assert decision["target_stage"] == outreach.ASANA_STAGE_SOURCED


def test_decide_asana_sync_action_existing_task_updates_not_creates():
    """The core no-duplicates guarantee — a lead with an AsanaTaskGID
    already set is NEVER a 'create', regardless of anything else."""
    lead = _lead_for_asana(AsanaTaskGID="123456")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Outreach Sent")
    assert decision["action"] == "update"


def test_decide_asana_sync_action_moves_stage_forward_on_update():
    lead = _lead_for_asana(AsanaTaskGID="123456", IntroSentAt="2026-08-01 09:00:00",
                            FollowUp1SentAt="2026-08-05 09:00:00")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Outreach Sent")
    assert decision["action"] == "update"
    assert decision["target_stage"] == outreach.ASANA_STAGE_FOLLOWUP


def test_decide_asana_sync_action_never_moves_out_of_rights_secured():
    """The other core guarantee — once a human has moved a task to
    Rights Secured, sync must never move it back out, no matter what
    the lead's own send/reply data would otherwise compute."""
    lead = _lead_for_asana(AsanaTaskGID="123456", ReplyStatus="Replied")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Rights Secured")
    assert decision["action"] == "update"
    assert decision["target_stage"] is None


def test_decide_asana_sync_action_never_moves_out_of_declined_dead():
    lead = _lead_for_asana(AsanaTaskGID="123456", ReplyStatus="Replied")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Declined / Dead")
    assert decision["action"] == "update"
    assert decision["target_stage"] is None


def test_decide_asana_sync_action_still_updates_fields_when_in_manual_stage():
    """target_stage=None means 'don't move it' — it does NOT mean 'skip
    this lead entirely'; the caller still updates its other fields."""
    lead = _lead_for_asana(AsanaTaskGID="123456")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Declined / Dead")
    assert decision["action"] == "update"


# ---------- build_asana_task_name ----------

def test_build_asana_task_name_all_three_fields_present():
    lead = _lead_for_asana(Client="DudeRobe", CreatorHandle="@rocky", Product="DudeRobe")
    assert outreach.build_asana_task_name(lead) == "DudeRobe | @rocky \u2013 DudeRobe"


def test_build_asana_task_name_missing_product_still_joins_available_parts():
    lead = _lead_for_asana(Client="DudeRobe", CreatorHandle="@rocky")
    assert outreach.build_asana_task_name(lead) == "DudeRobe | @rocky"


def test_build_asana_task_name_falls_back_to_email_when_nothing_available():
    lead = _lead_for_asana(Email="rocky@abc.com")
    assert outreach.build_asana_task_name(lead) == "rocky@abc.com"


def test_build_asana_task_name_falls_back_to_creator_column_for_handle():
    """The actual reported bug: a real campaign's sheet commonly puts
    the @handle under 'Creator' (the same column that also flows into
    Asana's own Creator custom field), not a separate 'CreatorHandle'
    column — every task title was silently missing the handle entirely,
    collapsing to 'DudeRobe | DudeRobe' instead of showing who the
    creator actually was."""
    lead = _lead_for_asana(Client="DudeRobe", Creator="@andrea_shepperd", Product="DudeRobe")
    assert outreach.build_asana_task_name(lead) == "DudeRobe | @andrea_shepperd \u2013 DudeRobe"


def test_build_asana_task_name_explicit_creator_handle_wins_over_creator_fallback():
    lead = _lead_for_asana(Client="DudeRobe", CreatorHandle="@dedicated_handle",
                            Creator="Andrea Shepperd", Product="DudeRobe")
    assert outreach.build_asana_task_name(lead) == "DudeRobe | @dedicated_handle \u2013 DudeRobe"


# ---------- _match_asana_option ----------

def test_match_asana_option_case_insensitive():
    options = {"DudeRobe": "gid1", "SheRobe": "gid2"}
    assert outreach._match_asana_option("duderobe", options) == "gid1"


def test_match_asana_option_no_match_returns_none():
    options = {"DudeRobe": "gid1"}
    assert outreach._match_asana_option("SomethingElse", options) is None


# ---------- build_asana_custom_fields_payload ----------

def _field_defs():
    return {
        "Creator": {"gid": "f_creator", "type": "text", "options": {}},
        "Content Score": {"gid": "f_score", "type": "enum", "options": {"5": "opt5", "4": "opt4"}},
        "Product": {"gid": "f_product", "type": "multi_enum",
                    "options": {"DudeRobe": "opt_dr", "SheRobe": "opt_sr"}},
        "Last Contact Date": {"gid": "f_date", "type": "date", "options": {}},
    }


def test_build_custom_fields_payload_maps_text_field():
    lead = _lead_for_asana(Creator="Rocky Rivera")
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert payload["f_creator"] == "Rocky Rivera"


def test_build_custom_fields_payload_maps_enum_field_by_name():
    lead = _lead_for_asana(**{"Content Score": "5"})
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert payload["f_score"] == "opt5"


def test_build_custom_fields_payload_maps_multi_enum_single_value():
    lead = _lead_for_asana(Product="DudeRobe")
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert payload["f_product"] == ["opt_dr"]


def test_build_custom_fields_payload_maps_multi_enum_comma_separated():
    lead = _lead_for_asana(Product="DudeRobe, SheRobe")
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert set(payload["f_product"]) == {"opt_dr", "opt_sr"}


def test_build_custom_fields_payload_maps_date_field():
    lead = _lead_for_asana(**{"Last Contact Date": "2026-08-26"})
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert payload["f_date"] == {"date": "2026-08-26"}


def test_build_custom_fields_payload_skips_column_with_no_matching_asana_field():
    lead = _lead_for_asana(SomeRandomColumn="whatever")
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert payload == {}


def test_build_custom_fields_payload_skips_blank_values():
    lead = _lead_for_asana(Creator="")
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert "f_creator" not in payload


def test_build_custom_fields_payload_skips_enum_value_with_no_matching_option():
    lead = _lead_for_asana(**{"Content Score": "999"})
    payload = outreach.build_asana_custom_fields_payload(lead, _field_defs())
    assert "f_score" not in payload


def test_build_custom_fields_payload_never_includes_reserved_master_columns():
    """The email system's own tracking fields (IntroSentAt, Status, ...)
    must never be treated as creator-data custom fields, even if a
    field happened to exist on the Asana project with a matching name."""
    field_defs = dict(_field_defs())
    field_defs["IntroSentAt"] = {"gid": "f_intro", "type": "text", "options": {}}
    lead = _lead_for_asana(IntroSentAt="2026-08-01 09:00:00")
    payload = outreach.build_asana_custom_fields_payload(lead, field_defs)
    assert "f_intro" not in payload


def test_build_custom_fields_payload_falls_back_to_email_for_creator_email_field():
    """The real gap this fixes — the system's own 'Email' column IS the
    creator's email; requiring it typed a second time under a separate
    'Creator Email' column would be pointless duplicate data entry."""
    field_defs = dict(_field_defs())
    field_defs["Creator Email"] = {"gid": "f_creator_email", "type": "text", "options": {}}
    lead = _lead_for_asana(Email="rocky@abc.com")
    payload = outreach.build_asana_custom_fields_payload(lead, field_defs)
    assert payload["f_creator_email"] == "rocky@abc.com"


def test_build_custom_fields_payload_explicit_creator_email_column_wins_over_fallback():
    field_defs = dict(_field_defs())
    field_defs["Creator Email"] = {"gid": "f_creator_email", "type": "text", "options": {}}
    lead = _lead_for_asana(Email="rocky@abc.com", **{"Creator Email": "different@abc.com"})
    payload = outreach.build_asana_custom_fields_payload(lead, field_defs)
    assert payload["f_creator_email"] == "different@abc.com"


def test_build_custom_fields_payload_no_fallback_when_email_blank():
    field_defs = dict(_field_defs())
    field_defs["Creator Email"] = {"gid": "f_creator_email", "type": "text", "options": {}}
    lead = _lead_for_asana(Email="")
    payload = outreach.build_asana_custom_fields_payload(lead, field_defs)
    assert "f_creator_email" not in payload


# =============================================================================
# sync_campaign_to_asana — the full orchestration, with every Asana HTTP call
# mocked. No real network traffic in this test suite.
# =============================================================================

def _fake_asana_project_response():
    return {
        "data": {
            "sections": [
                {"name": "Sourced", "gid": "sec_sourced"},
                {"name": "Outreach Sent", "gid": "sec_outreach"},
                {"name": "Follow-up", "gid": "sec_followup"},
                {"name": "Negotiating", "gid": "sec_negotiating"},
                {"name": "Rights Secured", "gid": "sec_rights"},
                {"name": "Declined / Dead", "gid": "sec_declined"},
            ],
            "custom_field_settings": [
                {"custom_field": {"name": "Creator", "gid": "f_creator", "resource_subtype": "text"}},
            ],
        }
    }


class FakeAsanaResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


def _make_fake_asana_request(task_sections=None, create_gid="new_task_gid"):
    """task_sections: {existing_task_gid: current_section_name} — for
    GET /tasks/<gid> lookups on leads that already have an AsanaTaskGID."""
    task_sections = task_sections or {}
    calls = []

    def fake_request(method, url, headers=None, timeout=None, json=None):
        calls.append((method, url, json))
        if method == "GET" and url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        if method == "GET" and "/projects?" in url:
            return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_1"}]})
        if method == "GET" and url.endswith("/projects/proj_1") is False and "/projects/proj_1?" in url:
            return _wrap(_fake_asana_project_response())
        if method == "GET" and "/tasks/" in url and "opt_fields=memberships" in url:
            task_gid = url.split("/tasks/")[1].split("?")[0]
            section_name = task_sections.get(task_gid)
            memberships = [{"project": {"gid": "proj_1"}, "section": {"name": section_name}}] if section_name else []
            return FakeAsanaResponse({"data": {"memberships": memberships}})
        if method == "POST" and url.endswith("/tasks"):
            return FakeAsanaResponse({"data": {"gid": create_gid}})
        if method == "POST" and "/addTask" in url:
            return FakeAsanaResponse({"data": {}})
        if method == "PUT" and "/tasks/" in url:
            return FakeAsanaResponse({"data": {}})
        raise AssertionError(f"Unexpected Asana call: {method} {url}")

    def _wrap(data):
        return FakeAsanaResponse(data)

    return fake_request, calls


def test_asana_find_project_gid_includes_workspace_param(monkeypatch):
    """The actual reported bug: Asana's /projects endpoint rejects a
    request with no workspace specified at all (a real 400 from their
    API, not a coding error on the caller's end) — this must always be
    included."""
    calls = []

    def fake_request(method, url, headers=None, timeout=None, json=None):
        calls.append(url)
        if url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_1"}]})

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    gid = outreach.asana_find_project_gid("Creator Outreach", api_key="fake-key")

    assert gid == "proj_1"
    projects_calls = [c for c in calls if "/projects?" in c]
    assert len(projects_calls) == 1
    assert "workspace=workspace_1" in projects_calls[0]


def test_asana_find_project_gid_searches_every_workspace(monkeypatch):
    """A token's owner can belong to more than one workspace — the
    project must be findable regardless of which one it's actually in,
    not just the first."""
    def fake_request(method, url, headers=None, timeout=None, json=None):
        if url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [
                {"name": "Workspace A", "gid": "workspace_a"},
                {"name": "Workspace B", "gid": "workspace_b"},
            ]})
        if "workspace=workspace_a" in url:
            return FakeAsanaResponse({"data": [{"name": "Some Other Project", "gid": "other"}]})
        if "workspace=workspace_b" in url:
            return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_in_b"}]})
        raise AssertionError(f"Unexpected call: {url}")

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    gid = outreach.asana_find_project_gid("Creator Outreach", api_key="fake-key")
    assert gid == "proj_in_b"


def test_asana_find_project_gid_raises_clearly_when_no_workspaces(monkeypatch):
    def fake_request(method, url, headers=None, timeout=None, json=None):
        return FakeAsanaResponse({"data": []})

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    with pytest.raises(RuntimeError, match="no accessible workspaces"):
        outreach.asana_find_project_gid("Creator Outreach", api_key="fake-key")


def test_asana_find_project_gid_returns_none_when_not_found_in_any_workspace(monkeypatch):
    def fake_request(method, url, headers=None, timeout=None, json=None):
        if url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        return FakeAsanaResponse({"data": [{"name": "Some Other Project", "gid": "other"}]})

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    assert outreach.asana_find_project_gid("Creator Outreach", api_key="fake-key") is None


def test_sync_campaign_to_asana_disabled_is_a_noop(monkeypatch):
    fake_sheets = FakeSheets([])
    campaign_cfg = {"asana": {"enabled": False}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")
    assert result["skipped_disabled"] is True


def test_sync_campaign_to_asana_missing_project_name_raises(monkeypatch):
    fake_sheets = FakeSheets([])
    campaign_cfg = {"asana": {"enabled": True, "project_name": ""}}
    with pytest.raises(RuntimeError, match="no project_name"):
        outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")


def test_sync_campaign_to_asana_creates_new_task_and_writes_gid_back(monkeypatch):
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", Creator="Rocky Rivera",
                      AsanaTaskGID="")
    fake_sheets = FakeSheets([lead])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["created"] == 1
    assert result["updated"] == 0
    assert result["errors"] == []
    updated_lead = fake_sheets.get_all_leads()[0]
    assert updated_lead["AsanaTaskGID"] == "new_task_gid"  # written back for future dedup

    create_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert len(create_calls) == 1
    assign_calls = [c for c in calls if "/addTask" in c[1]]
    assert assign_calls[0][1] == f"{outreach.ASANA_API_BASE}/sections/sec_sourced/addTask"  # no activity = Sourced


def test_sync_campaign_to_asana_never_creates_a_second_task_for_same_lead(monkeypatch):
    """The end-to-end no-duplicates guarantee, not just the unit-level
    decision logic — running sync against a lead that already has an
    AsanaTaskGID must never call POST /tasks at all."""
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", AsanaTaskGID="existing_gid_123")
    fake_sheets = FakeSheets([lead])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Outreach Sent"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["created"] == 0
    assert result["updated"] == 1
    create_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert create_calls == []  # never even attempted a create


def test_sync_campaign_to_asana_self_heals_a_previously_wrong_task_name(monkeypatch):
    """The other half of the reported fix: a task created before the
    Creator-column fallback existed has a wrong name ('DudeRobe |
    DudeRobe' with no handle) stored in Asana forever unless an update
    also corrects it — not just custom fields. No manual renaming in
    Asana should ever be needed."""
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", AsanaTaskGID="existing_gid_123",
                      Client="DudeRobe", Creator="@andrea_shepperd", Product="DudeRobe")
    fake_sheets = FakeSheets([lead])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Outreach Sent"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    update_calls = [c for c in calls if c[0] == "PUT" and "/tasks/existing_gid_123" in c[1]]
    assert len(update_calls) == 1
    assert update_calls[0][2]["data"]["name"] == "DudeRobe | @andrea_shepperd \u2013 DudeRobe"


def test_sync_campaign_to_asana_never_moves_task_out_of_rights_secured(monkeypatch):
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", AsanaTaskGID="existing_gid_123",
                      ReplyStatus="Replied")
    fake_sheets = FakeSheets([lead])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Rights Secured"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    move_calls = [c for c in calls if "/addTask" in c[1]]
    assert move_calls == []  # Negotiating would otherwise be computed — must never move it


def test_sync_campaign_to_asana_skips_lead_with_no_email(monkeypatch):
    lead = make_lead(_row=2, LeadID="L1", Email="", AsanaTaskGID="")
    fake_sheets = FakeSheets([lead])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["skipped_no_email"] == 1
    assert result["created"] == 0


def test_decide_asana_sync_action_creates_when_existing_gid_explicitly_none():
    """The actual fix that makes self-healing work: the caller can now
    say 'treat this as having no task' via existing_task_gid, even
    though the lead's own AsanaTaskGID field still has a (known-stale)
    value in it. Without this explicit override, the function would
    just re-derive the stale answer from the lead dict itself."""
    lead = _lead_for_asana(AsanaTaskGID="stale_gid_that_404d")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name=None,
                                                  existing_task_gid="")
    assert decision["action"] == "create"


def test_decide_asana_sync_action_defaults_to_reading_lead_when_not_given():
    """Backward compatible — every normal call site doesn't pass
    existing_task_gid explicitly and gets the same behavior as before."""
    lead = _lead_for_asana(AsanaTaskGID="real_gid_123")
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name="Outreach Sent")
    assert decision["action"] == "update"


def test_asana_request_raises_specific_error_for_404(monkeypatch):
    def fake_request(method, url, headers=None, timeout=None, json=None):
        return FakeAsanaResponse({}, status_code=404)

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    with pytest.raises(outreach.AsanaTaskNotFoundError):
        outreach._asana_request("GET", "/tasks/does_not_exist", api_key="fake-key")


def test_sync_campaign_to_asana_self_heals_a_404_stale_task_gid(monkeypatch):
    """The actual reported production case: a stored AsanaTaskGID that
    Asana now returns 404 for (the task is genuinely gone, whatever the
    reason) must not fail the sync forever — it self-heals by creating
    a fresh task and overwriting the stale GID, rather than requiring
    someone to manually clear the cell in the Sheet."""
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", AsanaTaskGID="dead_gid_123")
    fake_sheets = FakeSheets([lead])

    def fake_request(method, url, headers=None, timeout=None, json=None):
        if method == "GET" and url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        if method == "GET" and "/projects?" in url:
            return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_1"}]})
        if method == "GET" and "/projects/proj_1?" in url:
            return FakeAsanaResponse(_fake_asana_project_response())
        if method == "GET" and "/tasks/dead_gid_123" in url:
            return FakeAsanaResponse({}, status_code=404)
        if method == "POST" and url.endswith("/tasks"):
            return FakeAsanaResponse({"data": {"gid": "brand_new_gid"}})
        if method == "POST" and "/addTask" in url:
            return FakeAsanaResponse({"data": {}})
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["errors"] == []
    assert result["created"] == 1
    assert result["updated"] == 0
    updated_lead = fake_sheets.get_all_leads()[0]
    assert updated_lead["AsanaTaskGID"] == "brand_new_gid"  # stale GID overwritten with the fresh one


def test_sync_campaign_to_asana_403_stays_a_hard_error_not_self_healed(monkeypatch):
    """A 403 is deliberately NOT treated the same as a 404 — it could
    mean the task still exists but access to it was lost, and blindly
    creating a second task in that case would be a real, visible
    duplicate sitting in Asana that the current token just can't see."""
    lead = make_lead(_row=2, LeadID="L1", Email="rocky@abc.com", AsanaTaskGID="forbidden_gid_123")
    fake_sheets = FakeSheets([lead])

    def fake_request(method, url, headers=None, timeout=None, json=None):
        if method == "GET" and url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        if method == "GET" and "/projects?" in url:
            return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_1"}]})
        if method == "GET" and "/projects/proj_1?" in url:
            return FakeAsanaResponse(_fake_asana_project_response())
        if method == "GET" and "/tasks/forbidden_gid_123" in url:
            return FakeAsanaResponse({}, status_code=403)
        raise AssertionError(f"Unexpected call: {method} {url}")

    monkeypatch.setattr(outreach.requests, "request", fake_request)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["created"] == 0
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert "403" in result["errors"][0]["error"]
    updated_lead = fake_sheets.get_all_leads()[0]
    assert updated_lead["AsanaTaskGID"] == "forbidden_gid_123"  # never overwritten


def test_sync_campaign_to_asana_one_lead_error_does_not_block_others(monkeypatch):
    lead_ok = make_lead(_row=2, LeadID="L1", Email="ok@abc.com", AsanaTaskGID="")
    lead_bad = make_lead(_row=3, LeadID="L2", Email="bad@abc.com", AsanaTaskGID="broken_gid")
    fake_sheets = FakeSheets([lead_ok, lead_bad])
    fake_request, calls = _make_fake_asana_request()  # "broken_gid" has no entry -> no memberships found

    def fake_request_with_failure(method, url, headers=None, timeout=None, json=None):
        if "broken_gid" in url:
            raise requests.ConnectionError("simulated failure")
        return fake_request(method, url, headers=headers, timeout=timeout, json=json)

    monkeypatch.setattr(outreach.requests, "request", fake_request_with_failure)

    campaign_cfg = {"asana": {"enabled": True, "project_name": "Creator Outreach"}}
    result = outreach.sync_campaign_to_asana(fake_sheets, campaign_cfg, api_key="fake-key")

    assert result["created"] == 1  # lead_ok still succeeded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["email"] == "bad@abc.com"


# ---------- cmd_sync_asana_all ----------

def test_cmd_sync_asana_all_skips_campaigns_without_asana_enabled(monkeypatch, capsys):
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "fake-key")
    monkeypatch.setattr(outreach, "discover_campaign_names", lambda: ["Foo", "Bar"])
    monkeypatch.setattr(outreach, "get_campaign", lambda name, **kw: {"asana": {"enabled": False}})
    sync_calls = []
    monkeypatch.setattr(outreach, "sync_campaign_to_asana", lambda *a, **kw: sync_calls.append(1))

    outreach.cmd_sync_asana_all(argparse.Namespace())

    assert sync_calls == []
    assert "nothing to do" in capsys.readouterr().out


def test_cmd_sync_asana_all_syncs_only_enabled_campaigns(monkeypatch, capsys):
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "fake-key")
    monkeypatch.setattr(outreach, "discover_campaign_names", lambda: ["Foo", "Bar"])

    def fake_get_campaign(name, **kw):
        return {"_campaign_name": name, "asana": {"enabled": name == "Foo", "project_name": "X"}}

    monkeypatch.setattr(outreach, "get_campaign", fake_get_campaign)
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: FakeSheets([]))
    synced_campaigns = []

    def fake_sync(sheets, campaign_cfg, api_key):
        synced_campaigns.append(campaign_cfg["_campaign_name"])
        return {"created": 1, "updated": 0, "skipped_no_email": 0, "errors": []}

    monkeypatch.setattr(outreach, "sync_campaign_to_asana", fake_sync)

    outreach.cmd_sync_asana_all(argparse.Namespace())

    assert synced_campaigns == ["Foo"]  # Bar never synced


def test_cmd_sync_asana_all_one_campaign_failure_does_not_block_others(monkeypatch, capsys):
    monkeypatch.setenv("ASANA_ACCESS_TOKEN", "fake-key")
    monkeypatch.setattr(outreach, "discover_campaign_names", lambda: ["Foo", "Bar"])
    monkeypatch.setattr(outreach, "get_campaign",
                         lambda name, **kw: {"_campaign_name": name, "asana": {"enabled": True, "project_name": "X"}})
    monkeypatch.setattr(outreach, "_connect_sheets", lambda cfg: FakeSheets([]))

    def fake_sync(sheets, campaign_cfg, api_key):
        if campaign_cfg["_campaign_name"] == "Foo":
            raise RuntimeError("Foo's project not found")
        return {"created": 2, "updated": 0, "skipped_no_email": 0, "errors": []}

    monkeypatch.setattr(outreach, "sync_campaign_to_asana", fake_sync)

    with pytest.raises(SystemExit):
        outreach.cmd_sync_asana_all(argparse.Namespace())

    out = capsys.readouterr().out
    assert "Foo" in out and "sync failed entirely" in out
    assert "Bar: created 2" in out  # Bar still succeeded despite Foo's failure


def test_cmd_sync_asana_all_no_token_exits_nonzero(monkeypatch):
    monkeypatch.delenv("ASANA_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        outreach.cmd_sync_asana_all(argparse.Namespace())


# =============================================================================
# DM Asana sync — same Asana project, same stages, reusing every
# underlying function the email sync already uses and already has
# tested; new coverage here is for the DM-specific pieces only: the
# Shortlist-row adapter and the orchestration function that reads
# Shortlist instead of the outreach Sheet.
# =============================================================================

# =============================================================================
# decide_asana_sync_action's computed_stage override — the mechanism
# that lets a non-email lead type (e.g. a DM-routed Shortlist row, in
# discovery/sync_dm_asana.py) share this same decision logic and its
# human-only-stage protection, without needing a second copy of either.
# =============================================================================

def test_decide_action_computed_stage_override_used_instead_of_email_computation():
    """The actual mechanism that makes DM sharing possible without a
    second copy of the human-only-stage protection: passing
    computed_stage skips compute_lead_asana_stage's own email-specific
    derivation entirely."""
    lead = {}  # no email fields at all — compute_lead_asana_stage would say Sourced
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name=None,
                                                  computed_stage=outreach.ASANA_STAGE_NEGOTIATING)
    assert decision["target_stage"] == outreach.ASANA_STAGE_NEGOTIATING


def test_decide_action_computed_stage_defaults_to_email_computation_when_not_given():
    """Every existing email call site must be completely unaffected —
    omitting computed_stage falls back to the original behavior."""
    lead = {"IntroSentAt": "2026-08-01"}
    decision = outreach.decide_asana_sync_action(lead, current_asana_section_name=None)
    assert decision["target_stage"] == outreach.ASANA_STAGE_OUTREACH_SENT


def test_decide_action_computed_stage_still_respects_manual_only_protection():
    """The override must not bypass the human-only-stage guarantee —
    even an explicitly passed computed_stage must not move a task out
    of Rights Secured."""
    decision = outreach.decide_asana_sync_action(
        {}, current_asana_section_name="Rights Secured", existing_task_gid="123",
        computed_stage=outreach.ASANA_STAGE_NEGOTIATING)
    assert decision["target_stage"] is None


# ---------- _shortlist_row_to_asana_lead_shape ----------

