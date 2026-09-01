"""Tests for shortlist.py's changes:
- sync condition is review_status == "approved" (was Shortlisted == "Y")
- dedup against the Shortlist tab is (dedup_key, Campaign)-scoped
- SECTOR_HEADERS matches discover.py's derivation exactly (the column-
  misalignment risk the file's own docstring warns about)

No pre-existing test suite for this repo to regress against — these are
new tests for new/changed logic only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread

import discover
import shortlist


class FakeWorksheet:
    def __init__(self, headers, rows=None):
        self.headers = headers
        self._rows = rows or []

    def get_all_records(self):
        return [dict(r) for r in self._rows]

    def append_row(self, row):
        # In this codebase, append_row is only ever used to write the header
        # row when a tab is freshly created (headers are already fixed at
        # construction time here) — never a real data row, so this is a
        # deliberate no-op rather than something that would double as a
        # data record and pollute get_all_records().
        pass

    def append_rows(self, rows):
        for row in rows:
            self._rows.append(dict(zip(self.headers, row)))


class FakeSheet:
    def __init__(self, master_rows):
        self._tabs = {"Master": FakeWorksheet(discover.MASTER_HEADERS, master_rows)}

    def worksheet(self, title):
        if title not in self._tabs:
            raise gspread.WorksheetNotFound(title)
        return self._tabs[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(shortlist.SHORTLIST_HEADERS)
        self._tabs[title] = ws
        return ws


def _master_row(dedup_key, campaign, review_status, **extra):
    row = {h: "" for h in discover.MASTER_HEADERS}
    row["dedup_key"] = dedup_key
    row["Campaign"] = campaign
    row["review_status"] = review_status
    row.update(extra)
    return row


def _run_sync(monkeypatch, fake_sheet, env=None):
    monkeypatch.setattr(shortlist.Credentials, "from_service_account_file", lambda *a, **k: object())
    monkeypatch.setattr(shortlist.gspread, "authorize", lambda creds: _FakeClient(fake_sheet))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "fake-path.json")
    monkeypatch.setenv("SPREADSHEET_ID", "fake-sheet-id")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    shortlist.sync_shortlist()
    return fake_sheet.worksheet("Shortlist")


class _FakeClient:
    def __init__(self, sheet):
        self._sheet = sheet

    def open_by_key(self, sheet_id):
        return self._sheet


# ---------- SECTOR_HEADERS alignment ----------

def test_sector_headers_matches_discover_derivation():
    """The exact failure mode the file's own docstring warns about — this
    would have caught the pre-existing missing-follower_source bug."""
    expected = [h for h in discover.MASTER_HEADERS if h not in discover.NICHE_COLS]
    assert shortlist.SECTOR_HEADERS == expected


# ---------- review_status gate ----------

def test_sync_only_copies_approved_rows(monkeypatch):
    fake_sheet = FakeSheet([
        _master_row("instagram:approved1", "DudeRobe", "Approved"),
        _master_row("instagram:pending1", "DudeRobe", ""),
        _master_row("instagram:rejected1", "DudeRobe", "Rejected"),
        _master_row("instagram:oldyes", "DudeRobe", "Y"),  # old-style value, must NOT be treated as approved
    ])
    shortlist_ws = _run_sync(monkeypatch, fake_sheet)
    copied_keys = {r["dedup_key"] for r in shortlist_ws.get_all_records()}
    assert copied_keys == {"instagram:approved1"}


def test_sync_is_case_insensitive_on_approved(monkeypatch):
    fake_sheet = FakeSheet([_master_row("instagram:approved1", "DudeRobe", "approved")])
    shortlist_ws = _run_sync(monkeypatch, fake_sheet)
    assert len(shortlist_ws.get_all_records()) == 1


# ---------- campaign-scoped dedup ----------

def test_sync_dedup_is_campaign_scoped(monkeypatch):
    """Same account, approved independently for two campaigns — both must
    land in Shortlist as separate rows, not deduped against each other."""
    fake_sheet = FakeSheet([
        _master_row("instagram:samecreator", "DudeRobe", "Approved"),
        _master_row("instagram:samecreator", "SheRobe", "Approved"),
    ])
    shortlist_ws = _run_sync(monkeypatch, fake_sheet)
    rows = shortlist_ws.get_all_records()
    assert len(rows) == 2
    assert {r["Campaign"] for r in rows} == {"DudeRobe", "SheRobe"}


def test_sync_skips_rows_already_in_shortlist_for_same_campaign(monkeypatch):
    fake_sheet = FakeSheet([_master_row("instagram:already", "DudeRobe", "Approved")])
    existing = FakeWorksheet(shortlist.SHORTLIST_HEADERS, rows=[
        {"dedup_key": "instagram:already", "Campaign": "DudeRobe"},
    ])
    fake_sheet._tabs["Shortlist"] = existing
    shortlist_ws = _run_sync(monkeypatch, fake_sheet)
    assert len(shortlist_ws.get_all_records()) == 1  # still just the pre-existing one, not duplicated
