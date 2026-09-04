import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from settings_logic import (
    load_raw_override, validate_settings, build_updated_override,
    override_to_yaml_bytes, override_file_path,
)


# ---------- load_raw_override ----------

def test_load_raw_override_returns_empty_dict_when_file_missing(tmp_path):
    assert load_raw_override("NoSuchCampaign", str(tmp_path)) == {}


def test_load_raw_override_reads_existing_file(tmp_path):
    (tmp_path / "Foo.yaml").write_text("status: paused\nsending:\n  daily_limit: 50\n")
    result = load_raw_override("Foo", str(tmp_path))
    assert result == {"status": "paused", "sending": {"daily_limit": 50}}


def test_load_raw_override_empty_file_returns_empty_dict(tmp_path):
    (tmp_path / "Foo.yaml").write_text("")
    assert load_raw_override("Foo", str(tmp_path)) == {}


# ---------- validate_settings ----------

def test_validate_settings_valid():
    assert validate_settings(100, 20) == []


def test_validate_settings_valid_with_no_per_account_limit():
    assert validate_settings(100, None) == []


def test_validate_settings_rejects_non_positive_daily_limit():
    assert len(validate_settings(0, None)) == 1
    assert len(validate_settings(-5, None)) == 1


def test_validate_settings_rejects_non_positive_per_account_limit():
    assert len(validate_settings(100, 0)) == 1
    assert len(validate_settings(100, -1)) == 1


def test_validate_settings_reports_both_errors_at_once():
    errors = validate_settings(0, -1)
    assert len(errors) == 2


# ---------- build_updated_override — the "preserve everything else" guarantee ----------

def test_build_updated_override_preserves_status_and_other_top_level_keys():
    raw = {"status": "paused", "schedule": {"timezone": "America/Los_Angeles"}, "sending": {"daily_limit": 50}}
    updated = build_updated_override(raw, daily_limit=200, per_account_daily_limit=None,
                                      sender_rotation=False, rotation_accounts=[])
    assert updated["status"] == "paused"
    assert updated["schedule"] == {"timezone": "America/Los_Angeles"}
    assert updated["sending"]["daily_limit"] == 200


def test_build_updated_override_never_mutates_input():
    raw = {"status": "active", "sending": {"daily_limit": 50}}
    build_updated_override(raw, daily_limit=200, per_account_daily_limit=None,
                            sender_rotation=False, rotation_accounts=[])
    assert raw == {"status": "active", "sending": {"daily_limit": 50}}  # untouched


def test_build_updated_override_preserves_stages_and_variants_if_explicit():
    raw = {"stages": [{"name": "intro"}], "variants": ["A"], "sending": {}}
    updated = build_updated_override(raw, daily_limit=100, per_account_daily_limit=None,
                                      sender_rotation=False, rotation_accounts=[])
    assert updated["stages"] == [{"name": "intro"}]
    assert updated["variants"] == ["A"]


def test_build_updated_override_sets_per_account_limit_when_given():
    updated = build_updated_override({}, daily_limit=100, per_account_daily_limit=20,
                                      sender_rotation=True, rotation_accounts=["sales1"])
    assert updated["sending"]["per_account_daily_limit"] == 20


def test_build_updated_override_removes_per_account_limit_when_none_and_previously_set():
    raw = {"sending": {"per_account_daily_limit": 20}}
    updated = build_updated_override(raw, daily_limit=100, per_account_daily_limit=None,
                                      sender_rotation=False, rotation_accounts=[])
    assert "per_account_daily_limit" not in updated["sending"]


def test_build_updated_override_sets_rotation_accounts_when_given():
    updated = build_updated_override({}, daily_limit=100, per_account_daily_limit=None,
                                      sender_rotation=True, rotation_accounts=["sales1", "sales2"])
    assert updated["sending"]["rotation_accounts"] == ["sales1", "sales2"]


def test_build_updated_override_removes_rotation_accounts_when_empty_and_previously_set():
    raw = {"sending": {"rotation_accounts": ["sales1"]}}
    updated = build_updated_override(raw, daily_limit=100, per_account_daily_limit=None,
                                      sender_rotation=False, rotation_accounts=[])
    assert "rotation_accounts" not in updated["sending"]


def test_build_updated_override_from_completely_empty_raw():
    updated = build_updated_override({}, daily_limit=100, per_account_daily_limit=None,
                                      sender_rotation=False, rotation_accounts=[])
    assert updated == {"sending": {"daily_limit": 100, "sender_rotation": False}}


# ---------- override_to_yaml_bytes / override_file_path ----------

def test_override_to_yaml_bytes_round_trips():
    import yaml
    override = {"status": "active", "sending": {"daily_limit": 100}}
    raw = override_to_yaml_bytes(override)
    assert yaml.safe_load(raw.decode("utf-8")) == override


def test_override_file_path_format():
    assert override_file_path("DudeRobe") == "outreach/config/campaigns/DudeRobe.yaml"


def test_full_round_trip_load_edit_save_reload(tmp_path):
    """The real end-to-end contract: write a file, load it, edit it,
    serialize it, write it back, load it again — confirms nothing is
    lost or corrupted across the whole cycle."""
    original_path = tmp_path / "Foo.yaml"
    original_path.write_text("status: paused\nsending:\n  daily_limit: 50\n  sender_rotation: true\n")

    raw = load_raw_override("Foo", str(tmp_path))
    updated = build_updated_override(raw, daily_limit=999, per_account_daily_limit=30,
                                      sender_rotation=True, rotation_accounts=["sales1"])
    yaml_bytes = override_to_yaml_bytes(updated)
    original_path.write_bytes(yaml_bytes)  # simulate the commit landing back at the same path

    reloaded = load_raw_override("Foo", str(tmp_path))
    assert reloaded["status"] == "paused"  # preserved
    assert reloaded["sending"]["daily_limit"] == 999
    assert reloaded["sending"]["per_account_daily_limit"] == 30
    assert reloaded["sending"]["rotation_accounts"] == ["sales1"]
