import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from preview_logic import list_campaigns, get_campaign_cfg, run_preview


def test_list_campaigns_finds_the_real_sample_campaign():
    campaigns = list_campaigns()
    assert "Kelson_Creators_Licensing" in campaigns


def test_get_campaign_cfg_matches_outreach_get_campaign_directly():
    cfg = get_campaign_cfg("Kelson_Creators_Licensing")
    assert cfg["_campaign_name"] == "Kelson_Creators_Licensing"
    assert len(cfg["stages"]) == 5  # this sample campaign has all 5 stages built
    assert cfg["variants"] == ["A", "B", "C", "D"]


def test_get_campaign_cfg_raises_clear_error_for_unknown_campaign():
    with pytest.raises(Exception, match="No templates found"):
        get_campaign_cfg("Definitely_Not_A_Real_Campaign")


def test_run_preview_renders_real_templates_for_a_fake_lead():
    leads = [{
        "_row": 2, "LeadID": "L1", "FirstName": "Jordan", "LastName": "Lee",
        "Email": "jordan@example.com", "Company": "Acme Studios", "Approval": "Yes",
    }]
    plan = run_preview("Kelson_Creators_Licensing", "intro", 10, leads)
    assert len(plan) == 1
    assert plan[0]["variant"] in ["A", "B", "C", "D"]
    assert "Subject" not in plan[0]["subject"]  # rendered subject, not the raw "Subject:" prefix
    assert plan[0]["lead"]["Email"] == "jordan@example.com"


def test_run_preview_excludes_leads_missing_approval():
    leads = [{
        "_row": 2, "LeadID": "L1", "Email": "jordan@example.com", "Approval": "",
    }]
    plan = run_preview("Kelson_Creators_Licensing", "intro", 10, leads)
    assert plan == []


def test_run_preview_respects_forced_variant():
    leads = [{
        "_row": 2, "LeadID": "L1", "Email": "jordan@example.com", "Approval": "Yes",
    }]
    plan = run_preview("Kelson_Creators_Licensing", "intro", 10, leads, forced_variant="C")
    assert plan[0]["variant"] == "C"


def test_run_preview_ignore_wait_days_makes_not_yet_due_followup_visible():
    from datetime import datetime

    recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    leads = [{
        "_row": 2, "LeadID": "L1", "Email": "jordan@example.com", "Approval": "Yes",
        "IntroSentAt": recent, "IntroVariant": "A",
    }]

    plan_default = run_preview("Kelson_Creators_Licensing", "followup1", 10, leads)
    assert plan_default == []  # not due yet under the campaign's normal wait_days

    plan_override = run_preview("Kelson_Creators_Licensing", "followup1", 10, leads, ignore_wait_days=True)
    assert len(plan_override) == 1
