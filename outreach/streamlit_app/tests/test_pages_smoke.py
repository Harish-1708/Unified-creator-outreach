"""These tests actually RUN the page scripts (via Streamlit's own
AppTest harness), not just their helper modules — catching import errors,
undefined names, and wrong Streamlit API usage that unit tests of
send_logic/campaign_builder/etc. can't see, since those never execute the
page files themselves. External calls (Google, GitHub) are mocked at the
boundary; nothing here touches a real network.
"""
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gspread
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

PAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "pages")


@pytest.fixture(autouse=True)
def _reset_streamlit_global_caches():
    """st.cache_resource/st.cache_data are backed by process-global storage
    that outlives any single AppTest instance — without clearing them, one
    test's cached connector (or a lock left behind by an interrupted cache
    population) can hang or contaminate the next test in this same process.
    Each page's cached functions are re-created fresh per test either way."""
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


class FakeWorksheet:
    def __init__(self, records, header=None):
        self._records = records
        self._header = header or (list(records[0].keys()) if records else [])
        self.read_call_count = 0

    def get_all_records(self):
        self.read_call_count += 1
        return [dict(r) for r in self._records]

    def row_values(self, row_number):
        if row_number == 1:
            self.read_call_count += 1
            return list(self._header)
        raise NotImplementedError("Fake only supports reading the header row (row 1)")


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]


def _dashboard_secrets():
    return {
        "shared_sheet_id": "fake-sheet-id",
        "google_sheets_readonly": {"service_account_json": {"type": "service_account"}},
        "github": {"token": "tok", "owner": "acme", "repo": "outreach"},
        "auth_users": {},
    }


def _authed_session():
    return {"auth_user": "alice"}


def test_dashboard_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "dashboard.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Dashboard page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Dashboard page showed an error: {[e.value for e in at.error]}"
    assert list(at.warning) == [], f"Dashboard page showed a warning: {[w.value for w in at.warning]}"
    assert len(at.selectbox) >= 1  # campaign selector rendered
    # Real assertion on computed content, not just "nothing crashed" — this
    # is what actually catches a wrong-key/wrong-tab-name bug, since a
    # broad except-Exception in the page would otherwise mask it as a
    # graceful st.error with no uncaught exception to catch.
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Leads == 1 from the fake Master Sheet row above


def test_new_campaign_dialog_disabled_until_confirmation_checked():
    """The confirm checkbox is the ONLY remaining safety net now that
    there's no GitHub trip — this must actually gate the button, not just
    be decorative."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        assert list(at.exception) == []
        create_button = next(b for b in at.button if b.label == "Create Campaign")
        assert create_button.disabled is True

        confirm_checkbox = at.checkbox[0]
        confirm_checkbox.set_value(True)
        at.run(timeout=15)
        create_button = next(b for b in at.button if b.label == "Create Campaign")
        assert create_button.disabled is False


def test_new_campaign_dialog_creates_campaign_and_stays_on_hub():
    """Deliberately does NOT auto-navigate into the new campaign — right
    after committing, Streamlit Cloud's local checkout is very likely
    still stale until it redeploys, so jumping straight to the detail
    view would hit a real 'No templates found' error. Staying on the hub
    with a clear message is the honest version of this UX."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["dispatched"] = workflow_file
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        text_inputs = {ti.label: ti for ti in at.text_input}
        text_inputs["Campaign name (letters, numbers, underscores only)"].set_value("BrandNewCampaign")
        at.checkbox[0].set_value(True)
        at.run(timeout=15)

        create_button = next(b for b in at.button if b.label == "Create Campaign")
        create_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Create campaign raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    assert captured["commits"][0]["path"] == "templates/BrandNewCampaign/intro_A.txt"
    # No template content was ever asked for — a placeholder is used instead.
    assert b"Write your subject here" in captured["commits"][0]["content"]
    assert captured["dispatched"] == "dashboard.yml"  # auto tab-init was triggered
    assert "selected_campaign" not in at.session_state  # stayed on the hub, didn't auto-navigate
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles  # still the hub, not a campaign detail page


def test_new_campaign_dialog_only_asks_for_name_no_template_fields():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    dialog_text_inputs = [ti.label for ti in at.text_input if "Campaign name" in ti.label]
    assert len(dialog_text_inputs) == 1
    # No Subject/Body fields anywhere in the dialog.
    assert not any("Subject" in ti.label for ti in at.text_input if ti.label)
    assert len(at.text_area) == 0


def test_new_campaign_dialog_does_not_reopen_after_navigating_away_and_back():
    """The actual reported bug: opening the dialog, then visiting a
    different page, then returning to Campaigns without ever clicking
    Create or Cancel, was silently reopening the dialog again — because
    the session_state flag needed to survive reruns FROM WITHIN the
    dialog had no way to distinguish that from a genuine return visit."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)
        assert at.session_state["show_new_campaign_dialog"] is True

        # Simulate visiting a different page — exactly what dashboard.py /
        # controls.py / etc. do via mark_active_page at their own top.
        at.session_state["_active_page"] = "dashboard"

        # Return to Campaigns — WITHOUT ever clicking Create or Cancel.
        at.run(timeout=15)

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is False
    # The dialog's own fields shouldn't be showing anymore either.
    assert not any("Campaign name" in ti.label for ti in at.text_input if ti.label)


def test_new_campaign_dialog_stays_open_across_its_own_widget_interactions():
    """The flip side of the above — interacting with a widget INSIDE the
    dialog (not navigating away) must NOT close it. This is what the
    session_state approach was originally introduced to fix; confirming
    it still holds after adding the navigation check."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        name_input = next(ti for ti in at.text_input if "Campaign name" in ti.label)
        name_input.set_value("SomeName")
        at.run(timeout=15)  # a rerun triggered by a widget INSIDE the dialog

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is True  # still open
    assert any("Campaign name" in ti.label for ti in at.text_input if ti.label)


def test_new_campaign_dialog_cancel_button_closes_it():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        cancel_button = next(b for b in at.button if b.label == "Cancel")
        cancel_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is False


def test_new_campaign_dialog_rejects_duplicate_name():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        text_inputs = {ti.label: ti for ti in at.text_input}
        text_inputs["Campaign name (letters, numbers, underscores only)"].set_value("Kelson_Creators_Licensing")
        at.checkbox[0].set_value(True)
        at.run(timeout=15)

        create_button = next(b for b in at.button if b.label == "Create Campaign")
        create_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    error_texts = " ".join(e.value for e in at.error)
    assert "already exists" in error_texts


