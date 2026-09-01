import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from campaign_status_logic import (
    compute_campaign_readiness, is_lead_finished, compute_campaign_is_complete,
    compute_campaign_status, status_label,
    STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_ATTENTION, STATUS_DELETED,
)

STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
]


def _cfg(**overrides):
    cfg = {
        "stages": STAGES, "_global_default_account": "sales1",
        "sending": {}, "status": "active",
    }
    cfg.update(overrides)
    return cfg


def _lead(**overrides):
    lead = {"Approval": "Yes", "Email": "a@abc.com", "Status": "", "IntroSentAt": "", "FollowUp1SentAt": ""}
    lead.update(overrides)
    return lead


# ---------- readiness ----------

def test_readiness_passes_with_global_default_account_stages_and_approved_lead():
    ready, problems = compute_campaign_readiness(_cfg(), [_lead()])
    assert ready is True
    assert problems == []


def test_readiness_fails_with_no_sender_account():
    cfg = _cfg(_global_default_account="", sending={})
    ready, problems = compute_campaign_readiness(cfg, [_lead()])
    assert ready is False
    assert "No sender account configured" in problems


def test_readiness_passes_with_sender_rotation_true_even_without_explicit_accounts():
    # Heuristic, not a guarantee — real capacity is still checked server-side.
    cfg = _cfg(_global_default_account="", sending={"sender_rotation": True})
    ready, _ = compute_campaign_readiness(cfg, [_lead()])
    assert ready is True


def test_readiness_fails_with_no_stages():
    cfg = _cfg(stages=[])
    ready, problems = compute_campaign_readiness(cfg, [_lead()])
    assert ready is False
    assert "No template stages found" in problems


def test_readiness_fails_with_no_approved_leads():
    ready, problems = compute_campaign_readiness(_cfg(), [_lead(Approval="No")])
    assert ready is False
    assert any("approved" in p for p in problems)


def test_readiness_fails_with_approved_lead_missing_email():
    ready, problems = compute_campaign_readiness(_cfg(), [_lead(Email="")])
    assert ready is False


def test_readiness_reports_multiple_problems_at_once():
    cfg = _cfg(_global_default_account="", sending={}, stages=[])
    ready, problems = compute_campaign_readiness(cfg, [])
    assert ready is False
    assert len(problems) == 3


# ---------- is_lead_finished / is_complete ----------

def test_lead_finished_when_status_stopped():
    assert is_lead_finished(_lead(Status="Stopped - Replied"), STAGES) is True


def test_lead_finished_when_last_stage_sent():
    assert is_lead_finished(_lead(FollowUp1SentAt="2026-08-01 09:00:00"), STAGES) is True


def test_lead_not_finished_when_only_intro_sent_and_more_stages_remain():
    assert is_lead_finished(_lead(IntroSentAt="2026-08-01 09:00:00"), STAGES) is False


def test_lead_not_finished_with_no_stages_and_not_stopped():
    assert is_lead_finished(_lead(), []) is False


def test_campaign_not_complete_with_zero_approved_leads():
    assert compute_campaign_is_complete(_cfg(), []) is False
    assert compute_campaign_is_complete(_cfg(), [_lead(Approval="No")]) is False


def test_campaign_complete_when_every_approved_lead_finished():
    leads = [_lead(FollowUp1SentAt="2026-08-01 09:00:00"), _lead(Status="Stopped - Bounced")]
    assert compute_campaign_is_complete(_cfg(), leads) is True


def test_campaign_not_complete_when_one_lead_still_pending():
    leads = [_lead(FollowUp1SentAt="2026-08-01 09:00:00"), _lead(IntroSentAt="2026-08-01 09:00:00")]
    assert compute_campaign_is_complete(_cfg(), leads) is False


def test_campaign_complete_ignores_unapproved_leads():
    leads = [_lead(FollowUp1SentAt="2026-08-01 09:00:00"), _lead(Approval="No", IntroSentAt="")]
    assert compute_campaign_is_complete(_cfg(), leads) is True


# ---------- compute_campaign_status — the combined status ----------

def test_status_draft_from_explicit_config():
    status, problems = compute_campaign_status(_cfg(status="draft"), [_lead()])
    assert status == STATUS_DRAFT
    assert problems == []


def test_status_paused_from_explicit_config():
    status, problems = compute_campaign_status(_cfg(status="paused"), [_lead()])
    assert status == STATUS_PAUSED
    assert problems == []


def test_status_running_when_active_ready_and_not_complete():
    status, problems = compute_campaign_status(_cfg(status="active"), [_lead()])
    assert status == STATUS_RUNNING
    assert problems == []


def test_status_completed_when_active_ready_and_all_leads_finished():
    leads = [_lead(FollowUp1SentAt="2026-08-01 09:00:00")]
    status, problems = compute_campaign_status(_cfg(status="active"), leads)
    assert status == STATUS_COMPLETED


def test_status_attention_when_active_but_not_ready():
    cfg = _cfg(status="active", _global_default_account="", sending={})
    status, problems = compute_campaign_status(cfg, [_lead()])
    assert status == STATUS_ATTENTION
    assert len(problems) > 0


def test_status_missing_status_key_defaults_to_active_behavior():
    cfg = _cfg()
    del cfg["status"]
    status, _ = compute_campaign_status(cfg, [_lead()])
    assert status == STATUS_RUNNING  # not draft, not paused — backward compatible


def test_status_unrecognized_value_treated_as_active_not_hidden():
    # A typo in status shouldn't silently make a campaign disappear from view.
    status, _ = compute_campaign_status(_cfg(status="oops_typo"), [_lead()])
    assert status == STATUS_RUNNING


def test_status_paused_wins_over_readiness_problems():
    # Even a mis-configured campaign that's explicitly paused should show
    # as Paused, not Attention — the human already knows and stopped it.
    cfg = _cfg(status="paused", _global_default_account="", sending={})
    status, problems = compute_campaign_status(cfg, [_lead()])
    assert status == STATUS_PAUSED
    assert problems == []


# ---------- status_label ----------

def test_status_label_known_values():
    assert status_label(STATUS_DRAFT) == "📝 Draft"
    assert status_label(STATUS_RUNNING) == "🟢 Running"
    assert status_label(STATUS_PAUSED) == "⏸ Paused"
    assert status_label(STATUS_COMPLETED) == "✅ Completed"
    assert status_label(STATUS_ATTENTION) == "⚠️ Attention needed"


def test_status_label_unknown_value_passthrough():
    assert status_label("something_else") == "something_else"


# ---------- deleted (temporary removal) ----------

def test_compute_campaign_status_deleted():
    campaign_cfg = {"status": "deleted", "stages": [{"name": "Intro"}], "_global_default_account": "sales1"}
    status, problems = compute_campaign_status(campaign_cfg, [])
    assert status == STATUS_DELETED
    assert problems == []


def test_compute_campaign_status_deleted_takes_precedence_over_readiness_issues():
    """A deleted campaign shouldn't show as 'Attention needed' just
    because it also happens to be missing a sender — deleted is checked
    first, unconditionally."""
    campaign_cfg = {"status": "deleted"}  # no sender, no stages — would be Attention if not deleted
    status, problems = compute_campaign_status(campaign_cfg, [])
    assert status == STATUS_DELETED


def test_status_label_deleted():
    assert "Deleted" in status_label(STATUS_DELETED)
