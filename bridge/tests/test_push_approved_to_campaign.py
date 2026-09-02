"""Tests for the discovery -> outreach bridge. Fakes out SheetsConnector,
get_campaign, and import_leads at the names the bridge module imported them
under — never touches real Sheets, real outreach.py internals, or real
network calls. outreach.py itself is not modified anywhere by this bridge,
so there's nothing to regress in it; these tests are entirely about the
bridge's own mapping and push logic.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "outreach"))

import push_approved_to_campaign as bridge


class FakeConfigError(Exception):
    pass


class FakeSheetsConnector:
    """Stands in for outreach.py's real SheetsConnector — records what
    import_leads() would have appended, and hands it back via
    get_all_leads() with sequential LeadIDs, mirroring the real function's
    own numbering behavior closely enough for these tests."""

    def __init__(self, *args, **kwargs):
        self._leads = []
        self._next_id = 1

    def get_all_leads(self):
        return list(self._leads)

    def append_lead(self, fields):
        row = dict(fields)
        row["LeadID"] = str(self._next_id)
        self._next_id += 1
        self._leads.append(row)

    def seed_existing_lead(self, email):
        self._leads.append({"LeadID": str(self._next_id), "Email": email})
        self._next_id += 1


def _fake_import_leads(sheets, campaign_name, new_leads):
    """A faithful-enough stand-in for the real import_leads(): skips blank
    or duplicate (case-insensitive) emails, appends the rest."""
    existing_emails = {(l.get("Email") or "").strip().lower() for l in sheets.get_all_leads()}
    imported = skipped_dup = skipped_no_email = 0
    for lead in new_leads:
        email = (lead.get("Email") or "").strip()
        if not email:
            skipped_no_email += 1
            continue
        if email.lower() in existing_emails:
            skipped_dup += 1
            continue
        row = dict(lead)
        row["Campaign"] = campaign_name
        sheets.append_lead(row)
        existing_emails.add(email.lower())
        imported += 1
    return {"imported": imported, "skipped_duplicate": skipped_dup, "skipped_no_email": skipped_no_email}


class FakeDiscoveryWorksheet:
    """Minimal fake of the discovery Shortlist worksheet — only the two
    methods _write_push_result actually calls."""

    def __init__(self, header):
        self.header = header
        self.cells = {}  # (row, col) -> value

    def row_values(self, row_num):
        return list(self.header) if row_num == 1 else []

    def update_cell(self, row_num, col, value):
        self.cells[(row_num, col)] = value

    def written_row(self, row_num):
        """Convenience for assertions: {column_name: value} for one row."""
        out = {}
        for (r, c), v in self.cells.items():
            if r == row_num:
                out[self.header[c - 1]] = v
        return out


DISCOVERY_HEADER = [
    "dedup_key", "platform", "contact_email", "username", "dr_name",
    "product_fit_score", "content_angle", "fit_explanation",
    "campaign_push_status", "outreach_campaign", "outreach_record_id", "pushed_at", "push_error",
]


def _creator(dedup_key, row, contact_email="creator@example.com", dr_name="", username="handle"):
    return {
        "dedup_key": dedup_key, "_row": row, "platform": "instagram",
        "contact_email": contact_email, "username": username, "dr_name": dr_name,
        "product_fit_score": "8", "content_angle": "Post-shower routine",
        "fit_explanation": "Strong persona match.",
    }


def _patch(monkeypatch, campaign_cfg_or_error=None):
    monkeypatch.setattr(bridge, "SheetsConnector", FakeSheetsConnector)
    monkeypatch.setattr(bridge, "import_leads", _fake_import_leads)
    monkeypatch.setattr(bridge, "ConfigError", FakeConfigError)
    if isinstance(campaign_cfg_or_error, Exception):
        def _raise(name):
            raise campaign_cfg_or_error
        monkeypatch.setattr(bridge, "get_campaign", _raise)
    else:
        cfg = campaign_cfg_or_error or {
            "sheet_id": "fake", "master_tab": "M", "responses_tab": "R",
            "send_log_tab": "S", "error_log_tab": "E", "dashboard_tab": "D",
        }
        monkeypatch.setattr(bridge, "get_campaign", lambda name: cfg)


# ---------- mapping ----------

def test_map_creator_prefers_dr_name_first_token_for_first_name():
    creator = _creator("instagram:dudedad", row=2, dr_name="Taylor Calmus", username="dudedad")
    lead = bridge.map_creator_to_lead(creator)
    assert lead["FirstName"] == "Taylor"


def test_map_creator_falls_back_to_username_when_no_dr_name():
    creator = _creator("instagram:dudedad", row=2, dr_name="", username="dudedad")
    lead = bridge.map_creator_to_lead(creator)
    assert lead["FirstName"] == "dudedad"


def test_map_creator_includes_custom_columns():
    creator = _creator("instagram:dudedad", row=2)
    lead = bridge.map_creator_to_lead(creator)
    assert lead["SourceCreatorID"] == "instagram:dudedad"
    assert lead["Platform"] == "instagram"
    assert lead["ProductFitScore"] == "8"
    assert lead["ContentAngle"] == "Post-shower routine"
    assert lead["FitExplanation"] == "Strong persona match."


# ---------- successful push ----------

def test_successful_push_writes_pushed_status_and_record_id(monkeypatch):
    _patch(monkeypatch)
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [_creator("instagram:dudedad", row=2, contact_email="dudedad@example.com")]

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach")

    assert results[0]["status"] == "pushed"
    assert results[0]["outreach_record_id"] == "1"
    written = ws.written_row(2)
    assert written["campaign_push_status"] == "pushed"
    assert written["outreach_record_id"] == "1"
    assert written["outreach_campaign"] == "DudeRobe – UGC Outreach"
    assert written["pushed_at"]


# ---------- missing email ----------

def test_missing_email_is_skipped_not_pushed(monkeypatch):
    _patch(monkeypatch)
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [_creator("instagram:noemail", row=3, contact_email="")]

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach")

    assert results[0]["status"] == "skipped_no_email"
    assert ws.written_row(3)["campaign_push_status"] == "skipped_no_email"
    assert "outreach_record_id" not in ws.written_row(3) or not ws.written_row(3)["outreach_record_id"]


# ---------- duplicate email ----------

def test_duplicate_email_against_existing_lead_is_skipped(monkeypatch):
    _patch(monkeypatch)
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [_creator("instagram:dupe", row=4, contact_email="already@example.com")]

    # Simulate the target campaign already having this email as a lead —
    # requires reaching into the fake connector import_leads() will build.
    # Patch SheetsConnector to pre-seed one existing lead.
    class SeededConnector(FakeSheetsConnector):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.seed_existing_lead("already@example.com")
    monkeypatch.setattr(bridge, "SheetsConnector", SeededConnector)

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach")

    assert results[0]["status"] == "skipped_duplicate"
    assert ws.written_row(4)["campaign_push_status"] == "skipped_duplicate"


# ---------- campaign doesn't exist: whole batch fails, loudly ----------

def test_nonexistent_campaign_fails_every_creator_in_batch(monkeypatch):
    _patch(monkeypatch, campaign_cfg_or_error=FakeConfigError(
        "No templates found for campaign 'Ghost Campaign' — expected a folder at 'templates/Ghost Campaign'. "
        "Currently available campaigns: DudeRobe – UGC Outreach, Kelson_Creators_Licensing"
    ))
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [_creator("instagram:a", row=2), _creator("instagram:b", row=3)]

    results = bridge.push_creators_to_outreach(ws, creators, "Ghost Campaign")

    assert all(r["status"] == "failed" for r in results)
    assert "No templates found" in results[0]["error"]
    assert ws.written_row(2)["campaign_push_status"] == "failed"
    assert ws.written_row(3)["campaign_push_status"] == "failed"
    assert "No templates found" in ws.written_row(2)["push_error"]


# ---------- dry run: zero writes, zero connector calls ----------

def test_dry_run_writes_nothing_and_builds_no_connector(monkeypatch):
    _patch(monkeypatch)

    def _fail_if_called(*a, **k):
        raise AssertionError("SheetsConnector must not be built during a dry run")
    monkeypatch.setattr(bridge, "SheetsConnector", _fail_if_called)

    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [_creator("instagram:dudedad", row=2, contact_email="dudedad@example.com")]

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach", dry_run=True)

    assert results[0]["status"] == "preview"
    assert results[0]["lead"]["Email"] == "dudedad@example.com"
    assert ws.cells == {}  # nothing written to the discovery sheet either


# ---------- partial batch: one succeeds, one has no email ----------

def test_partial_batch_isolates_each_creators_outcome(monkeypatch):
    _patch(monkeypatch)
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [
        _creator("instagram:good", row=2, contact_email="good@example.com"),
        _creator("instagram:bad", row=3, contact_email=""),
    ]

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach")
    by_key = {r["dedup_key"]: r for r in results}

    assert by_key["instagram:good"]["status"] == "pushed"
    assert by_key["instagram:bad"]["status"] == "skipped_no_email"


def test_two_creators_sharing_one_email_only_first_credited_as_pushed(monkeypatch):
    """A second, independent gap the email-matching approach could miss:
    two different creators mapped to the same contact email in one batch.
    Only one real lead gets created; only one should be marked pushed."""
    _patch(monkeypatch)
    ws = FakeDiscoveryWorksheet(DISCOVERY_HEADER)
    creators = [
        _creator("instagram:agency_a", row=2, contact_email="agency@example.com"),
        _creator("instagram:agency_b", row=3, contact_email="agency@example.com"),
    ]

    results = bridge.push_creators_to_outreach(ws, creators, "DudeRobe – UGC Outreach")
    statuses = sorted(r["status"] for r in results)

    assert statuses == ["pushed", "skipped_duplicate"]