def test_overview_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00"},
             {"Email": "b@abc.com", "Approval": "Yes", "IntroSentAt": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "overview.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Overview page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Overview page showed an error: {[e.value for e in at.error]}"
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Sent from the fake Send Log row
    assert "1" in metric_values  # Total Pending: 1 lead sent, 1 not yet contacted


def test_email_accounts_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    secrets = _dashboard_secrets()
    secrets["email_accounts_directory"] = {"sales1": "sales1@example.com", "sales2": "sales2@example.com"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Email Accounts page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Email Accounts page showed an error: {[e.value for e in at.error]}"
    assert len(at.dataframe) >= 1
    df = at.dataframe[0].value
    account_col = " ".join(str(v) for v in df["Account"])
    assert "sales1" in account_col
    assert "sales2" in account_col


def test_email_accounts_page_shows_info_when_no_accounts_configured_at_all():
    """No longer a warning — now that Add Account exists, having zero
    accounts is just a starting state, not something wrong."""
    at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
    at.secrets.update(_dashboard_secrets())  # no email_accounts_directory key, no slot mapping file
    for k, v in _authed_session().items():
        at.session_state[k] = v
    at.run()

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "No accounts configured yet" in info_texts
    assert any(b.label == "➕ Add Account" for b in at.button)


def test_email_accounts_page_shows_connection_status_when_health_data_exists():
    fake_ws = {
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Email Accounts Health": FakeWorksheet([
            {"AccountName": "sales1", "Address": "sales1@example.com", "Status": "Connected",
             "Detail": "", "CheckedAt": "2026-08-29 12:00:00"},
            {"AccountName": "sales2", "Address": "sales2@example.com", "Status": "Disconnected",
             "Detail": "AUTHENTICATIONFAILED", "CheckedAt": "2026-08-29 12:00:00"},
        ]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    secrets = _dashboard_secrets()
    secrets["email_accounts_directory"] = {"sales1": "sales1@example.com", "sales2": "sales2@example.com"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Email Accounts page raised: {list(at.exception)}"
    assert list(at.error) == []
    df = at.dataframe[0].value
    status_col = " ".join(str(v) for v in df["Status"])
    assert "🟢 Connected" in status_col
    assert "🔴 Disconnected" in status_col
    detail_col = " ".join(str(v) for v in df["Detail"])
    assert "AUTHENTICATIONFAILED" in detail_col  # the disconnection reason shown, plainly


def test_email_accounts_page_shows_unknown_status_when_no_health_tab_yet():
    """A brand new deployment, before check_account_health.yml has ever
    run — must show 'Unknown', not an error, and explain why."""
    fake_ws = {"Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([])}  # no health tab at all
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    secrets = _dashboard_secrets()
    secrets["email_accounts_directory"] = {"sales1": "sales1@example.com"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == []
    assert list(at.error) == []
    df = at.dataframe[0].value
    status_col = " ".join(str(v) for v in df["Status"])
    assert "⚪ Unknown" in status_col
    caption_texts = " ".join(c.value for c in at.caption)
    assert "runs automatically every 2 hours" in caption_texts


def test_email_accounts_page_refresh_button_busts_caches_and_refetches():
    fake_ws = {
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Email Accounts Health": FakeWorksheet([
            {"AccountName": "sales1", "Address": "sales1@x.com", "Status": "Connected",
             "Detail": "", "CheckedAt": "2026-08-29 12:00:00"},
        ]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    secrets = _dashboard_secrets()
    secrets["email_accounts_directory"] = {"sales1": "sales1@x.com"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        assert list(at.exception) == []
        refresh_button = next(b for b in at.button if "Refresh" in b.label)
        refresh_button.click()
        at.run()

    assert list(at.exception) == [], f"Refresh raised: {list(at.exception)}"
    assert list(at.error) == []


    assert list(at.exception) == []


def _mock_secret_writes():
    """Returns (captured, fake_set_secret, fake_delete_secret) — same
    class-level-patch pattern as _mock_github_writes, extended for the
    two secret-specific methods."""
    captured = {"set_secret_calls": [], "delete_secret_calls": []}

    def fake_set_secret(self, secret_name, plaintext_value):
        captured["set_secret_calls"].append({"name": secret_name, "value": plaintext_value})

    def fake_delete_secret(self, secret_name):
        captured["delete_secret_calls"].append(secret_name)

    return captured, fake_set_secret, fake_delete_secret


def test_add_account_dialog_disabled_until_confirmed(tmp_path):
    (tmp_path / "config").mkdir()
    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        add_button = next(b for b in at.button if b.label == "➕ Add Account")
        add_button.click()
        at.run()

        assert list(at.exception) == []
        submit_button = next(b for b in at.button if b.label == "Add Account")
        assert submit_button.disabled is True

        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "add_account_confirm")
        confirm_checkbox.set_value(True)
        at.run()
        submit_button = next(b for b in at.button if b.label == "Add Account")
        assert submit_button.disabled is False


def test_add_account_happy_path_writes_secret_and_mapping_and_triggers_health_check(tmp_path):
    (tmp_path / "config").mkdir()
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()
    dispatch_captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        dispatch_captured["workflow"] = workflow_file
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        add_button = next(b for b in at.button if b.label == "➕ Add Account")
        add_button.click()
        at.run()

        name_input = next(ti for ti in at.text_input if ti.key == "add_account_name")
        name_input.set_value("sales1")
        address_input = next(ti for ti in at.text_input if ti.key == "add_account_address")
        address_input.set_value("sales1@gmail.com")
        password_input = next(ti for ti in at.text_input if ti.key == "add_account_password")
        password_input.set_value("aaaabbbbccccdddd")
        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "add_account_confirm")
        confirm_checkbox.set_value(True)
        at.run()

        submit_button = next(b for b in at.button if b.label == "Add Account")
        submit_button.click()
        at.run()

    assert list(at.exception) == [], f"Add Account raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["set_secret_calls"]) == 1
    assert captured["set_secret_calls"][0]["name"] == "EMAIL_ACCOUNT_SLOT_1"
    payload = json.loads(captured["set_secret_calls"][0]["value"])
    assert payload == {"name": "sales1", "address": "sales1@gmail.com", "app_password": "aaaabbbbccccdddd"}
    assert dispatch_captured["workflow"] == "check_account_health.yml"

    import yaml as _yaml
    mapping_commit = commits_captured["commits"][0]
    assert mapping_commit["path"] == "config/email_account_slots.yaml"
    written_mapping = _yaml.safe_load(mapping_commit["content"].decode("utf-8"))
    assert written_mapping == {"sales1": {"slot": 1, "address": "sales1@gmail.com"}}


def test_add_account_custom_provider_fields_reach_the_secret(tmp_path):
    (tmp_path / "config").mkdir()
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", lambda self, w, i: None):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        add_button = next(b for b in at.button if b.label == "➕ Add Account")
        add_button.click()
        at.run()

        provider_radio = next(r for r in at.radio if r.key == "add_account_provider")
        provider_radio.set_value("Custom (Hostinger, etc.)")
        at.run()

        next(ti for ti in at.text_input if ti.key == "add_account_name").set_value("hostinger1")
        next(ti for ti in at.text_input if ti.key == "add_account_address").set_value("sales@example.com")
        next(ti for ti in at.text_input if ti.key == "add_account_password").set_value("smtp-pass-here")
        next(ti for ti in at.text_input if ti.key == "add_account_smtp_host").set_value("smtp.hostinger.com")
        next(ti for ti in at.text_input if ti.key == "add_account_smtp_port").set_value("465")
        next(ti for ti in at.text_input if ti.key == "add_account_imap_host").set_value("imap.hostinger.com")
        next(ti for ti in at.text_input if ti.key == "add_account_imap_port").set_value("993")
        next(cb for cb in at.checkbox if cb.key == "add_account_confirm").set_value(True)
        at.run()

        submit_button = next(b for b in at.button if b.label == "Add Account")
        submit_button.click()
        at.run()

    assert list(at.exception) == [], f"Add Account (custom provider) raised: {list(at.exception)}"
    assert list(at.error) == []
    payload = json.loads(captured["set_secret_calls"][0]["value"])
    assert payload["smtp_host"] == "smtp.hostinger.com"
    assert payload["smtp_port"] == 465
    assert payload["imap_host"] == "imap.hostinger.com"
    assert payload["imap_port"] == 993


def test_bulk_add_accounts_csv_adds_every_valid_row(tmp_path):
    (tmp_path / "config").mkdir()
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()

    csv_content = (
        "Name,Email,Password\n"
        "sales1,sales1@gmail.com,pass1\n"
        "sales2,sales2@gmail.com,pass2\n"
        "sales3,sales3@gmail.com,pass3\n"
    ).encode("utf-8")

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", lambda self, w, i: None):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        uploader = next(fu for fu in at.file_uploader if fu.key == "bulk_accounts_csv_upload")
        uploader.upload("accounts.csv", csv_content, "text/csv")
        at.run()

        assert list(at.exception) == []
        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "confirm_bulk_add_accounts")
        confirm_checkbox.set_value(True)
        at.run()

        add_all_button = next(b for b in at.button if b.key == "bulk_add_accounts_button")
        add_all_button.click()
        at.run()

    assert list(at.exception) == [], f"Bulk add raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["set_secret_calls"]) == 3
    assert {c["name"] for c in captured["set_secret_calls"]} == {
        "EMAIL_ACCOUNT_SLOT_1", "EMAIL_ACCOUNT_SLOT_2", "EMAIL_ACCOUNT_SLOT_3",
    }
    # exactly ONE mapping-file commit for the whole batch, not one per account
    mapping_commits = [c for c in commits_captured["commits"] if c["path"] == "config/email_account_slots.yaml"]
    assert len(mapping_commits) == 1

    import yaml as _yaml
    written_mapping = _yaml.safe_load(mapping_commits[0]["content"].decode("utf-8"))
    assert set(written_mapping.keys()) == {"sales1", "sales2", "sales3"}


def test_bulk_add_accounts_csv_reports_invalid_rows_without_blocking_valid_ones(tmp_path):
    (tmp_path / "config").mkdir()
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()

    csv_content = (
        "Name,Email,Password\n"
        "sales1,sales1@gmail.com,pass1\n"
        "bad_row,,pass2\n"
    ).encode("utf-8")

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", lambda self, w, i: None):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        uploader = next(fu for fu in at.file_uploader if fu.key == "bulk_accounts_csv_upload")
        uploader.upload("accounts.csv", csv_content, "text/csv")
        at.run()

    assert list(at.exception) == []
    warning_texts = " ".join(w.value for w in at.warning)
    assert "Row 2" in warning_texts
    assert "Email is required" in warning_texts
    success_texts = " ".join(s.value for s in at.success)
    assert "1 account(s) ready" in success_texts


def test_add_account_rejects_blank_fields_without_writing_anything(tmp_path):
    (tmp_path / "config").mkdir()
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        add_button = next(b for b in at.button if b.label == "➕ Add Account")
        add_button.click()
        at.run()

        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "add_account_confirm")
        confirm_checkbox.set_value(True)
        at.run()

        submit_button = next(b for b in at.button if b.label == "Add Account")
        submit_button.click()
        at.run()

    assert list(at.exception) == []
    assert captured["set_secret_calls"] == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Account name is required" in error_texts


def test_add_account_dialog_does_not_reopen_after_navigating_away(tmp_path):
    (tmp_path / "config").mkdir()
    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        add_button = next(b for b in at.button if b.label == "➕ Add Account")
        add_button.click()
        at.run()
        assert at.session_state["show_add_account_dialog"] is True

        at.session_state["_active_page"] = "dashboard"  # simulate visiting another page
        at.run()  # return, without clicking Add Account or Cancel

    assert list(at.exception) == []
    assert at.session_state["show_add_account_dialog"] is False


def _write_slot_mapping_fixture(tmp_path, mapping_yaml):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "email_account_slots.yaml").write_text(mapping_yaml)


def test_manage_section_shows_only_accounts_tracked_in_slot_mapping(tmp_path):
    _write_slot_mapping_fixture(tmp_path, "sales1:\n  slot: 1\n  address: sales1@gmail.com\n")

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet({})})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"legacy_only": "legacy@gmail.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == []
    manage_selector = next(sb for sb in at.selectbox if sb.key == "manage_account_select")
    assert "sales1" in manage_selector.options
    assert "legacy_only" not in manage_selector.options  # not manageable via this app


