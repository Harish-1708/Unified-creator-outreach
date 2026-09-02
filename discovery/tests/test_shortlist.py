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

    def row_values(self, row_num):
        # Fake tabs in these tests are always constructed with the full,
        # current header already in place — matching a tab that's already
        # been through ensure_tab_headers() at least once. The self-healing
        # behavior itself (missing -> appended) is covered directly in
        # test_ensure_tab_headers_widens_a_stale_header below, against a
        # deliberately SHORT header, not through this shared fake.
        return list(self.headers) if row_num == 1 else []

    def update(self, range_str, values):
        # Functional (not just an assertion) so the sync-level integration
        # test below can exercise a real stale-header widening through
        # sync_shortlist() itself, not just the ensure_tab_headers()
        # function in isolation. Every other existing test's fakes already
        # have a full, current header, so this path is simply never
        # reached for them — unaffected either way.
        self.headers = self.headers + values[0]

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


def test_ensure_tab_headers_widens_a_stale_header_without_moving_data():
    """The actual bug: a Shortlist tab created before SECTOR_HEADERS grew
    (13 columns added across this build) never had its header row
    refreshed, so new columns were appended as real, correctly-positioned
    data with no label above them. This must self-heal: missing headers
    get appended, existing ones untouched, existing data never moves."""
    stale_header = ["dedup_key", "platform", "profile_link", "username"]
    ws = FakeWorksheet(stale_header, rows=[
        {"dedup_key": "instagram:dudedad", "platform": "instagram",
         "profile_link": "https://instagram.com/dudedad", "username": "dudedad"},
    ])

    class WideningFakeWorksheet(FakeWorksheet):
        def row_values(self, row_num):
            return list(self.headers) if row_num == 1 else []

        def update(self, range_str, values):
            # Mirrors the real behavior ensure_tab_headers relies on:
            # appends new header cells starting at range_str, doesn't
            # touch anything already there.
            self.headers = self.headers + values[0]

    ws.__class__ = WideningFakeWorksheet

    full_headers = shortlist.SHORTLIST_HEADERS
    result = shortlist.ensure_tab_headers(ws, full_headers)

    assert result == full_headers
    assert ws.headers[:4] == stale_header  # original columns untouched, not reordered
    # The row that existed before widening must still read correctly by
    # its original column names — nothing about the underlying data moved.
    assert ws.get_all_records()[0]["username"] == "dudedad"


def test_sync_shortlist_widens_an_already_existing_stale_shortlist_tab(monkeypatch):
    """Closes the gap the sabotage check exposed: the other sync_shortlist
    tests all use fakes whose headers are already current, so they'd pass
    even if the widening call were silently removed. This one gives
    sync_shortlist() a Shortlist tab that ALREADY EXISTS with a stale,
    short header — exactly the real-world scenario reported — and checks
    that running sync_shortlist() itself widens it, not just that the
    ensure_tab_headers() function works when called directly."""
    fake_sheet = FakeSheet([_master_row("instagram:dudedad", "DudeRobe", "Approved")])
    stale_shortlist = FakeWorksheet(["dedup_key", "platform", "profile_link", "username"])
    fake_sheet._tabs["Shortlist"] = stale_shortlist

    shortlist_ws = _run_sync(monkeypatch, fake_sheet)

    assert set(shortlist.SHORTLIST_HEADERS).issubset(set(shortlist_ws.headers))
    assert shortlist_ws.headers[:4] == ["dedup_key", "platform", "profile_link", "username"]
