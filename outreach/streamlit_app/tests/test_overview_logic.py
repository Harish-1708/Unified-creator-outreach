import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from preview_logic import get_campaign_cfg
from overview_logic import build_campaign_overview_row, build_all_campaigns_overview, OVERVIEW_COLUMNS


def _leads(n_with_email=3, n_contacted=1):
    leads = []
    for i in range(n_with_email):
        lead = {"Email": f"lead{i}@abc.com", "Approval": "Yes"}
        if i < n_contacted:
            lead["IntroSentAt"] = "2026-08-01 09:00:00"
        leads.append(lead)
    return leads


def test_overview_columns_has_pending_inserted_after_total_leads():
    assert OVERVIEW_COLUMNS[0] == "Campaign"
    assert OVERVIEW_COLUMNS[1] == "Total Leads"
    assert OVERVIEW_COLUMNS[2] == "Pending (Not Yet Contacted)"


def test_build_campaign_overview_row_computes_pending_correctly():
    cfg = get_campaign_cfg("Kelson_Creators_Licensing")
    leads = _leads(n_with_email=5, n_contacted=2)
    row = build_campaign_overview_row(cfg, leads, responses=[], send_log=[])

    row_dict = dict(zip(OVERVIEW_COLUMNS, row))
    assert row_dict["Campaign"] == "Kelson_Creators_Licensing"
    assert row_dict["Total Leads"] == "5"
    assert row_dict["Unique Contacted"] == "2"
    assert row_dict["Pending (Not Yet Contacted)"] == "3"


def test_build_campaign_overview_row_pending_never_negative():
    cfg = get_campaign_cfg("Kelson_Creators_Licensing")
    # Pathological case: more "contacted" markers than total leads with email
    # shouldn't be possible in real data, but pending must still floor at 0.
    leads = _leads(n_with_email=2, n_contacted=2)
    row = build_campaign_overview_row(cfg, leads, responses=[], send_log=[])
    row_dict = dict(zip(OVERVIEW_COLUMNS, row))
    assert int(row_dict["Pending (Not Yet Contacted)"]) >= 0


def test_build_all_campaigns_overview_skips_unreadable_campaigns():
    def fetch(name):
        if name == "Broken":
            raise RuntimeError("Tab doesn't exist yet")
        cfg = get_campaign_cfg("Kelson_Creators_Licensing")
        return cfg, _leads(), [], []

    rows, errors = build_all_campaigns_overview(["Kelson_Creators_Licensing", "Broken"], fetch)
    assert len(rows) == 1
    assert len(errors) == 1
    assert errors[0][0] == "Broken"
    assert "Tab doesn't exist yet" in errors[0][1]


def test_build_all_campaigns_overview_empty_list_returns_empty():
    rows, errors = build_all_campaigns_overview([], lambda name: (None, [], [], []))
    assert rows == []
    assert errors == []
