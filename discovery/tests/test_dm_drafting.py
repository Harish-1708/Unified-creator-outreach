"""Tests for the campaign/channel filter added to dm_drafting.py.

Before this change, draft_all_pending() drafted DMs for every Shortlist
row with fit_reasoning filled in, regardless of which campaign approved it
or which channel was chosen — meaning a "Draft DMs" run for one brand
would also draft (and spend a real Claude call on) rows belonging to a
completely different brand, and rows a human had explicitly routed to
email instead of DM. These tests exist specifically to guard that gap.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread

import dm_drafting

SHORTLIST_HEADERS = [
    "dedup_key", "platform", "username", "Campaign", "outreach_channel",
    "fit_reasoning", "personalization_notes", "dm_draft", "dm_reasoning", "dm_status",
]


class FakeWorksheet:
    def __init__(self, headers, rows=None):
        self.headers = headers
        self._rows = rows or []  # list of dicts

    def get_all_records(self):
        return [dict(r) for r in self._rows]

    def row_values(self, row_num):
        return list(self.headers) if row_num == 1 else []

    def update_cell(self, row_num, col, value):
        r = self._rows[row_num - 2]
        r[self.headers[col - 1]] = value

    def append_row(self, row):
        pass  # header-write no-op, same rationale as test_shortlist.py's fake

    def append_rows(self, rows):
        for row in rows:
            self._rows.append(dict(zip(self.headers, row)))


class FakeSheet:
    def __init__(self, shortlist_rows):
        self._tabs = {"Shortlist": FakeWorksheet(SHORTLIST_HEADERS, shortlist_rows)}

    def worksheet(self, title):
        if title not in self._tabs:
            raise gspread.WorksheetNotFound(title)
        return self._tabs[title]

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(dm_drafting.CONTACT_HISTORY_HEADERS)
        self._tabs[title] = ws
        return ws


class _FakeClient:
    def __init__(self, sheet):
        self._sheet = sheet

    def open_by_key(self, sheet_id):
        return self._sheet


def _row(dedup_key, campaign, channel, fit_reasoning="looks like a good fit", **extra):
    row = {h: "" for h in SHORTLIST_HEADERS}
    row.update({
        "dedup_key": dedup_key, "username": dedup_key.split(":")[-1], "platform": "instagram",
        "Campaign": campaign, "outreach_channel": channel, "fit_reasoning": fit_reasoning,
    })
    row.update(extra)
    return row


def _run_draft(monkeypatch, fake_sheet, campaign="DudeRobe", brand_name="DudeRobe"):
    monkeypatch.setattr(dm_drafting.Credentials, "from_service_account_file", lambda *a, **k: object())
    monkeypatch.setattr(dm_drafting.gspread, "authorize", lambda creds: _FakeClient(fake_sheet))
    monkeypatch.setattr(dm_drafting, "draft_dm", lambda creator, brand, ctx, prior_contact_note="": {
        "personalization_notes": "fake notes", "chosen_skeleton": "A - Experience First",
        "final_dm": "fake drafted dm text",
    })
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "fake-path.json")
    monkeypatch.setenv("SPREADSHEET_ID", "fake-sheet-id")
    dm_drafting.draft_all_pending(campaign, brand_name)
    return fake_sheet.worksheet("Shortlist")


def test_only_drafts_for_matching_campaign(monkeypatch):
    fake_sheet = FakeSheet([
        _row("instagram:duderobe_creator", "DudeRobe", "dm"),
        _row("instagram:sherobe_creator", "SheRobe", "dm"),
    ])
    ws = _run_draft(monkeypatch, fake_sheet, campaign="DudeRobe")
    rows = ws.get_all_records()
    drafted = {r["dedup_key"] for r in rows if r.get("dm_draft")}
    assert drafted == {"instagram:duderobe_creator"}


def test_only_drafts_for_dm_channel_not_email_or_none(monkeypatch):
    fake_sheet = FakeSheet([
        _row("instagram:dm_creator", "DudeRobe", "dm"),
        _row("instagram:email_creator", "DudeRobe", "email"),
        _row("instagram:none_creator", "DudeRobe", "none"),
        _row("instagram:blank_creator", "DudeRobe", ""),
    ])
    ws = _run_draft(monkeypatch, fake_sheet, campaign="DudeRobe")
    rows = ws.get_all_records()
    drafted = {r["dedup_key"] for r in rows if r.get("dm_draft")}
    assert drafted == {"instagram:dm_creator"}


def test_still_requires_fit_reasoning_and_skips_already_drafted(monkeypatch):
    fake_sheet = FakeSheet([
        _row("instagram:no_reasoning", "DudeRobe", "dm", fit_reasoning=""),
        _row("instagram:already_drafted", "DudeRobe", "dm", dm_draft="existing draft"),
    ])
    ws = _run_draft(monkeypatch, fake_sheet, campaign="DudeRobe")
    rows = {r["dedup_key"]: r for r in ws.get_all_records()}
    assert rows["instagram:no_reasoning"]["dm_draft"] == ""
    assert rows["instagram:already_drafted"]["dm_draft"] == "existing draft"  # untouched, not re-drafted
