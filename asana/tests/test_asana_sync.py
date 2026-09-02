"""Tests for asana_sync.py. Two invariants matter more than the rest and
get dedicated sabotage checks: the per-campaign asana_sync gate must
actually be respected (this is the entire feature — never let a test
campaign's data reach Asana), and a row that's already synced must be
updated, never duplicated.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asana_sync as async_mod


def _row(dedup_key, campaign, outreach_channel="email", asana_task_id="", dm_status=""):
    return {"dedup_key": dedup_key, "Campaign": campaign, "outreach_channel": outreach_channel,
            "asana_task_id": asana_task_id, "dm_status": dm_status, "username": dedup_key.split(":")[-1],
            "platform": "instagram", "followers_count": "10000", "follower_verification": "verified"}


# ---------- select_rows_to_sync: the gate ----------

def test_respects_asana_sync_disabled_campaign():
    settings = {"TestCampaign": {"asana_sync": False}, "RealCampaign": {"asana_sync": True}}
    rows = [_row("a", "TestCampaign"), _row("b", "RealCampaign")]
    eligible = async_mod.select_rows_to_sync(rows, settings)
    assert {r["dedup_key"] for r in eligible} == {"b"}


def test_unconfigured_campaign_defaults_excluded():
    """No settings entry at all == asana_sync off by default (matches
    campaign_settings.py's own safety default) — must never sync."""
    rows = [_row("a", "NeverConfigured")]
    eligible = async_mod.select_rows_to_sync(rows, {})
    assert eligible == []


def test_excludes_none_and_blank_channel_rows():
    settings = {"X": {"asana_sync": True}}
    rows = [_row("a", "X", outreach_channel="none"), _row("b", "X", outreach_channel=""),
            _row("c", "X", outreach_channel="email")]
    eligible = async_mod.select_rows_to_sync(rows, settings)
    assert {r["dedup_key"] for r in eligible} == {"c"}


# ---------- build_task_payload ----------

def test_dm_row_notes_say_awaiting_manual_send_not_automated():
    row = _row("a", "X", outreach_channel="dm", dm_status="Sent")
    payload = async_mod.build_task_payload(row)
    assert "awaiting manual send" in payload["notes"]
    assert "Sent" in payload["notes"]


def test_email_row_notes_show_push_status():
    row = _row("a", "X", outreach_channel="email")
    row["campaign_push_status"] = "pushed"
    row["outreach_campaign"] = "Kelson_Creators_Licensing"
    payload = async_mod.build_task_payload(row)
    assert "pushed" in payload["notes"]
    assert "Kelson_Creators_Licensing" in payload["notes"]


def test_task_name_includes_username_and_campaign():
    row = _row("instagram:dudedad", "DudeRobe Creator Discovery")
    payload = async_mod.build_task_payload(row)
    assert payload["name"] == "dudedad — DudeRobe Creator Discovery"


# ---------- sync_campaign: create vs update vs dry-run ----------

class FakeAsanaClient:
    def __init__(self):
        self.created = []
        self.updated = []
        self._existing_gids = {"already-synced-gid"}

    def create_task(self, name, notes=""):
        gid = f"new-gid-{len(self.created)}"
        self.created.append({"name": name, "notes": notes, "gid": gid})
        return gid

    def update_task(self, task_gid, name=None, notes=None):
        self.updated.append({"gid": task_gid, "name": name, "notes": notes})

    def task_exists(self, task_gid):
        return task_gid in self._existing_gids


class FakeWorksheet:
    def __init__(self, header, rows):
        self.header = header
        self._rows = rows
        self.cells = {}

    def get_all_records(self):
        return [dict(r) for r in self._rows]

    def row_values(self, row_num):
        return list(self.header) if row_num == 1 else []

    def update_cell(self, row_num, col, value):
        self.cells[(row_num, col)] = value


HEADER = ["dedup_key", "Campaign", "outreach_channel", "asana_task_id", "asana_synced_at",
          "username", "platform", "followers_count", "follower_verification",
          "campaign_push_status", "outreach_campaign", "dm_status", "contact_email",
          "overall_fit", "content_angle", "dr_concerns"]


def test_new_row_gets_created_not_updated():
    ws = FakeWorksheet(HEADER, [_row("a", "X", asana_task_id="")])
    client = FakeAsanaClient()
    results = async_mod.sync_campaign(ws, {"X": {"asana_sync": True}}, client)
    assert results[0]["status"] == "created"
    assert len(client.created) == 1
    assert len(client.updated) == 0


def test_already_synced_row_gets_updated_not_duplicated():
    """The core idempotency guarantee."""
    ws = FakeWorksheet(HEADER, [_row("a", "X", asana_task_id="already-synced-gid")])
    client = FakeAsanaClient()
    results = async_mod.sync_campaign(ws, {"X": {"asana_sync": True}}, client)
    assert results[0]["status"] == "updated"
    assert len(client.created) == 0
    assert len(client.updated) == 1


def test_task_id_pointing_at_deleted_task_creates_fresh():
    """A task_id on the Sheet whose Asana task was deleted directly (not
    through this pipeline) must not raise or get stuck — falls through to
    creating a new one."""
    ws = FakeWorksheet(HEADER, [_row("a", "X", asana_task_id="a-gid-that-no-longer-exists")])
    client = FakeAsanaClient()
    results = async_mod.sync_campaign(ws, {"X": {"asana_sync": True}}, client)
    assert results[0]["status"] == "created"


def test_dry_run_makes_zero_client_calls_and_zero_writes():
    ws = FakeWorksheet(HEADER, [_row("a", "X", asana_task_id="")])

    class _FailIfCalled:
        def create_task(self, *a, **k):
            raise AssertionError("must not create a real task during dry run")

        def update_task(self, *a, **k):
            raise AssertionError("must not update a real task during dry run")

        def task_exists(self, *a, **k):
            raise AssertionError("must not check task existence during dry run")

    results = async_mod.sync_campaign(ws, {"X": {"asana_sync": True}}, _FailIfCalled(), dry_run=True)
    assert results[0]["status"] == "preview"
    assert ws.cells == {}


def test_one_row_failing_does_not_block_the_rest():
    ws = FakeWorksheet(HEADER, [_row("a", "X", asana_task_id=""), _row("b", "X", asana_task_id="")])

    class _FailsOnFirstCreate:
        def __init__(self):
            self.calls = 0

        def create_task(self, name, notes=""):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Asana is down")
            return "gid-2"

        def update_task(self, *a, **k):
            pass

        def task_exists(self, *a, **k):
            return False

    results = async_mod.sync_campaign(ws, {"X": {"asana_sync": True}}, _FailsOnFirstCreate())
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["created", "failed"]


def test_campaign_disabled_row_never_reaches_the_client():
    ws = FakeWorksheet(HEADER, [_row("a", "DisabledCampaign", asana_task_id="")])

    class _FailIfCalled:
        def create_task(self, *a, **k):
            raise AssertionError("a disabled campaign's row must never reach the Asana client")

        def update_task(self, *a, **k):
            raise AssertionError("a disabled campaign's row must never reach the Asana client")

        def task_exists(self, *a, **k):
            raise AssertionError("a disabled campaign's row must never reach the Asana client")

    results = async_mod.sync_campaign(ws, {"DisabledCampaign": {"asana_sync": False}}, _FailIfCalled())
    assert results == []
