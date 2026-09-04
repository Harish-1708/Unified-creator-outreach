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


# ---------- Brand / Campaign registry (explicit creation, not just what's
# already been run) ----------
#
# A brand and a campaign have never been stored entities anywhere in this
# pipeline before now — a discovery run just stamps whatever brand_name/
# campaign string was typed into it onto Run Log, after the fact. That's
# fine for browsing history, but it means there was no way to "create" a
# campaign ahead of ever running discovery for it. This registry — a
# brand_name field added to each campaign's settings entry, plus a
# separate small file for brands with zero campaigns yet — makes creation
# a real, immediately-visible action, reusing the exact same file and
# commit mechanism the Asana toggle already established.

def load_brands(path: str = "config/brands.yaml") -> list:
    """Returns [] if the file doesn't exist yet — no brand has ever been
    explicitly added via +Add Brand. Brands implied by an existing
    campaign's brand_name, or by Run Log history, still show up via
    list_all_brands() below even with this file totally empty."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return list(data) if data else []
    except FileNotFoundError:
        return []


def build_updated_brands_yaml(brands: list, new_brand: str) -> str:
    """Pure — adds new_brand if it isn't already present (exact match;
    brand names aren't case-normalized, matching how Campaign names
    already work elsewhere in this pipeline), sorted for a stable diff."""
    updated = set(brands)
    updated.add(new_brand)
    return yaml.safe_dump(sorted(updated), sort_keys=False, default_flow_style=False)


def build_new_campaign_yaml(all_settings: dict, campaign: str, brand_name: str) -> str:
    """Adds a new campaign entry under the given brand, asana_sync
    defaulting to False (same safety default as everywhere else in this
    file). If the campaign name already exists under a DIFFERENT brand,
    raises ValueError instead of silently reassigning it — campaign names
    are meant to be unique regardless of brand, and a silent
    reassignment would be a confusing, hard-to-notice way to corrupt
    that."""
    updated = {k: dict(v) for k, v in all_settings.items()}
    existing = updated.get(campaign)
    if existing and existing.get("brand_name") and existing["brand_name"] != brand_name:
        raise ValueError(
            f"Campaign '{campaign}' already exists under brand '{existing['brand_name']}' — "
            f"campaign names must be unique across brands."
        )
    updated.setdefault(campaign, {})
    updated[campaign]["brand_name"] = brand_name
    updated[campaign].setdefault("asana_sync", False)
    return yaml.safe_dump(updated, sort_keys=True, default_flow_style=False)


def list_all_brands(registry_brands: list, run_log_brand_names: list, all_settings: dict) -> list:
    """Union of three sources: brands explicitly added via +Add Brand
    (registry_brands, zero campaigns is fine), brands implied by an
    explicitly-created campaign's brand_name (all_settings), and brands
    that show up in Run Log history — a real discovery run happened under
    them even if nobody went through +Add Brand first (true for every
    campaign that existed before this registry did)."""
    brands = set(registry_brands)
    brands.update(run_log_brand_names)
    for entry in all_settings.values():
        if entry.get("brand_name"):
            brands.add(entry["brand_name"])
    return sorted(brands)


def list_all_campaigns_for_brand(brand: str, run_log_campaigns_for_brand: list, all_settings: dict) -> list:
    """Same union reasoning as list_all_brands, scoped to one brand:
    campaigns explicitly created under it, plus ones with real run
    history under it."""
    campaigns = set(run_log_campaigns_for_brand)
    for campaign_name, entry in all_settings.items():
        if entry.get("brand_name") == brand:
            campaigns.add(campaign_name)
    return sorted(campaigns)