def test_manage_section_edit_address_commits_updated_mapping_no_secret_write(tmp_path):
    _write_slot_mapping_fixture(tmp_path, "sales1:\n  slot: 1\n  address: old@gmail.com\n")
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet({})})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        address_input = next(ti for ti in at.text_input if ti.key == "manage_address_sales1")
        address_input.set_value("new@gmail.com")
        at.run()

        save_button = next(b for b in at.button if b.key == "manage_save_sales1")
        save_button.click()
        at.run()

    assert list(at.exception) == [], f"Edit raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["set_secret_calls"] == []  # no password change, no secret write

    import yaml as _yaml
    mapping_commit = commits_captured["commits"][0]
    written_mapping = _yaml.safe_load(mapping_commit["content"].decode("utf-8"))
    assert written_mapping["sales1"]["address"] == "new@gmail.com"
    assert written_mapping["sales1"]["slot"] == 1  # slot never changes on an edit


def test_manage_section_edit_with_new_password_writes_secret(tmp_path):
    _write_slot_mapping_fixture(tmp_path, "sales1:\n  slot: 1\n  address: sales1@gmail.com\n")
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.set_secret", fake_set_secret), \
         patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet({})})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        password_input = next(ti for ti in at.text_input if ti.key == "manage_password_sales1")
        password_input.set_value("newpassword1234")
        at.run()

        save_button = next(b for b in at.button if b.key == "manage_save_sales1")
        save_button.click()
        at.run()

    assert list(at.exception) == []
    assert list(at.error) == []
    assert len(captured["set_secret_calls"]) == 1
    payload = json.loads(captured["set_secret_calls"][0]["value"])
    assert payload["app_password"] == "newpassword1234"


def test_manage_section_remove_requires_confirmation_checkbox(tmp_path):
    _write_slot_mapping_fixture(tmp_path, "sales1:\n  slot: 1\n  address: sales1@gmail.com\n")
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.delete_secret", fake_delete_secret), \
         patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet({})})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        remove_button = next(b for b in at.button if b.key == "manage_remove_sales1")
        assert remove_button.disabled is True  # confirm checkbox not checked yet


def test_manage_section_remove_deletes_secret_and_updates_mapping(tmp_path):
    _write_slot_mapping_fixture(tmp_path, "sales1:\n  slot: 1\n  address: sales1@gmail.com\n")
    captured, fake_set_secret, fake_delete_secret = _mock_secret_writes()
    commits_captured, fake_create_file = _mock_github_writes()

    with patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH", str(tmp_path / "config" / "email_account_slots.yaml")), \
         patch("github_client.GitHubClient.delete_secret", fake_delete_secret), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: FakeSpreadsheet({})})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "manage_confirm_remove_sales1")
        confirm_checkbox.set_value(True)
        at.run()

        remove_button = next(b for b in at.button if b.key == "manage_remove_sales1")
        remove_button.click()
        at.run()

    assert list(at.exception) == [], f"Remove raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["delete_secret_calls"] == ["EMAIL_ACCOUNT_SLOT_1"]

    import yaml as _yaml
    mapping_commit = commits_captured["commits"][0]
    written_mapping = _yaml.safe_load(mapping_commit["content"].decode("utf-8"))
    assert "sales1" not in written_mapping


def _campaigns_page_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00",
              "IntroVariant": "A", "SenderAccount": "sales1",
              "FollowUp1SentAt": "", "FollowUp1Variant": "", "Status": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_campaigns_hub_page_renders_without_exceptions():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

    assert list(at.exception) == [], f"Campaigns hub raised: {list(at.exception)}"
    assert list(at.error) == [], f"Campaigns hub showed an error: {[e.value for e in at.error]}"
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Kelson_Creators_Licensing" in markdown_text


def test_campaigns_hub_refresh_button_clears_caches_without_error():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        refresh_button = next(b for b in at.button if b.key == "refresh_hub_button")
        refresh_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Refresh raised: {list(at.exception)}"
    assert list(at.error) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Kelson_Creators_Licensing" in markdown_text  # data still shows after the cache clear + rerun



    """Directly sets selected_campaign in session_state, bypassing the
    click — proves the detail view + Analytics tab (Phase B, real data,
    not a stub) work end to end against realistic Sheet data."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Campaign detail raised: {list(at.exception)}"
    assert list(at.error) == [], f"Campaign detail showed an error: {[e.value for e in at.error]}"
    titles = [t.value for t in at.title]
    assert "Kelson_Creators_Licensing" in titles
    # 6 outer tabs (Analytics/Data/Sequences/Schedule/Settings/Responses) —
    # Preview removed (redundant with Sequences' own template preview),
    # Send lives in Settings, Check Replies in Responses, Maintenance in
    # Sequences.
    assert len(at.tabs) == 6
    # Real analytics data should show up as metrics, not just tab labels.
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Leads == 1


def test_campaigns_detail_view_has_no_remaining_stub_tabs():
    """All six tabs are real now (A through H complete) — this replaces
    the earlier per-phase 'is this tab honestly still a stub' check,
    which no longer applies once nothing is a stub."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "isn't built yet" not in info_texts


def test_campaigns_back_button_clears_selected_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        back_button = next(b for b in at.button if "Back to Campaigns" in b.label)
        back_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert "selected_campaign" not in at.session_state
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles


def test_campaign_detail_reruns_do_not_re_fetch_sheets_data():
    """The actual regression: Streamlit reruns the WHOLE script on nearly
    every widget interaction. Before this was cached, each rerun on the
    campaign detail page re-issued 4 fresh Sheets reads — easily enough
    to exceed Google's 60-reads/minute quota during ordinary use (e.g.
    adjusting several CSV mapping dropdowns in a row) and return a 429.
    This proves a second rerun reuses the cache instead of re-fetching."""
    fake_ws = _campaigns_page_fake_ws()
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    master_ws = fake_ws["Kelson_Creators_Licensing Master Sheet"]

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        assert list(at.exception) == []
        reads_after_first_run = master_ws.read_call_count
        assert reads_after_first_run > 0  # sanity — it did fetch at least once

        # Simulate a widget interaction elsewhere on the page (a full
        # script rerun, exactly like adjusting a filter or mapping
        # dropdown would trigger) — this must NOT trigger a second fetch.
        status_filter = next(sb for sb in at.selectbox if sb.label == "Filter")
        status_filter.set_value("Removed")
        at.run(timeout=15)

    assert list(at.exception) == []
    assert master_ws.read_call_count == reads_after_first_run, (
        f"Expected no new Sheets reads on rerun (cached), but count went from "
        f"{reads_after_first_run} to {master_ws.read_call_count}"
    )


def test_refresh_data_button_actually_busts_the_cache():
    fake_ws = _campaigns_page_fake_ws()
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    master_ws = fake_ws["Kelson_Creators_Licensing Master Sheet"]

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)
        reads_after_first_run = master_ws.read_call_count

        refresh_button = next(b for b in at.button if "Refresh data" in b.label)
        refresh_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert master_ws.read_call_count > reads_after_first_run  # the explicit refresh DID re-fetch


def _empty_master_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [], header=["LeadID", "FirstName", "LastName", "Email", "Company", "Campaign", "Approval",
                        "SenderAccount", "RequestedAction", "CurrentStage", "ScheduledAt", "IntroSentAt",
                        "IntroVariant", "Status", "LastActionAt"]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_data_tab_upload_shows_mapping_ui_with_correct_defaults():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Email\nSam,sam@abc.com\nAlex,alex@abc.com\n", "text/csv")
        at.run(timeout=15)

    assert list(at.exception) == [], f"Data tab upload raised: {list(at.exception)}"
    assert list(at.error) == []
    mapping = {sb.label: sb.value for sb in at.selectbox if "maps to" in sb.label}
    assert mapping["'First Name' maps to"] == "FirstName"
    assert mapping["'Email' maps to"] == "Email"
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "2 of 2 row" in markdown_text


def test_data_tab_import_commits_payload_and_triggers_workflow():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured["path"] = path
        captured["content"] = content_bytes

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Email\nSam,sam@abc.com\n", "text/csv")
        at.run(timeout=15)
        import_button = next(b for b in at.button if b.label == "Import Leads")
        import_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Import click raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "import_leads.yml"
    assert captured["inputs"]["campaign"] == "Kelson_Creators_Licensing"
    assert captured["path"].startswith("imports/Kelson_Creators_Licensing/")
    import json
    payload = json.loads(captured["content"].decode("utf-8"))
    assert payload == {"leads": [{"FirstName": "Sam", "Email": "sam@abc.com"}]}


