"""Tests for the campaign-scoped identity changes made to discover.py:
- CAMPAIGN becomes a required config input
- Master row identity is (dedup_key, Campaign), not dedup_key alone
- review_status / outreach_channel / campaign_push_status replace Shortlisted

These are new tests for new/changed logic — discover.py has no pre-existing
test suite, so there's nothing to regress against here; this establishes a
baseline for these specific fields going forward. Discovery/scoring/
enrichment logic itself is NOT touched or tested here — only the identity
and config changes made for multi-campaign support.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import discover


# ---------- get_config() requires CAMPAIGN ----------

REQUIRED_ENV = {
    "CAMPAIGN": "DudeRobe",
    "NICHE": "loungewear",
    "BRAND_NAME": "DudeRobe",
    "LOCATION": "USA",
    "PLATFORM": "instagram",
    "TARGET_GENDER": "male",
    "RESULT_LIMIT": "5",
}


def _set_env(monkeypatch, overrides=None):
    overrides = overrides or {}
    for key in list(REQUIRED_ENV) + ["SEARCH_BUDGET", "LLM_CANDIDATE_LIMIT", "MIN_FOLLOWERS",
                                      "MAX_FOLLOWERS", "CREATOR_SIZE_TIER", "GEMINI_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_get_config_requires_campaign(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("CAMPAIGN", raising=False)
    with pytest.raises(ValueError, match="CAMPAIGN"):
        discover.get_config()


def test_get_config_returns_campaign(monkeypatch):
    _set_env(monkeypatch)
    cfg = discover.get_config()
    assert cfg["campaign"] == "DudeRobe"


def test_get_config_campaign_independent_of_brand_name(monkeypatch):
    """campaign and brand_name are deliberately separate fields — a
    differently-named campaign against the same brand must not be silently
    coerced to match brand_name."""
    _set_env(monkeypatch, {"CAMPAIGN": "DudeRobe_Relaunch_2026", "BRAND_NAME": "DudeRobe"})
    cfg = discover.get_config()
    assert cfg["campaign"] == "DudeRobe_Relaunch_2026"
    assert cfg["brand_name"] == "DudeRobe"


# ---------- MASTER_HEADERS shape ----------

def test_master_headers_has_campaign_and_no_shortlisted():
    assert "Campaign" in discover.MASTER_HEADERS
    assert "Shortlisted" not in discover.MASTER_HEADERS
    assert "review_status" in discover.MASTER_HEADERS
    assert "outreach_channel" in discover.MASTER_HEADERS
    assert "campaign_push_status" in discover.MASTER_HEADERS


def test_run_log_headers_has_campaign():
    assert "campaign" in discover.RUN_LOG_HEADERS


# ---------- Fakes for Sheets-backed functions ----------

class FakeMasterWorksheet:
    """Minimal stand-in for a gspread Worksheet, holding rows as plain
    dicts (get_all_records) and as a raw grid (get_all_values), matching
    the two different read APIs load_master_keys()/load_master_index()
    actually use."""

    def __init__(self, headers, rows=None, title="FakeTab"):
        self.headers = headers
        self._rows = rows or []  # list of dicts
        self.title = title
        self.id = "fake-tab-id"

    def get_all_records(self):
        return [dict(r) for r in self._rows]

    def get_all_values(self):
        grid = [self.headers]
        for r in self._rows:
            grid.append([r.get(h, "") for h in self.headers])
        return grid

    def update_cell(self, row_num, col, value):
        # row_num is 1-indexed with header as row 1
        r = self._rows[row_num - 2]
        header_name = self.headers[col - 1]
        r[header_name] = value

    def append_rows(self, rows):
        for row in rows:
            self._rows.append(dict(zip(self.headers, row)))

    # -- ensure_tab_headers()'s interface — headers already match
    # required_headers exactly in every test below, so the "missing
    # columns" branch (append_row / update) is never actually exercised;
    # these exist only so ensure_tab_headers' row_values(1) check succeeds.
    def row_values(self, row_num):
        return list(self.headers) if row_num == 1 else []

    def append_row(self, row):
        self._rows.append(dict(zip(self.headers, row)))

    def update(self, range_str, values):  # pragma: no cover - not exercised
        raise AssertionError("update() should not be called — headers already match required_headers")


class _FakeSheetRef:
    """Only ever used for its .id attribute in a print statement — write_batch
    and write_excluded don't call anything else on the sheet object once
    get_or_create_tab is monkeypatched out."""
    id = "fake-sheet-id"


def _row(dedup_key, campaign, **extra):
    base = {h: "" for h in discover.MASTER_HEADERS}
    base["dedup_key"] = dedup_key
    base["Campaign"] = campaign
    base.update(extra)
    return base


# ---------- load_master_keys() / load_master_index() are campaign-scoped ----------

def test_load_master_keys_is_scoped_by_campaign():
    ws = FakeMasterWorksheet(discover.MASTER_HEADERS, rows=[
        _row("instagram:samecreator", "DudeRobe"),
    ])
    keys = discover.load_master_keys(ws)
    assert ("instagram:samecreator", "DudeRobe") in keys
    # Sabotage check: the same account under a DIFFERENT campaign must NOT
    # be considered "already known" — this is the entire point of the
    # compound key. If this assertion is ever false, a creator legitimately
    # relevant to a second campaign would be silently skipped.
    assert ("instagram:samecreator", "SheRobe") not in keys


def test_load_master_index_scoped_by_campaign():
    ws = FakeMasterWorksheet(discover.MASTER_HEADERS, rows=[
        _row("instagram:samecreator", "DudeRobe", **{"Niche 1": "loungewear"}),
    ])
    index = discover.load_master_index(ws)
    assert ("instagram:samecreator", "DudeRobe") in index
    assert ("instagram:samecreator", "SheRobe") not in index


def test_write_batch_deduplicates_same_creator_same_campaign(monkeypatch):
    """The other half of the compound-key story: two runs for the SAME
    campaign finding the SAME creator again must NOT create a second row —
    only a *different* campaign should. A key computation that silently
    drops campaign (comparing against master_index's tuple keys with a
    bare string) would fail this by treating every re-run as new."""
    master_ws = FakeMasterWorksheet(discover.MASTER_HEADERS)
    sector_ws = FakeMasterWorksheet(discover.SECTOR_HEADERS)
    monkeypatch.setattr(discover, "get_or_create_tab",
                         lambda sheet, name, headers: master_ws if headers is discover.MASTER_HEADERS else sector_ws)

    creator = {"dedup_key": "instagram:samecreator", "username": "samecreator",
               "date_added": "2026-09-01"}
    discover.write_batch(_FakeSheetRef(), master_ws, sector_name="Loungewear", creators=[dict(creator)],
                          sector_label="loungewear", campaign="DudeRobe")
    # Same creator, same campaign, second run (e.g. found again via a
    # different search lane in a later discovery run).
    discover.write_batch(_FakeSheetRef(), master_ws, sector_name="Loungewear", creators=[dict(creator)],
                          sector_label="loungewear", campaign="DudeRobe")

    rows = master_ws.get_all_records()
    assert len(rows) == 1, f"expected exactly 1 row for repeated (creator, campaign), got {len(rows)}"


def test_write_batch_creates_independent_rows_per_campaign(monkeypatch):
    """The sabotage scenario this whole change exists to prevent: the same
    creator found under two different campaigns must produce two rows, not
    one row silently reused (which would mix one campaign's review/outreach
    state into another's).

    get_or_create_tab()'s own tab-creation/column-migration behavior isn't
    what changed here, so it's monkeypatched to hand back the fakes
    directly rather than re-implementing that machinery in a fake."""
    master_ws = FakeMasterWorksheet(discover.MASTER_HEADERS)
    sector_ws = FakeMasterWorksheet(discover.SECTOR_HEADERS)
    monkeypatch.setattr(discover, "get_or_create_tab",
                         lambda sheet, name, headers: master_ws if headers is discover.MASTER_HEADERS else sector_ws)

    creator = {"dedup_key": "instagram:samecreator", "username": "samecreator",
               "date_added": "2026-09-01"}
    discover.write_batch(_FakeSheetRef(), master_ws, sector_name="Loungewear", creators=[dict(creator)],
                          sector_label="loungewear", campaign="DudeRobe")
    discover.write_batch(_FakeSheetRef(), master_ws, sector_name="Athleisure", creators=[dict(creator)],
                          sector_label="athleisure", campaign="SheRobe")

    campaigns_written = {r["Campaign"] for r in master_ws.get_all_records()}
    assert campaigns_written == {"DudeRobe", "SheRobe"}
    assert len(master_ws.get_all_records()) == 2


def test_write_excluded_stamps_campaign(monkeypatch):
    excluded = [{"dedup_key": "instagram:rejected1", "rejection_reason": "wrong account type"}]
    ws = FakeMasterWorksheet(discover.EXCLUDED_HEADERS)
    monkeypatch.setattr(discover, "get_or_create_tab", lambda sheet, name, headers: ws)

    discover.write_excluded(_FakeSheetRef(), excluded, campaign="DudeRobe")
    assert ws.get_all_records()[0]["Campaign"] == "DudeRobe"


def test_dr_audience_gender_and_concerns_reach_master_row():
    """Real gap found reviewing an actual Deep Research report against the
    Master sheet: audience_gender/research_confidence/concerns were already
    extracted and already fed into scoring, but never reached MASTER_HEADERS
    — a human reviewer had no direct visibility into them, only the LLM did."""
    creator = {h: "" for h in discover.MASTER_HEADERS}
    creator["dr_audience_gender"] = "72.3% Female / 27.7% Male"
    creator["dr_research_confidence"] = "HIGH"
    creator["dr_concerns"] = "Premium sponsorship rates due to mega tier scale"

    row = discover.build_master_row(creator, primary_niche="men's loungewear")
    row_by_header = dict(zip(discover.MASTER_HEADERS, row))

    assert row_by_header["dr_audience_gender"] == "72.3% Female / 27.7% Male"
    assert row_by_header["dr_research_confidence"] == "HIGH"
    assert row_by_header["dr_concerns"] == "Premium sponsorship rates due to mega tier scale"
