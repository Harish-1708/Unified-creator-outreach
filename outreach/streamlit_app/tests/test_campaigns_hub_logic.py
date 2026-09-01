import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from campaigns_hub_logic import (
    compute_last_activity_timestamp, build_draft_campaign_row, build_campaign_hub_row,
    build_campaigns_hub, filter_campaigns_by_search, build_deleted_campaign_row,
)

STAGES = [{"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0}]


def _cfg(name="Foo", status="active"):
    return {"_campaign_name": name, "stages": STAGES, "status": status,
            "_global_default_account": "sales1", "sending": {}}


def _lead(**overrides):
    lead = {"Approval": "Yes", "Email": "a@abc.com", "Status": "", "IntroSentAt": ""}
    lead.update(overrides)
    return lead


# ---------- compute_last_activity_timestamp ----------

def test_last_activity_picks_max_timestamp():
    send_log = [{"Timestamp": "2026-08-20 10:00:00"}, {"Timestamp": "2026-08-25 09:00:00"}]
    assert compute_last_activity_timestamp(send_log) == "2026-08-25 09:00:00"


def test_last_activity_empty_send_log():
    assert compute_last_activity_timestamp([]) == ""


def test_last_activity_ignores_blank_timestamps():
    send_log = [{"Timestamp": ""}, {"Timestamp": "2026-08-20 10:00:00"}]
    assert compute_last_activity_timestamp(send_log) == "2026-08-20 10:00:00"


# ---------- build_draft_campaign_row ----------

def test_draft_row_has_zero_metrics_and_draft_status():
    row = build_draft_campaign_row(_cfg("NewCampaign", status="draft"))
    assert row["name"] == "NewCampaign"
    assert row["status"] == "draft"
    assert row["total_leads"] == 0
    assert row["sent"] == 0


# ---------- build_campaign_hub_row ----------

def test_hub_row_computes_status_and_metrics_together():
    two_stage_cfg = _cfg()
    two_stage_cfg["stages"] = [
        {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
        {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
    ]
    leads = [_lead(IntroSentAt="2026-08-01 09:00:00")]  # intro sent, followup1 not — still running
    send_log = [{"Status": "sent", "Timestamp": "2026-08-01 09:00:00"}]
    row = build_campaign_hub_row(two_stage_cfg, leads, [], send_log)
    assert row["name"] == "Foo"
    assert row["status"] == "running"
    assert row["total_leads"] == 1
    assert row["sent"] == 1
    assert row["last_activity"] == "2026-08-01 09:00:00"


def test_hub_row_reflects_attention_status():
    cfg = _cfg()
    cfg["_global_default_account"] = ""
    row = build_campaign_hub_row(cfg, [_lead()], [], [])
    assert row["status"] == "attention"
    assert len(row["problems"]) > 0


# ---------- build_campaigns_hub ----------

def test_build_campaigns_hub_skips_sheet_fetch_for_draft_campaigns():
    fetch_calls = []

    def get_cfg(name):
        return _cfg(name, status="draft" if name == "NewOne" else "active")

    def fetch(campaign_cfg):
        fetch_calls.append(campaign_cfg["_campaign_name"])
        return [_lead()], [], [{"Status": "sent", "Timestamp": "2026-08-01 09:00:00"}]

    rows, deleted_rows, errors = build_campaigns_hub(["NewOne", "Established"], get_cfg, fetch)

    assert len(rows) == 2
    assert fetch_calls == ["Established"]  # never fetched Sheet data for the draft one
    by_name = {r["name"]: r for r in rows}
    assert by_name["NewOne"]["status"] == "draft"
    assert by_name["Established"]["status"] == "running"


def test_build_campaigns_hub_isolates_per_campaign_config_errors():
    def get_cfg(name):
        if name == "Broken":
            raise Exception("No templates found")
        return _cfg(name)

    def fetch(campaign_cfg):
        return [_lead()], [], []

    rows, deleted_rows, errors = build_campaigns_hub(["Broken", "Fine"], get_cfg, fetch)
    assert len(rows) == 1
    assert rows[0]["name"] == "Fine"
    assert errors == [("Broken", "No templates found")]


def test_build_campaigns_hub_isolates_per_campaign_sheet_errors():
    def get_cfg(name):
        return _cfg(name)

    def fetch(campaign_cfg):
        if campaign_cfg["_campaign_name"] == "NoTabsYet":
            raise Exception("Tab doesn't exist yet")
        return [_lead()], [], []

    rows, deleted_rows, errors = build_campaigns_hub(["NoTabsYet", "Fine"], get_cfg, fetch)
    assert len(rows) == 1
    assert rows[0]["name"] == "Fine"
    assert errors == [("NoTabsYet", "Tab doesn't exist yet")]


def test_build_campaigns_hub_empty_list():
    rows, deleted_rows, errors = build_campaigns_hub([], lambda n: _cfg(n), lambda c: ([], [], []))
    assert rows == []
    assert errors == []


def test_build_campaigns_hub_excludes_deleted_campaigns_from_normal_rows():
    def get_cfg(name):
        status = "deleted" if name == "Removed" else "active"
        return _cfg(name, status=status)

    def fetch(campaign_cfg):
        return [_lead()], [], []

    rows, deleted_rows, errors = build_campaigns_hub(["Removed", "Fine"], get_cfg, fetch)
    assert [r["name"] for r in rows] == ["Fine"]  # deleted one never appears here
    assert [r["name"] for r in deleted_rows] == ["Removed"]
    assert errors == []


def test_build_campaigns_hub_deleted_campaign_never_needs_a_sheet_fetch():
    fetch_calls = []

    def get_cfg(name):
        return _cfg(name, status="deleted")

    def fetch(campaign_cfg):
        fetch_calls.append(campaign_cfg["_campaign_name"])
        return [], [], []

    build_campaigns_hub(["Removed"], get_cfg, fetch)
    assert fetch_calls == []  # same principle as Draft — status is knowable from config alone


def test_build_deleted_campaign_row_shape():
    row = build_deleted_campaign_row(_cfg("Removed", status="deleted"))
    assert row["name"] == "Removed"
    assert row["status"] == "deleted"
    assert "Deleted" in row["status_label"]


# ---------- filter_campaigns_by_search ----------

def test_filter_by_search_case_insensitive_substring():
    rows = [{"name": "DudeRobe"}, {"name": "SheRobe"}, {"name": "TestCampaign"}]
    result = filter_campaigns_by_search(rows, "robe")
    assert {r["name"] for r in result} == {"DudeRobe", "SheRobe"}


def test_filter_by_search_empty_query_returns_all():
    rows = [{"name": "A"}, {"name": "B"}]
    assert filter_campaigns_by_search(rows, "") == rows
    assert filter_campaigns_by_search(rows, "   ") == rows


def test_filter_by_search_no_match_returns_empty():
    rows = [{"name": "A"}]
    assert filter_campaigns_by_search(rows, "zzz") == []
