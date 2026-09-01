"""Read-only counterpart to outreach.SheetsConnector.

Deliberately a SEPARATE, minimal class rather than reusing SheetsConnector:
- It authenticates with a Viewer-scoped service account, not the
  Editor-scoped one GitHub Actions uses to actually send email. If this
  dashboard's credential ever leaked, it could not write or delete anything.
- It never creates a worksheet (SheetsConnector._get_or_create_ws does, on
  purpose, for outreach.py's own runs — that's a write operation this
  module should never perform).

Everything else (column names, record shapes) intentionally matches
outreach.py's own get_all_leads/get_all_responses/etc. exactly, so the
dashboard math functions imported from outreach.py work unmodified.
"""
from typing import Dict, List

import gspread
from google.oauth2.service_account import Credentials

SCOPES_READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class ReadOnlySheetsError(Exception):
    pass


class ReadOnlySheetsConnector:
    def __init__(self, service_account_info: dict = None, sheet_id: str = None,
                 _spreadsheet=None):
        """Pass _spreadsheet directly (a gspread-like Spreadsheet object) in
        tests to skip real Google auth entirely."""
        if _spreadsheet is not None:
            self._spreadsheet = _spreadsheet
            return
        if not service_account_info or not sheet_id:
            raise ReadOnlySheetsError(
                "ReadOnlySheetsConnector needs service_account_info and sheet_id."
            )
        creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES_READONLY)
        client = gspread.authorize(creds)
        self._spreadsheet = client.open_by_key(sheet_id)

    def _ws(self, title: str):
        try:
            return self._spreadsheet.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            raise ReadOnlySheetsError(
                f"Tab '{title}' doesn't exist yet. It's created automatically the first "
                "time Preview, Send, or Check Replies actually runs for this campaign — "
                "run one of those first."
            )

    def get_all_leads(self, master_tab: str) -> List[Dict]:
        records = self._ws(master_tab).get_all_records()
        leads = []
        for i, record in enumerate(records, start=2):  # row 1 is header
            record["_row"] = i
            leads.append(record)
        return leads

    def get_all_responses(self, responses_tab: str) -> List[Dict]:
        return self._ws(responses_tab).get_all_records()

    def get_all_send_log(self, send_log_tab: str) -> List[Dict]:
        return self._ws(send_log_tab).get_all_records()

    def get_all_error_log(self, error_log_tab: str) -> List[Dict]:
        return self._ws(error_log_tab).get_all_records()

    def get_header(self, tab_name: str) -> List[str]:
        """The tab's actual header row — used to discover custom trailing
        columns (Title, Website, LinkedIn, ...) that exist in the real
        Sheet but aren't part of outreach.MASTER_COLUMNS, so the Data
        tab's column-mapping UI can offer them as valid targets without
        guessing."""
        return self._ws(tab_name).row_values(1)

    def get_account_health(self, tab_name: str = "Email Accounts Health") -> List[Dict]:
        """The shared (not per-campaign) account connectivity snapshot
        written by check_account_health.yml. Returns [] rather than
        raising if the tab doesn't exist yet — unlike every per-campaign
        tab, this one has no natural "first run creates it" trigger from
        the Streamlit side, so a brand new deployment shouldn't show an
        error here before the periodic workflow has ever run once."""
        try:
            return self._ws(tab_name).get_all_records()
        except ReadOnlySheetsError:
            return []
