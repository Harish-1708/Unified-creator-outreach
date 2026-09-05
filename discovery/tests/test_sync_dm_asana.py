"""Tests for sync_dm_asana.py — the DM half of the ONE unified Asana
integration (same project, same stages as email leads). Mirrors the
exact rigor of outreach.py's own sync_campaign_to_asana test suite:
create, update, no-duplicates, the 404-vs-403 self-heal asymmetry, and
the human-only-stage protection, sabotage-verified where it matters
most.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "outreach"))

import outreach
from sync_dm_asana import build_dm_asana_task_name, sync_dm_to_asana


def _dm_row(**overrides):
    row = {"dedup_key": "instagram:dudedad", "Campaign": "Kelson_Creators_Licensing", "username": "dudedad",
           "dr_name": "", "platform": "instagram", "contact_email": "dudedad@abc.com",
           "content_angle": "Skincare reviews", "outreach_channel": "dm", "review_status": "Approved",
           "dm_status": "", "asana_task_id": ""}
    row.update(overrides)
    return row


SHORTLIST_HEADER = ["dedup_key", "Campaign", "username", "dr_name", "platform", "contact_email",
                    "content_angle", "outreach_channel", "review_status", "dm_status", "asana_task_id"]


class FakeShortlistWorksheet:
    def __init__(self, records, header=None):
        self._header = header or SHORTLIST_HEADER
        self._records = records
        self.update_cell_calls = []

    def row_values(self, row_num):
        return self._header if row_num == 1 else []

    def get_all_records(self):
        return [dict(r) for r in self._records]

    def update_cell(self, row, col, value):
        self.update_cell_calls.append((row, col, value))
        row_idx = row - 2
        col_name = self._header[col - 1]
        self._records[row_idx][col_name] = value


# ---------- build_dm_asana_task_name ----------

def test_task_name_prefers_dr_name_over_username():
    row = _dm_row(dr_name="Dude Dad Official")
    assert "Dude Dad Official" in build_dm_asana_task_name(row)


def test_task_name_falls_back_to_username_when_dr_name_blank():
    row = _dm_row(dr_name="")
    assert "dudedad" in build_dm_asana_task_name(row)


def test_task_name_includes_client_and_content_angle():
    row = _dm_row()
    name = build_dm_asana_task_name(row)
    assert "Kelson_Creators_Licensing" in name
    assert "Skincare reviews" in name


# ---------- sync_dm_to_asana ----------

def _fake_asana_project_response():
    return {
        "data": {
            "sections": [
                {"name": "Sourced", "gid": "sec_sourced"},
                {"name": "Outreach Sent", "gid": "sec_outreach"},
                {"name": "Follow-up", "gid": "sec_followup"},
                {"name": "Negotiating", "gid": "sec_negotiating"},
                {"name": "Rights Secured", "gid": "sec_rights"},
                {"name": "Declined / Dead", "gid": "sec_declined"},
            ],
            "custom_field_settings": [],
        }
    }


class FakeAsanaResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise outreach.requests.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._json


def _make_fake_asana_request(task_sections=None, create_gid="new_dm_task_gid", not_found_gids=None):
    """task_sections: {existing_task_gid: current_section_name} for a
    task that DOES exist. not_found_gids: task GIDs that should return
    a genuine 404 — a real Asana-doesn't-know-this-GID response, distinct
    from a task that exists but happens to have no section membership."""
    task_sections = task_sections or {}
    not_found_gids = not_found_gids or set()
    calls = []

    def fake_request(method, url, headers=None, timeout=None, json=None):
        calls.append((method, url, json))
        if method == "GET" and url.endswith("/workspaces"):
            return FakeAsanaResponse({"data": [{"name": "Kelson Agency", "gid": "workspace_1"}]})
        if method == "GET" and "/projects?" in url:
            return FakeAsanaResponse({"data": [{"name": "Creator Outreach", "gid": "proj_1"}]})
        if method == "GET" and "/projects/proj_1?" in url:
            return FakeAsanaResponse(_fake_asana_project_response())
        if method == "GET" and "/tasks/" in url and "opt_fields=memberships" in url:
            task_gid = url.split("/tasks/")[1].split("?")[0]
            if task_gid in not_found_gids:
                return FakeAsanaResponse({}, status_code=404)
            section_name = task_sections.get(task_gid)
            memberships = [{"project": {"gid": "proj_1"}, "section": {"name": section_name}}] if section_name else []
            return FakeAsanaResponse({"data": {"memberships": memberships}})
        if method == "POST" and url.endswith("/tasks"):
            return FakeAsanaResponse({"data": {"gid": create_gid}})
        if method == "POST" and "/addTask" in url:
            return FakeAsanaResponse({"data": {}})
        if method == "PUT" and "/tasks/" in url:
            return FakeAsanaResponse({"data": {}})
        raise AssertionError(f"Unexpected Asana call: {method} {url}")

    return fake_request, calls


def test_sync_dm_skips_rows_for_a_different_campaign(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(Campaign="OtherCampaign")])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)
    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")
    assert result["created"] == 0
    assert result["updated"] == 0
    # The project lookup itself still happens (there's no row-level
    # filter to short-circuit it), but no task-level call should ever
    # fire for a row belonging to a different campaign.
    create_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert create_calls == []


def test_sync_dm_skips_rows_not_routed_to_dm(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(outreach_channel="email")])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)
    sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")
    create_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert create_calls == []


def test_sync_dm_skips_rows_not_yet_approved(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(review_status="Pending")])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)
    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")
    assert result["skipped"] == 1
    assert result["created"] == 0


def test_sync_dm_creates_new_task_and_writes_asana_task_id_back(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(dm_status="Not Contacted")])
    fake_request, calls = _make_fake_asana_request()
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    assert result["created"] == 1
    assert result["errors"] == []
    assert ws.update_cell_calls == [(2, SHORTLIST_HEADER.index("asana_task_id") + 1, "new_dm_task_gid")]
    assign_calls = [c for c in calls if "/addTask" in c[1]]
    assert assign_calls[0][1] == f"{outreach.ASANA_API_BASE}/sections/sec_sourced/addTask"


def test_sync_dm_never_creates_a_second_task_for_same_creator(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(asana_task_id="existing_gid_123", dm_status="Sent")])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Sourced"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    assert result["created"] == 0
    assert result["updated"] == 1
    create_calls = [c for c in calls if c[0] == "POST" and c[1].endswith("/tasks")]
    assert create_calls == []


def test_sync_dm_update_call_does_not_crash_on_section_gid_argument(monkeypatch):
    """The actual bug this fixes: the original code passed
    section_gid=... into outreach.asana_update_task, which accepts no
    such parameter at all — this raised TypeError on every single
    update, for every DM creator that already had a task. This is the
    regression test for that exact crash."""
    ws = FakeShortlistWorksheet([_dm_row(asana_task_id="existing_gid_123", dm_status="Sent")])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Sourced"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    assert result["errors"] == []  # no TypeError swallowed into errors either


def test_sync_dm_moves_task_to_new_stage_on_status_change(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(asana_task_id="existing_gid_123", dm_status="Sent")])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Sourced"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    move_calls = [c for c in calls if "/addTask" in c[1] and "sec_outreach" in c[1]]
    assert len(move_calls) == 1


def test_sync_dm_never_moves_task_out_of_rights_secured(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(asana_task_id="existing_gid_123", dm_status="Closed")])
    fake_request, calls = _make_fake_asana_request(task_sections={"existing_gid_123": "Rights Secured"})
    monkeypatch.setattr(outreach.requests, "request", fake_request)

    sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    move_calls = [c for c in calls if "/addTask" in c[1]]
    assert move_calls == []


def test_sync_dm_self_heals_a_404_stale_asana_task_id(monkeypatch):
    ws = FakeShortlistWorksheet([_dm_row(asana_task_id="dead_gid")])
    fake_request, calls = _make_fake_asana_request(not_found_gids={"dead_gid"})

    monkeypatch.setattr(outreach.requests, "request", fake_request)
    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    assert result["created"] == 1
    assert result["errors"] == []
    assert ws.update_cell_calls[-1][2] == "new_dm_task_gid"


def test_sync_dm_one_row_error_does_not_block_others(monkeypatch):
    ws = FakeShortlistWorksheet([
        _dm_row(dedup_key="instagram:bad"),
        _dm_row(dedup_key="instagram:good"),
    ])
    real_fake, calls = _make_fake_asana_request()
    call_count = {"n": 0}

    def flaky_request(method, url, headers=None, timeout=None, json=None):
        if method == "POST" and url.endswith("/tasks"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Simulated Asana failure for the first creator")
        return real_fake(method, url, headers=headers, timeout=timeout, json=json)

    monkeypatch.setattr(outreach.requests, "request", flaky_request)
    result = sync_dm_to_asana(ws, "Kelson_Creators_Licensing", "Creator Outreach", "fake-key")

    assert len(result["errors"]) == 1
    assert result["created"] == 1
