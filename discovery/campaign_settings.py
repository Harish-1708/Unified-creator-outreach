"""
campaign_settings.py — per-(discovery)-Campaign settings. Currently just
Asana sync ON/OFF, but structured to hold more later without a redesign.

Lives here, in discovery/, because "Campaign" is a discovery-side concept,
and this setting has to gate BOTH the email and DM channels uniformly.
It can't live inside outreach.py's own per-campaign YAML overrides:
outreach.py's "campaign" is a narrower, different concept — one discovery
Campaign can route creators into several different outreach campaigns
(manual selection at push time, see the bridge), and DM creators never
touch an outreach campaign at all. Discovery Campaign is the only concept
both channels share.

Stored as one YAML file, config/campaign_settings.yaml, one entry per
discovery Campaign name — deliberately NOT a Google Sheet tab, since this
is pipeline configuration, not creator data, matching every other
per-campaign setting already in this build (outreach's own
config/campaigns/*.yaml, email_account_slots.yaml).
"""
import yaml

DEFAULT_SETTINGS = {"asana_sync": False}


def load_all_settings(path: str = "config/campaign_settings.yaml") -> dict:
    """Returns {} if the file doesn't exist yet — a repo that's never
    configured any campaign's settings is a legitimate, common state
    (every campaign just behaves as all-defaults), not an error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def get_campaign_settings(campaign: str, all_settings: dict) -> dict:
    """Merges a campaign's stored settings over the defaults. A campaign
    with no entry at all — never configured, including a brand-new
    campaign nobody's visited the settings page for yet — behaves exactly
    like one explicitly set to every default. For asana_sync that means
    OFF: a new campaign never starts syncing to Asana just because nobody
    got around to its settings page. This is the safety-critical direction
    to default in; the reverse (new campaigns silently syncing until
    someone remembers to turn them off) is the one that actually causes
    the test-data-in-Asana problem this feature exists to prevent."""
    return {**DEFAULT_SETTINGS, **all_settings.get(campaign, {})}


def is_asana_sync_enabled(campaign: str, all_settings: dict) -> bool:
    return bool(get_campaign_settings(campaign, all_settings).get("asana_sync", False))


def build_updated_settings_yaml(all_settings: dict, campaign: str, asana_sync: bool) -> str:
    """Pure function — returns the FULL new file content after updating
    one campaign's asana_sync value, every other campaign's settings
    preserved untouched. Deliberately returns a string rather than writing
    a file directly: keeps this testable with no I/O at all, and keeps the
    actual commit mechanism (a GitHubClient commit, matching outreach.py's
    own Settings page) swappable later without touching this logic."""
    updated = {k: dict(v) for k, v in all_settings.items()}
    updated.setdefault(campaign, {})
    updated[campaign]["asana_sync"] = asana_sync
    return yaml.safe_dump(updated, sort_keys=True, default_flow_style=False)