def test_data_tab_shows_error_when_no_column_mapped_to_email():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Nickname\nSam,Sammy\n", "text/csv")
        at.run(timeout=15)
        # Neither column auto-maps to Email — force it to stay unmapped.
        email_selectbox = next(sb for sb in at.selectbox if "'Nickname' maps to" in sb.label)
        email_selectbox.set_value("-- Skip --")
        at.run(timeout=15)

    assert list(at.exception) == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Email" in error_texts


def test_data_tab_lead_table_and_remove_flow():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"LeadID": "1", "FirstName": "Sam", "LastName": "Lee", "Email": "sam@abc.com",
              "Company": "Acme", "Approval": "Yes", "Status": ""},
             {"LeadID": "2", "FirstName": "Alex", "LastName": "Kim", "Email": "alex@abc.com",
              "Company": "Beta", "Approval": "Yes", "Status": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured["path"] = path
        captured["content"] = content_bytes

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        assert list(at.exception) == []
        remove_select = next(ms for ms in at.multiselect if "Select leads to remove" in ms.label)
        matching_option = next(opt for opt in remove_select.options if "1 —" in opt)
        remove_select.set_value([matching_option])
        at.run(timeout=15)

        remove_button = next(b for b in at.button if b.label == "Remove Selected")
        remove_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Remove flow raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "remove_leads.yml"
    import json
    payload = json.loads(captured["content"].decode("utf-8"))
    assert payload == {"lead_ids": ["1"]}


def _mock_github_writes():
    """Returns (patchers, captured) — patches create_file/dispatch_workflow
    at the GitHubClient class level, same pattern as the Data tab tests."""
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured.setdefault("commits", []).append({"path": path, "content": content_bytes, "message": message})

    return captured, fake_create_file


def test_sequences_tab_shows_locked_variants_for_real_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Sequences tab raised: {list(at.exception)}"
    assert list(at.error) == []
    # Kelson_Creators_Licensing has 5 stages x 4 variants = 20 "Variant X" expanders
    variant_expanders = [e for e in at.expander if e.label.startswith("Variant ")]
    assert len(variant_expanders) == 20
    # Every text input/area for template content starts disabled (locked).
    subject_inputs = [ti for ti in at.text_input if ti.label.startswith("Subject")]
    assert all(ti.disabled for ti in subject_inputs)


def test_sequences_tab_intro_subject_label_never_says_continues_thread():
    """Regression: the blank-continues-the-thread hint was showing on
    EVERY stage's Subject field, including Intro — self-contradictory,
    since Intro can never actually use a blank subject (see
    outreach.render_email's is_first_stage guard)."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    intro_subject_inputs = [ti for ti in at.text_input if ti.key and ti.key.startswith("subject_intro_")]
    followup_subject_inputs = [ti for ti in at.text_input if ti.key and ti.key.startswith("subject_followup")]
    assert intro_subject_inputs, "Expected at least one Intro subject field"
    assert all("continues" not in ti.label.lower() for ti in intro_subject_inputs)
    assert all("required" in ti.label.lower() for ti in intro_subject_inputs)
    assert all("continues" in ti.label.lower() for ti in followup_subject_inputs)


def test_sequences_tab_save_rejects_blank_subject_for_intro_edit():
    """Editing Intro's subject down to blank must be caught here, before
    Save — not left to fail later, at send time, with a TemplateError."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        unlock_checkbox = next(cb for cb in at.checkbox if cb.key == "unlock_intro_A")
        unlock_checkbox.set_value(True)
        at.run(timeout=15)

        subject_input = next(ti for ti in at.text_input if ti.key == "subject_intro_A")
        subject_input.set_value("")
        at.run(timeout=15)

    assert list(at.exception) == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Subject is required" in error_texts
    assert captured.get("commits") is None


def test_sequences_tab_unlock_and_save_edits_one_variant():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        unlock_checkbox = next(cb for cb in at.checkbox if cb.key == "unlock_intro_A")
        unlock_checkbox.set_value(True)
        at.run(timeout=15)

        subject_input = next(ti for ti in at.text_input if ti.key == "subject_intro_A")
        subject_input.set_value("A brand new intro subject")
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label.startswith("💾 Save Changes"))
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Save edit raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    commit = captured["commits"][0]
    assert commit["path"] == "templates/Kelson_Creators_Licensing/intro_A.txt"
    assert b"A brand new intro subject" in commit["content"]


def test_sequences_tab_locked_variant_edit_is_not_saved():
    """Typing into a field while still locked must never reach Save —
    the disabled widget shouldn't even accept the value, but this proves
    the end-to-end behavior regardless of how disabling is implemented."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    # No "Save Changes" button should even appear — nothing is unlocked, so
    # nothing can be pending.
    save_buttons = [b for b in at.button if b.label.startswith("💾 Save Changes")]
    assert save_buttons == []


def test_sequences_tab_add_variant_maxed_out_for_real_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "maximum" in info_texts.lower()
    assert "already has all 5 stages" in info_texts


def test_sequences_tab_add_variant_validates_across_all_stages(tmp_path):
    """Uses a synthetic partial campaign (2 stages, 1 variant) so "Add
    Variant B" is actually available to test, unlike the real fixture
    which is already fully built out."""
    campaign_dir = tmp_path / "PartialSeqCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Intro A\n\nBody A")
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nFollowup body A")

    fake_ws = {
        "PartialSeqCampaign Master Sheet": FakeWorksheet([], header=["LeadID", "Email", "Approval"]),
        "PartialSeqCampaign Response Sheet": FakeWorksheet([]),
        "PartialSeqCampaign Custom Log Sheet": FakeWorksheet([]),
        "PartialSeqCampaign Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.TEMPLATES_ROOT", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "PartialSeqCampaign"
        at.run(timeout=15)

        add_button = next((b for b in at.button if b.label == "Add Variant B"), None)
        assert add_button is not None, "Expected an 'Add Variant B' button for a 1-variant campaign"
        add_button.click()  # click with everything still blank — should show validation errors, not commit
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None or captured["commits"] == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Subject is required" in error_texts or "Body is required" in error_texts


def test_settings_tab_renders_current_values_from_config():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Settings tab raised: {list(at.exception)}"
    assert list(at.error) == []
    account_selector = next(ms for ms in at.multiselect if "sender accounts" in ms.label)
    assert set(account_selector.options) == {"sales1", "sales2"}
    daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
    assert daily_limit_input.value == 100  # matches config/settings.yaml's real default


def test_settings_tab_shows_info_when_no_accounts_directory_configured():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())  # no email_accounts_directory
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "No accounts configured yet" in info_texts


def test_settings_tab_save_writes_yaml_with_new_values_and_correct_path():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.get_file_sha", return_value=None):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
        daily_limit_input.set_value(250)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Settings save raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    commit = captured["commits"][0]
    assert commit["path"] == "config/campaigns/Kelson_Creators_Licensing.yaml"
    import yaml
    written = yaml.safe_load(commit["content"].decode("utf-8"))
    assert written["sending"]["daily_limit"] == 250


def test_settings_tab_save_rejects_non_positive_daily_limit_without_committing():
    """The widget itself enforces min_value=1, so the only way to reach
    validate_settings' rejection path through the UI is the per-account
    limit toggle — tested directly and thoroughly in test_settings_logic.py
    instead, where it doesn't depend on simulating a specific widget's
    numeric-input quirks. This smoke test instead confirms the more
    load-bearing thing: Save actually works end-to-end on first use with
    the real config defaults, with no error."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert len(captured.get("commits", [])) == 1


def test_settings_tab_save_preserves_existing_status_and_schedule_keys(tmp_path):
    """The real regression this guards: Save must only ever touch the
    'sending' key of the override file. Anything else already there
    (status from Pause/Resume, schedule once that phase exists) must
    survive a Settings save untouched."""
    (tmp_path / "Kelson_Creators_Licensing.yaml").write_text(
        "status: paused\nschedule:\n  timezone: America/Los_Angeles\nsending:\n  daily_limit: 50\n"
    )
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.CAMPAIGNS_DIR", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
        daily_limit_input.set_value(300)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written["status"] == "paused"  # preserved, not clobbered
    assert written["schedule"] == {"timezone": "America/Los_Angeles"}  # preserved
    assert written["sending"]["daily_limit"] == 300  # actually updated


