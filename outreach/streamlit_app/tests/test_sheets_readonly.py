import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gspread
import pytest
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError


class FakeWorksheet:
    def __init__(self, records, header=None):
        self._records = records
        self._header = header or (list(records[0].keys()) if records else [])

    def get_all_records(self):
        return [dict(r) for r in self._records]

    def row_values(self, row_number):
        if row_number == 1:
            return list(self._header)
        raise NotImplementedError("Fake only supports reading the header row (row 1)")


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets  # {title: FakeWorksheet}

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]


def test_get_all_leads_adds_row_numbers_starting_at_2():
    ws = FakeWorksheet([{"Email": "a@abc.com"}, {"Email": "b@abc.com"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Master": ws}))

    leads = connector.get_all_leads("Master")
    assert leads[0]["_row"] == 2
    assert leads[1]["_row"] == 3
    assert leads[0]["Email"] == "a@abc.com"


def test_get_all_responses_passthrough():
    ws = FakeWorksheet([{"MessageID": "<m1>"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Responses": ws}))
    assert connector.get_all_responses("Responses") == [{"MessageID": "<m1>"}]


def test_get_all_send_log_passthrough():
    ws = FakeWorksheet([{"Status": "sent"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"SendLog": ws}))
    assert connector.get_all_send_log("SendLog") == [{"Status": "sent"}]


def test_get_all_error_log_passthrough():
    ws = FakeWorksheet([{"ErrorType": "Send Failure"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"ErrorLog": ws}))
    assert connector.get_all_error_log("ErrorLog") == [{"ErrorType": "Send Failure"}]


def test_missing_tab_raises_readonly_sheets_error_not_gspread_exception():
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({}))
    with pytest.raises(ReadOnlySheetsError, match="doesn't exist yet"):
        connector.get_all_leads("Nonexistent Tab")


def test_connector_requires_service_account_info_or_spreadsheet():
    with pytest.raises(ReadOnlySheetsError):
        ReadOnlySheetsConnector()


def test_connector_has_no_write_methods():
    # Explicit guard against accidental future write-method additions —
    # this connector must remain read-only by construction.
    write_like = {"update_lead_fields", "append_response", "append_send_log",
                  "append_error_log", "clear", "update", "batch_update",
                  "append_lead", "update_lead_statuses"}
    connector_methods = {m for m in dir(ReadOnlySheetsConnector) if not m.startswith("_")}
    assert connector_methods.isdisjoint(write_like)


def test_get_header_returns_header_row_explicitly():
    ws = FakeWorksheet([], header=["LeadID", "FirstName", "Email", "Title", "Website"])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Master": ws}))
    assert connector.get_header("Master") == ["LeadID", "FirstName", "Email", "Title", "Website"]


def test_get_header_works_even_with_zero_data_rows():
    # The exact case get_all_records() alone can't handle — a brand new
    # sheet with a header but no leads yet would lose custom-column
    # visibility if we only ever inferred columns from record dict keys.
    ws = FakeWorksheet([], header=["LeadID", "Email", "CustomField"])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Master": ws}))
    assert "CustomField" in connector.get_header("Master")


def test_get_header_missing_tab_raises_readonly_sheets_error():
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({}))
    with pytest.raises(ReadOnlySheetsError, match="doesn't exist yet"):
        connector.get_header("Nonexistent Tab")


def test_get_account_health_returns_records_when_tab_exists():
    ws = FakeWorksheet([{"AccountName": "sales1", "Address": "sales1@x.com", "Status": "Connected",
                          "Detail": "", "CheckedAt": "2026-08-29 09:00:00"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Email Accounts Health": ws}))
    records = connector.get_account_health()
    assert len(records) == 1
    assert records[0]["Status"] == "Connected"


def test_get_account_health_returns_empty_list_when_tab_missing():
    """Unlike every other tab, this one has no 'first run creates it'
    trigger from Streamlit's side — a fresh deployment before the
    periodic health-check workflow has ever run shouldn't show an error."""
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({}))
    assert connector.get_account_health() == []


def test_get_account_health_respects_custom_tab_name():
    ws = FakeWorksheet([{"AccountName": "sales1"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Custom Tab Name": ws}))
    records = connector.get_account_health(tab_name="Custom Tab Name")
    assert len(records) == 1
