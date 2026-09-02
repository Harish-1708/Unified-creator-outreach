"""Tests for campaign_settings.py. The single most important invariant:
a campaign nobody has configured yet must default to asana_sync = False —
that's the entire point of this feature (never let a fresh test campaign
silently start syncing to Asana), so it gets its own dedicated,
sabotage-verified test rather than being incidental to another one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import campaign_settings as cs


def test_load_all_settings_missing_file_returns_empty_dict(tmp_path):
    result = cs.load_all_settings(path=str(tmp_path / "does_not_exist.yaml"))
    assert result == {}


def test_unconfigured_campaign_defaults_to_asana_sync_off():
    """The safety-critical case: a campaign with NO entry at all in
    settings — including a brand-new one nobody has visited the settings
    page for — must default OFF, not ON."""
    all_settings = {"Kelson_Creators_Licensing": {"asana_sync": True}}
    assert cs.is_asana_sync_enabled("Harish_Testing_25AUG", all_settings) is False


def test_explicitly_enabled_campaign_returns_true():
    all_settings = {"Kelson_Creators_Licensing": {"asana_sync": True}}
    assert cs.is_asana_sync_enabled("Kelson_Creators_Licensing", all_settings) is True


def test_explicitly_disabled_campaign_returns_false():
    all_settings = {"Harish_Testing_25AUG": {"asana_sync": False}}
    assert cs.is_asana_sync_enabled("Harish_Testing_25AUG", all_settings) is False


def test_get_campaign_settings_merges_over_defaults_not_replaces():
    """Future settings fields (there will likely be more than just
    asana_sync eventually) must survive even for a campaign that's only
    ever had ONE field explicitly set — merging over defaults, not
    replacing them wholesale."""
    all_settings = {"Kelson_Creators_Licensing": {"asana_sync": True}}
    result = cs.get_campaign_settings("Kelson_Creators_Licensing", all_settings)
    assert result["asana_sync"] is True


def test_build_updated_settings_yaml_preserves_other_campaigns(tmp_path):
    all_settings = {
        "Kelson_Creators_Licensing": {"asana_sync": True},
        "Harish_Testing_25AUG": {"asana_sync": False},
    }
    new_yaml = cs.build_updated_settings_yaml(all_settings, "Harish_Testing_25AUG", asana_sync=True)

    path = tmp_path / "campaign_settings.yaml"
    path.write_text(new_yaml)
    reloaded = cs.load_all_settings(str(path))

    assert reloaded["Harish_Testing_25AUG"]["asana_sync"] is True
    assert reloaded["Kelson_Creators_Licensing"]["asana_sync"] is True  # untouched


def test_build_updated_settings_yaml_creates_new_campaign_entry(tmp_path):
    """A campaign with no prior entry at all must still get one created
    cleanly — this is the first-time-toggling-a-brand-new-campaign case."""
    new_yaml = cs.build_updated_settings_yaml({}, "Brand_New_Campaign", asana_sync=True)
    path = tmp_path / "campaign_settings.yaml"
    path.write_text(new_yaml)
    reloaded = cs.load_all_settings(str(path))
    assert reloaded["Brand_New_Campaign"]["asana_sync"] is True