def test_settings_tab_select_all_accounts_actually_selects_and_persists_through_save():
    """Regression: clicking 'Select all accounts' visually appeared to
    work but didn't actually change what got saved — the button was
    reassigning a local Python variable, not the multiselect widget's own
    state, so a later Save click re-read the widget fresh and silently
    discarded the selection."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        select_all_button = next(b for b in at.button if b.label == "Select all accounts")
        select_all_button.click()
        at.run(timeout=15)

        account_selector = next(ms for ms in at.multiselect if "sender accounts" in ms.label)
        assert set(account_selector.value) == {"sales1", "sales2"}  # widget itself actually shows both selected

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert set(written["sending"]["rotation_accounts"]) == {"sales1", "sales2"}  # actually persisted


def test_settings_tab_sender_picker_shows_accounts_from_slot_mapping_too():
    """The real bug this fixes: an account added via the Email Accounts
    page's Add Account button lives only in the slot-mapping file, never
    in the [email_accounts_directory] Streamlit secret — the sender
    picker here must show it too, not just accounts manually added to
    Streamlit Secrets."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with tempfile.TemporaryDirectory() as tmp_dir:
        config_dir = os.path.join(tmp_dir, "config")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "email_account_slots.yaml"), "w") as f:
            f.write("sales2:\n  slot: 1\n  address: sales2@gmail.com\n")

        with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
             patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
             patch("config.EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH",
                   os.path.join(config_dir, "email_account_slots.yaml")):
            at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
            secrets = _dashboard_secrets()
            secrets["email_accounts_directory"] = {"sales1": "sales1@x.com"}  # only the OLD account here
            at.secrets.update(secrets)
            for k, v in _authed_session().items():
                at.session_state[k] = v
            at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
            at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    account_selector = next(ms for ms in at.multiselect if "sender accounts" in ms.label)
    assert set(account_selector.options) == {"sales1", "sales2"}  # both sources present


def test_delete_campaign_requires_typed_name_confirmation():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    delete_button = next(b for b in at.button if b.key == "delete_campaign_button")
    assert delete_button.disabled is True  # nothing typed yet


def test_delete_campaign_enables_button_only_on_exact_name_match():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        confirm_input = next(ti for ti in at.text_input if ti.key == "delete_campaign_confirm_text")
        confirm_input.set_value("wrong name")
        at.run(timeout=15)
        assert next(b for b in at.button if b.key == "delete_campaign_button").disabled is True

        confirm_input = next(ti for ti in at.text_input if ti.key == "delete_campaign_confirm_text")
        confirm_input.set_value("Kelson_Creators_Licensing")
        at.run(timeout=15)

    assert list(at.exception) == []
    assert next(b for b in at.button if b.key == "delete_campaign_button").disabled is False


def test_delete_campaign_deletes_every_template_file(tmp_path):
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    deleted_paths = []

    def fake_delete_file(self, path, message, branch="main"):
        deleted_paths.append(path)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.delete_file", fake_delete_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        confirm_input = next(ti for ti in at.text_input if ti.key == "delete_campaign_confirm_text")
        confirm_input.set_value("Kelson_Creators_Licensing")
        at.run(timeout=15)

        delete_button = next(b for b in at.button if b.key == "delete_campaign_button")
        delete_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert len(deleted_paths) >= 1
    assert all(p.startswith("templates/Kelson_Creators_Licensing/") for p in deleted_paths)


def test_temporarily_remove_campaign_sets_deleted_status_without_deleting_files():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    commits_captured, fake_create_file = _mock_github_writes()
    deleted_paths = []

    def fake_delete_file(self, path, message, branch="main"):
        deleted_paths.append(path)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.delete_file", fake_delete_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        temp_remove_button = next(b for b in at.button if b.key == "temp_remove_campaign_button")
        temp_remove_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Temporary remove raised: {list(at.exception)}"
    assert list(at.error) == []
    assert deleted_paths == []  # no template files touched — this is a config-only change

    import yaml as _yaml
    override_commit = next(c for c in commits_captured["commits"] if c["path"].endswith(".yaml"))
    written = _yaml.safe_load(override_commit["content"].decode("utf-8"))
    assert written["status"] == "deleted"
    assert written["previous_status"] == "active"  # this fixture campaign has no explicit status -> "active"
    assert at.session_state["selected_campaign"] is None  # navigates back to the hub


def test_restore_campaign_brings_back_exact_previous_status_not_draft(tmp_path):
    """The actual bug being fixed: a campaign that was Paused (not just
    Draft) before Temporarily Remove must come back Paused when
    restored, not silently reset to Draft."""
    (tmp_path / "Kelson_Creators_Licensing.yaml").write_text(
        "status: deleted\nprevious_status: paused\n"
    )
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    commits_captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.CAMPAIGNS_DIR", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = None
        at.run(timeout=15)

        restore_button = next(b for b in at.button if b.key == "restore_Kelson_Creators_Licensing")
        restore_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Restore raised: {list(at.exception)}"
    assert list(at.error) == []

    import yaml as _yaml
    override_commit = next(c for c in commits_captured["commits"] if c["path"].endswith(".yaml"))
    written = _yaml.safe_load(override_commit["content"].decode("utf-8"))
    assert written["status"] == "paused"  # exact prior status, not "draft"
    assert "previous_status" not in written


def test_delete_variant_allowed_when_multiple_variants_exist():
    """This fixture campaign has all 4 variants (A–D) on disk — deleting
    any one of them must be allowed, since more than one remains after."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert any(b.key == "delete_variant_button" for b in at.button)


def test_delete_variant_actually_deletes_every_stage_file(tmp_path):
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    deleted_paths = []

    def fake_delete_file(self, path, message, branch="main"):
        deleted_paths.append(path)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.delete_file", fake_delete_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        # Default selectbox value is the first variant, "A".
        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "confirm_delete_variant")
        confirm_checkbox.set_value(True)
        at.run(timeout=15)

        delete_button = next(b for b in at.button if b.key == "delete_variant_button")
        delete_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    # 5 stages exist on disk for this fixture campaign — all 5 variant-A files deleted together.
    assert len(deleted_paths) == 5
    assert all(p.endswith("_A.txt") for p in deleted_paths)


def test_delete_stage_only_offered_for_the_last_stage():
    """This fixture campaign has 5 stages on disk (intro..followup4) —
    only followup4 should be deletable; the button must not exist for
    any earlier stage, since deleting a middle stage would orphan
    everything after it."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    delete_stage_button = next(b for b in at.button if b.key == "delete_stage_button")
    assert delete_stage_button.disabled is True  # confirm checkbox not checked yet
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "followup4" in markdown_text  # offered stage is genuinely the last one


def test_delete_stage_actually_deletes_all_variant_files_for_it():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    deleted_paths = []

    def fake_delete_file(self, path, message, branch="main"):
        deleted_paths.append(path)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.delete_file", fake_delete_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        confirm_checkbox = next(cb for cb in at.checkbox if cb.key == "confirm_delete_stage")
        confirm_checkbox.set_value(True)
        at.run(timeout=15)

        delete_button = next(b for b in at.button if b.key == "delete_stage_button")
        delete_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert len(deleted_paths) == 4  # 4 variants for followup4
    assert all("followup4_" in p for p in deleted_paths)


def test_schedule_tab_renders_sensible_defaults_for_unconfigured_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Schedule tab raised: {list(at.exception)}"
    assert list(at.error) == []
    tz_selector = next(sb for sb in at.selectbox if sb.label == "Time zone")
    assert tz_selector.value == "Pacific Time (US & Canada)"
    start_input = next(ti for ti in at.text_input if "Start time" in ti.label)
    assert start_input.value == "09:00"
    day_checkboxes = {cb.label: cb.value for cb in at.checkbox if cb.key and cb.key.startswith("schedule_day_")}
    assert day_checkboxes["Mon"] is True
    assert day_checkboxes["Sat"] is False


def test_schedule_tab_save_writes_correct_yaml_and_preserves_other_keys(tmp_path):
    (tmp_path / "Kelson_Creators_Licensing.yaml").write_text(
        "status: active\nsending:\n  daily_limit: 100\n"
    )
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.CAMPAIGNS_DIR", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        tz_selector = next(sb for sb in at.selectbox if sb.label == "Time zone")
        tz_selector.set_value("UTC")
        at.run(timeout=15)

        sat_checkbox = next(cb for cb in at.checkbox if cb.key == "schedule_day_sat")
        sat_checkbox.set_value(True)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Schedule")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Schedule save raised: {list(at.exception)}"
    assert list(at.error) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written["schedule"]["timezone"] == "UTC"
    assert "sat" in written["schedule"]["send_days"]
    assert written["status"] == "active"  # preserved
    assert written["sending"]["daily_limit"] == 100  # preserved


def test_schedule_tab_save_rejects_when_no_days_selected():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        # Uncheck every default-selected weekday.
        for code in ["mon", "tue", "wed", "thu", "fri"]:
            cb = next(c for c in at.checkbox if c.key == f"schedule_day_{code}")
            cb.set_value(False)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Schedule")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    error_texts = " ".join(e.value for e in at.error)
    assert "at least one day" in error_texts


def _fake_get_campaign_with_status(status: str):
    def fake_get_campaign(name, **kwargs):
        return {
            "_campaign_name": name,
            "stages": [{"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0}],
            "sending": {}, "status": status,
            "master_tab": f"{name} Master Sheet", "responses_tab": f"{name} Response Sheet",
            "send_log_tab": f"{name} Custom Log Sheet", "error_log_tab": f"{name} Error Log",
            "variants": ["A"], "_global_default_account": "sales1",
        }
    return fake_get_campaign


def test_status_controls_running_campaign_shows_pause_button():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    subheaders = [h.value for h in at.subheader]
    assert "🟢 Running" in subheaders
    assert any(b.label == "⏸ Pause" for b in at.button)
    assert not any(b.label in ("🚀 Launch", "▶ Resume") for b in at.button)


def test_status_controls_pause_button_commits_paused_status():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        pause_button = next(b for b in at.button if b.label == "⏸ Pause")
        pause_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written == {"status": "paused"}


def test_status_controls_paused_campaign_shows_resume_button():
    fake_spreadsheet = FakeSpreadsheet({})

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("paused")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "PausedCampaign"
        at.run(timeout=15)

    assert list(at.exception) == []
    subheaders = [h.value for h in at.subheader]
    assert "⏸ Paused" in subheaders
    assert any(b.label == "▶ Resume" for b in at.button)


def test_status_controls_resume_button_commits_active_status():
    fake_spreadsheet = FakeSpreadsheet({})
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("paused")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "PausedCampaign"
        at.run(timeout=15)

        resume_button = next(b for b in at.button if b.label == "▶ Resume")
        resume_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written == {"status": "active"}


def test_status_controls_draft_shows_launch_then_confirmation():
    fake_spreadsheet = FakeSpreadsheet({})

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("draft")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "DraftCampaign"
        at.run(timeout=15)

        assert any(b.label == "🚀 Launch" for b in at.button)
        assert not any(b.label == "Confirm Launch" for b in at.button)

        launch_button = next(b for b in at.button if b.label == "🚀 Launch")
        launch_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert any(b.label == "Confirm Launch" for b in at.button)
    assert any(b.label == "Cancel" for b in at.button)


def test_status_controls_confirm_launch_commits_active_status():
    fake_spreadsheet = FakeSpreadsheet({})
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("draft")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "DraftCampaign"
        at.run(timeout=15)

        launch_button = next(b for b in at.button if b.label == "🚀 Launch")
        launch_button.click()
        at.run(timeout=15)

        confirm_button = next(b for b in at.button if b.label == "Confirm Launch")
        confirm_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written == {"status": "active"}


def test_status_controls_cancel_launch_does_not_commit():
    fake_spreadsheet = FakeSpreadsheet({})
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("draft")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "DraftCampaign"
        at.run(timeout=15)

        launch_button = next(b for b in at.button if b.label == "🚀 Launch")
        launch_button.click()
        at.run(timeout=15)

        cancel_button = next(b for b in at.button if b.label == "Cancel")
        cancel_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    assert not any(b.label == "Confirm Launch" for b in at.button)


def test_status_controls_launch_confirmation_does_not_reopen_after_navigating_away():
    """Same class of bug as the New Campaign dialog — a confirmation box
    driven by session_state must reset on genuine navigation, or it
    silently reappears on an unrelated later visit."""
    fake_spreadsheet = FakeSpreadsheet({})

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("draft")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "DraftCampaign"
        at.run(timeout=15)

        launch_button = next(b for b in at.button if b.label == "🚀 Launch")
        launch_button.click()
        at.run(timeout=15)
        assert any(b.label == "Confirm Launch" for b in at.button)

        at.session_state["_active_page"] = "dashboard"  # simulate visiting another page
        at.run(timeout=15)  # return to Campaigns, without clicking Confirm or Cancel

    assert list(at.exception) == []
    assert not any(b.label == "Confirm Launch" for b in at.button)


def test_send_tab_send_batch_requires_typed_send_confirmation():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        # Click Send Batch WITHOUT typing SEND first.
        send_button = next(b for b in at.button if b.key == "campaigns_send_batch_button")
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert "workflow" not in captured  # never actually dispatched
    error_texts = " ".join(e.value for e in at.error)
    assert 'You must type "SEND"' in error_texts


def test_send_tab_send_batch_dispatches_with_correct_inputs_when_confirmed():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 42, "html_url": "https://github.com/x/actions/runs/42"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        confirm_input = next(ti for ti in at.text_input if ti.key == "campaigns_send_confirm_text")
        confirm_input.set_value("SEND")
        at.run(timeout=15)

        send_button = next(b for b in at.button if b.key == "campaigns_send_batch_button")
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Send raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "send_batch.yml"
    assert captured["inputs"]["campaign"] == "Kelson_Creators_Licensing"
    assert at.session_state["last_send_run_id"] == 42


def test_send_section_hidden_when_campaign_is_draft():
    """The real safety fix this locks in: Send must not even be offered
    for a Draft campaign — matching outreach.send_batch's own backend
    guard, which blocks Draft the same way it blocks Paused."""
    fake_spreadsheet = FakeSpreadsheet({})
    captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("draft")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "DraftCampaign"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert not any(b.key == "campaigns_send_batch_button" for b in at.button)  # never even offered
    info_texts = " ".join(i.value for i in at.info)
    assert "only available while a campaign is" in info_texts
    assert "Launch it above" in info_texts
    assert "workflow" not in captured  # nothing was ever triggered


def test_send_section_hidden_when_campaign_is_paused():
    fake_spreadsheet = FakeSpreadsheet({})

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("outreach.get_campaign", _fake_get_campaign_with_status("paused")):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "PausedCampaign"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert not any(b.key == "campaigns_send_batch_button" for b in at.button)
    info_texts = " ".join(i.value for i in at.info)
    assert "Resume it above" in info_texts


def test_send_section_visible_when_campaign_is_running():
    """The positive case — confirms the gate isn't accidentally hiding
    Send for the one status it should actually be available for."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert any(b.key == "campaigns_send_batch_button" for b in at.button)


def test_send_section_no_longer_shows_duplicate_limit_overrides():
    """The specific cleanup requested: daily_limit / per_account_daily_limit
    / sender_rotation overrides and the manual batch size input are gone
    from Send — those are already set once, above, in the same tab."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert not any(ni.key == "campaigns_send_daily_limit" for ni in at.number_input)
    assert not any(ni.key == "campaigns_send_per_account" for ni in at.number_input)
    assert not any(ni.key == "campaigns_send_batch_size" for ni in at.number_input)
    assert not any(sb.key == "campaigns_send_rotation" for sb in at.selectbox)
    # Stage/Variant/ignore-wait-days survive — those are genuinely per-run choices.
    assert any(b.key == "campaigns_send_ignore_wait_days" for b in at.checkbox)


def test_send_tab_check_replies_dispatches_correct_workflow():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 7, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        check_button = next(b for b in at.button if b.key == "campaigns_check_replies_button")
        check_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert captured["workflow"] == "check_replies.yml"
    assert captured["inputs"]["campaign"] == "Kelson_Creators_Licensing"


def test_send_tab_backfill_dispatches_correct_workflow_with_dry_run_default():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured = {}

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 9, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        backfill_button = next(b for b in at.button if b.key == "campaigns_run_backfill")
        backfill_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert captured["workflow"] == "backfill_thread_subject.yml"
    assert captured["inputs"]["dry_run"] == "true"  # dry_run checkbox defaults to True


def _responses_tab_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"LeadID": "5", "Email": "lead@abc.com", "Approval": "Yes", "SenderAccount": "sales1",
              "ThreadReferences": "<our1@mail.gmail.com>", "Status": "Stopped - Replied"}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet(
            [{"ResponseID": "r1", "LeadID": "5", "From": "lead@abc.com", "Subject": "Re: Hi there",
              "Snippet": "Interested, tell me more", "Classification": "Genuine Reply",
              "MessageID": "<inbound1@mail.gmail.com>", "ReceivedAt": "2026-08-28 18:07:00",
              "ActionTaken": "Stopped Sequence"}]
        ),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_responses_tab_shows_response_with_prefilled_reply_form():
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Responses tab raised: {list(at.exception)}"
    assert list(at.error) == []
    reply_inputs = {ti.label: ti.value for ti in at.text_input if ti.key and "reply_" in ti.key}
    assert reply_inputs["To"] == "lead@abc.com"
    assert reply_inputs["Subject"] == "Re: Hi there"
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "lead@abc.com" in markdown_text


def test_responses_tab_send_reply_commits_correct_payload_and_triggers_workflow():
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        body_input = next(ta for ta in at.text_area if ta.key and "reply_body" in ta.key)
        body_input.set_value("Thanks for your interest! Here is more info.")
        at.run(timeout=15)

        send_button = next(b for b in at.button if b.key and "send_reply" in b.key)
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Send reply raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "send_reply.yml"
    assert captured["inputs"]["campaign"] == "Kelson_Creators_Licensing"
    commit = captured["commits"][0]
    assert commit["path"].startswith("replies/Kelson_Creators_Licensing/")

    import json
    payload = json.loads(commit["content"].decode("utf-8"))
    assert payload["to"] == "lead@abc.com"
    assert payload["sender_account"] == "sales1"
    assert payload["in_reply_to"] == "<inbound1@mail.gmail.com>"
    assert payload["references"] == "<our1@mail.gmail.com> <inbound1@mail.gmail.com>"
    assert payload["body"] == "Thanks for your interest! Here is more info."


def test_responses_tab_send_reply_rejects_blank_body_without_committing():
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        # Leave body blank entirely — click Send Reply as-is.
        send_button = next(b for b in at.button if b.key and "send_reply" in b.key)
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    error_texts = " ".join(e.value for e in at.error)
    assert "Body is required" in error_texts


def test_responses_tab_send_reply_with_cc_and_bcc():
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        cc_input = next(ti for ti in at.text_input if ti.key and "reply_cc" in ti.key)
        cc_input.set_value("manager@abc.com")
        bcc_input = next(ti for ti in at.text_input if ti.key and "reply_bcc" in ti.key)
        bcc_input.set_value("audit@abc.com")
        body_input = next(ta for ta in at.text_area if ta.key and "reply_body" in ta.key)
        body_input.set_value("Looping you in.")
        at.run(timeout=15)

        send_button = next(b for b in at.button if b.key and "send_reply" in b.key)
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    import json
    payload = json.loads(captured["commits"][0]["content"].decode("utf-8"))
    assert payload["cc"] == ["manager@abc.com"]
    assert payload["bcc"] == ["audit@abc.com"]


def test_responses_tab_shows_info_when_no_responses_yet():
    fake_ws = _responses_tab_fake_ws()
    fake_ws["Kelson_Creators_Licensing Response Sheet"] = FakeWorksheet([])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "No responses yet" in info_texts


def test_responses_tab_check_replies_button_shows_even_with_zero_responses():
    """The real fix worth locking in: Check Replies moved into the
    Responses tab, but the tab used to `return` early when there were no
    responses yet — exactly the moment you'd most want to trigger a
    check. The trigger must render BEFORE that early return."""
    fake_ws = _responses_tab_fake_ws()
    fake_ws["Kelson_Creators_Licensing Response Sheet"] = FakeWorksheet([])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert any(b.key == "campaigns_check_replies_button" for b in at.button)
    info_texts = " ".join(i.value for i in at.info)
    assert "No responses yet" in info_texts  # both present together


def test_responses_tab_labels_stopped_vs_logged_only_correctly():
    """A predates-contact / unverified-match reply must be clearly
    labeled as NOT having stopped the sequence — this is the exact
    confusion this labeling exists to resolve (Classification alone reads
    ambiguously). Ported from the removed Controls page, since Responses
    is now the only place replies are shown."""
    fake_ws = _responses_tab_fake_ws()
    fake_ws["Kelson_Creators_Licensing Response Sheet"] = FakeWorksheet([
        {"ResponseID": "<m2>", "LeadID": "5", "Campaign": "Kelson_Creators_Licensing",
         "ReceivedAt": "2026-08-20 10:00:00", "From": "Old <old@abc.com>", "Subject": "Re: Old thread",
         "Snippet": "Okay", "Classification": "Genuine Reply", "MatchMethod": "Email",
         "MessageID": "<m2>", "InReplyTo": "<unrelated>", "ActionTaken": "Logged Only (Predates Contact)"},
    ])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    caption_texts = " ".join(c.value for c in at.caption)
    assert "NOT stopped" in caption_texts


def test_responses_tab_send_reply_with_attachment_round_trips_correctly():
    """The real end-to-end proof: an uploaded file's bytes survive
    base64-encoding into the committed payload and decode back to the
    exact original content."""
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        reply_uploader = next(fu for fu in at.file_uploader if fu.key and "reply_attachments" in fu.key)
        body_input = next(ta for ta in at.text_area if ta.key and "reply_body" in ta.key)
        body_input.set_value("Here is the photo you asked for.")
        reply_uploader.upload("photo.png", b"fake-png-bytes", "image/png")
        at.run(timeout=15)

        assert list(at.exception) == []
        send_button = next(b for b in at.button if b.key and "send_reply" in b.key)
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Send with attachment raised: {list(at.exception)}"
    assert list(at.error) == []
    import json
    import base64
    payload = json.loads(captured["commits"][0]["content"].decode("utf-8"))
    assert payload["attachments"] == [{"filename": "photo.png",
                                        "content_base64": base64.b64encode(b"fake-png-bytes").decode("ascii")}]
    assert base64.b64decode(payload["attachments"][0]["content_base64"]) == b"fake-png-bytes"


def test_responses_tab_send_reply_without_attachment_omits_attachments_key():
    fake_spreadsheet = FakeSpreadsheet(_responses_tab_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        body_input = next(ta for ta in at.text_area if ta.key and "reply_body" in ta.key)
        body_input.set_value("No attachment here.")
        at.run(timeout=15)

        send_button = next(b for b in at.button if b.key and "send_reply" in b.key)
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    import json
    payload = json.loads(captured["commits"][0]["content"].decode("utf-8"))
    assert "attachments" not in payload


def test_login_lockout_after_repeated_failures():
    from auth import hash_password

    salt = "testsalt"
    at = AppTest.from_file(os.path.join(os.path.dirname(PAGES_DIR), "app.py"))
    at.secrets["auth_users"] = {"alice": {"salt": salt, "password_hash": hash_password("testpass", salt)}}
    at.run()

    for _ in range(5):
        at.text_input[0].set_value("alice")
        at.text_input[1].set_value("wrong")
        at.button[0].click()
        at.run()

    assert "Locked for 60s" in at.error[0].value
    assert at.session_state["auth_locked_until"] > 0

    # Even the CORRECT password must be rejected while locked out.
    at.text_input[0].set_value("alice")
    at.text_input[1].set_value("testpass")
    at.button[0].click()
    at.run()
    assert at.session_state["auth_user"] is None
    assert "Try again in" in at.error[0].value


def test_pages_require_login_when_not_authenticated():
    """Every page must call login_gate() and stop — verified here by NOT
    setting auth_user and confirming the page doesn't render its main
    content (Dashboard title never appears)."""
    at = AppTest.from_file(os.path.join(PAGES_DIR, "dashboard.py"))
    at.secrets.update(_dashboard_secrets())
    at.run()

    assert list(at.exception) == []
    titles = [t.value for t in at.title]
    assert "📊 Dashboard" not in titles  # blocked by login gate before reaching st.title


def _responses_hub_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet([
            {"LeadID": "1", "Email": "lead1@abc.com", "Approval": "Yes", "SenderAccount": "sales1"},
            {"LeadID": "2", "Email": "old@abc.com", "Approval": "Yes", "SenderAccount": "sales1"},
        ]),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([
            {"ResponseID": "r1", "LeadID": "1", "From": "lead1@abc.com", "Subject": "Re: Hi",
             "Snippet": "Interested, tell me more", "Classification": "Genuine Reply",
             "MessageID": "<m1@mail.gmail.com>", "ReceivedAt": "2026-08-29 10:00:00",
             "ActionTaken": "Stopped Sequence"},
            {"ResponseID": "r2", "LeadID": "2", "From": "old@abc.com", "Subject": "Auto-reply",
             "Snippet": "Out of office", "Classification": "Auto-Reply",
             "MessageID": "<m2@mail.gmail.com>", "ReceivedAt": "2026-08-28 10:00:00",
             "ActionTaken": "Logged Only"},
        ]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_responses_hub_page_renders_without_exceptions():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

    assert list(at.exception) == [], f"Responses hub raised: {list(at.exception)}"
    assert list(at.error) == []
    titles = [t.value for t in at.title]
    assert "💬 Responses" in titles
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "lead1@abc.com" in markdown_text
    assert "old@abc.com" in markdown_text


def test_responses_hub_merely_loading_the_page_never_marks_anything_read():
    """The actual bug found and fixed: st.expander's body runs on every
    script rerun regardless of whether it's open or closed — a naive
    "mark read inside the expander" would mark EVERY response read the
    very first time the page loads, before anyone opened anything."""
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)
        at.run(timeout=15)  # a second rerun, in case a bug only manifests after the first pass

    assert list(at.exception) == []
    assert at.session_state["read_response_keys"] == set()
    assert at.session_state["pending_sync_keys"] == set()
    # Both responses should still show the unread marker — neither was
    # silently marked read just by the page rendering.
    markdown_text = " ".join(m.value for m in at.markdown)
    assert markdown_text.count("🔵") == 2


def test_responses_hub_status_filter_narrows_to_selected_classification():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        status_select = next(sb for sb in at.selectbox if sb.key == "responses_status_filter")
        status_select.set_value("Auto-Reply")
        at.run(timeout=15)

    assert list(at.exception) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "old@abc.com" in markdown_text
    assert "lead1@abc.com" not in markdown_text


def test_responses_hub_shows_intent_badge_when_classified():
    fake_ws = _responses_hub_fake_ws()
    fake_ws["Kelson_Creators_Licensing Response Sheet"] = FakeWorksheet([
        {"ResponseID": "r1", "LeadID": "1", "From": "lead1@abc.com", "Subject": "Re: Hi",
         "Snippet": "Interested, tell me more", "Classification": "Genuine Reply",
         "MessageID": "<m1@mail.gmail.com>", "ReceivedAt": "2026-08-29 10:00:00",
         "ActionTaken": "Stopped Sequence", "Intent": "Interested", "IntentConfidence": "High"},
    ])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

    assert list(at.exception) == []
    caption_texts = " ".join(c.value for c in at.caption)
    assert "🎯 Interested" in caption_texts
    assert "High confidence" in caption_texts


def test_responses_hub_status_filter_narrows_by_intent():
    fake_ws = _responses_hub_fake_ws()
    fake_ws["Kelson_Creators_Licensing Response Sheet"] = FakeWorksheet([
        {"ResponseID": "r1", "LeadID": "1", "From": "lead1@abc.com", "Subject": "Re: Hi",
         "Snippet": "Interested", "Classification": "Genuine Reply", "MessageID": "<m1@mail.gmail.com>",
         "ReceivedAt": "2026-08-29 10:00:00", "ActionTaken": "Stopped Sequence",
         "Intent": "Interested", "IntentConfidence": "High"},
        {"ResponseID": "r2", "LeadID": "2", "From": "old@abc.com", "Subject": "Re: Hey",
         "Snippet": "No thanks", "Classification": "Genuine Reply", "MessageID": "<m2@mail.gmail.com>",
         "ReceivedAt": "2026-08-28 10:00:00", "ActionTaken": "Stopped Sequence",
         "Intent": "Not Interested", "IntentConfidence": "High"},
    ])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        status_select = next(sb for sb in at.selectbox if sb.key == "responses_status_filter")
        status_select.set_value("Interested")
        at.run(timeout=15)

    assert list(at.exception) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "lead1@abc.com" in markdown_text
    assert "old@abc.com" not in markdown_text


def test_responses_hub_search_narrows_by_query():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        search_input = next(ti for ti in at.text_input if ti.key == "responses_search_query")
        search_input.set_value("interested")
        at.run(timeout=15)

    assert list(at.exception) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "lead1@abc.com" in markdown_text  # snippet is "Interested, tell me more"
    assert "old@abc.com" not in markdown_text  # snippet is "Out of office"


def test_responses_hub_conversation_view_shows_full_thread():
    fake_ws = _responses_hub_fake_ws()
    # Give lead1 an outgoing Intro too, so the thread has both directions.
    fake_ws["Kelson_Creators_Licensing Master Sheet"] = FakeWorksheet([
        {"LeadID": "1", "Email": "lead1@abc.com", "FirstName": "Sam", "Approval": "Yes",
         "SenderAccount": "sales1", "IntroSentAt": "2026-08-20 09:00:00", "IntroVariant": "A"},
        {"LeadID": "2", "Email": "old@abc.com", "Approval": "Yes", "SenderAccount": "sales1"},
    ])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        conversation_expander = next(e for e in at.expander if "conversation" in e.label)
        conversation_expander.expanded = True
        at.run(timeout=15)

    assert list(at.exception) == [], f"Conversation view raised: {list(at.exception)}"
    assert list(at.error) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "**You**" in markdown_text  # the re-rendered outgoing Intro
    body_text = " ".join(t.value for t in at.text)
    assert "Sam" in body_text  # template variable actually rendered for this lead


def test_responses_hub_campaign_filter_narrows_to_selected_campaign():
    fake_ws = _responses_hub_fake_ws()
    fake_ws["OtherCampaign Response Sheet"] = FakeWorksheet([
        {"ResponseID": "r3", "LeadID": "9", "From": "third@abc.com", "Subject": "Re: Hey",
         "Snippet": "Sounds good", "Classification": "Genuine Reply",
         "MessageID": "<m3@mail.gmail.com>", "ReceivedAt": "2026-08-27 10:00:00", "ActionTaken": "Stopped Sequence"},
    ])
    fake_ws["OtherCampaign Master Sheet"] = FakeWorksheet(
        [{"LeadID": "9", "Email": "third@abc.com", "Approval": "Yes", "SenderAccount": "sales1"}]
    )
    fake_ws["OtherCampaign Custom Log Sheet"] = FakeWorksheet([])
    fake_ws["OtherCampaign Error Log"] = FakeWorksheet([])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    def fake_get_campaign_cfg(name):
        return {
            "_campaign_name": name, "master_tab": f"{name} Master Sheet",
            "responses_tab": f"{name} Response Sheet", "send_log_tab": f"{name} Custom Log Sheet",
            "error_log_tab": f"{name} Error Log",
        }

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("preview_logic.list_campaigns", return_value=["Kelson_Creators_Licensing", "OtherCampaign"]), \
         patch("preview_logic.get_campaign_cfg", fake_get_campaign_cfg):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        campaign_select = next(sb for sb in at.selectbox if sb.key == "responses_campaign_filter")
        campaign_select.set_value("OtherCampaign")
        at.run(timeout=15)

    assert list(at.exception) == []
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "third@abc.com" in markdown_text
    assert "lead1@abc.com" not in markdown_text
    assert "old@abc.com" not in markdown_text


def test_responses_hub_unread_filter_hides_opened_response():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        # Explicitly marking r1 as read — merely opening an expander must
        # NOT do this on its own (st.expander's body runs every rerun
        # regardless of open/closed state).
        mark_read_button = next(b for b in at.button if "Mark as read" in b.label)
        mark_read_button.click()
        at.run(timeout=15)

        inbox_select = next(sb for sb in at.selectbox if sb.key == "responses_inbox_filter")
        inbox_select.set_value("Unread only")
        at.run(timeout=15)

    assert list(at.exception) == []


    assert list(at.exception) == []


def test_responses_hub_sync_read_status_marks_pending_and_dispatches_workflow():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())
    commits_captured, fake_create_file = _mock_github_writes()
    dispatched = []

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        dispatched.append((workflow_file, inputs.get("campaign")))
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        # Explicitly marking r1 as read queues it for sync — merely
        # opening the Reply expander must NOT do this on its own.
        mark_read_button = next(b for b in at.button if "Mark as read" in b.label)
        mark_read_button.click()
        at.run(timeout=15)

        assert at.session_state["pending_sync_keys"] == {"Kelson_Creators_Licensing:r1"}

        sync_button = next(b for b in at.button if "Sync read status" in b.label)
        sync_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Sync raised: {list(at.exception)}"
    assert list(at.error) == []
    assert dispatched == [("mark_responses_read.yml", "Kelson_Creators_Licensing")]
    import json as _json
    payload = _json.loads(commits_captured["commits"][0]["content"].decode("utf-8"))
    assert payload == {"response_ids": ["r1"]}
    assert at.session_state["pending_sync_keys"] == set()  # cleared after a successful sync


def test_responses_hub_check_replies_button_triggers_every_campaign():
    fake_spreadsheet = FakeSpreadsheet(_responses_hub_fake_ws())
    dispatched = []

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        dispatched.append((workflow_file, inputs.get("campaign")))
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        check_button = next(b for b in at.button if "Check Replies Now" in b.label)
        check_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert dispatched == [("check_replies.yml", "Kelson_Creators_Licensing")]


def test_responses_hub_reply_uses_correct_campaign_for_that_response():
    fake_ws = _responses_hub_fake_ws()
    fake_ws["OtherCampaign Response Sheet"] = FakeWorksheet([
        {"ResponseID": "r3", "LeadID": "9", "From": "third@abc.com", "Subject": "Re: Hey",
         "Snippet": "Sounds good", "Classification": "Genuine Reply",
         "MessageID": "<m3@mail.gmail.com>", "ReceivedAt": "2026-08-27 10:00:00", "ActionTaken": "Stopped Sequence"},
    ])
    fake_ws["OtherCampaign Master Sheet"] = FakeWorksheet(
        [{"LeadID": "9", "Email": "third@abc.com", "Approval": "Yes", "SenderAccount": "sales2"}]
    )
    fake_ws["OtherCampaign Custom Log Sheet"] = FakeWorksheet([])
    fake_ws["OtherCampaign Error Log"] = FakeWorksheet([])
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    commits_captured, fake_create_file = _mock_github_writes()
    dispatched = []

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        dispatched.append((workflow_file, inputs.get("campaign")))
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("preview_logic.list_campaigns", return_value=["Kelson_Creators_Licensing", "OtherCampaign"]), \
         patch("preview_logic.get_campaign_cfg", lambda name: {
             "_campaign_name": name, "master_tab": f"{name} Master Sheet",
             "responses_tab": f"{name} Response Sheet", "send_log_tab": f"{name} Custom Log Sheet",
             "error_log_tab": f"{name} Error Log",
         }), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "responses.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        campaign_select = next(sb for sb in at.selectbox if sb.key == "responses_campaign_filter")
        campaign_select.set_value("OtherCampaign")
        at.run(timeout=15)

        body_input = next(ta for ta in at.text_area if "hub_reply_body" in (ta.key or ""))
        body_input.set_value("Thanks!")
        at.run(timeout=15)

        send_button = next(b for b in at.button if "hub_send_reply" in (b.key or ""))
        send_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Send raised: {list(at.exception)}"
    assert list(at.error) == []
    assert dispatched[-1] == ("send_reply.yml", "OtherCampaign")
    import json as _json
    payload = _json.loads(commits_captured["commits"][0]["content"].decode("utf-8"))
    assert payload["to"] == "third@abc.com"
    assert payload["sender_account"] == "sales2"
