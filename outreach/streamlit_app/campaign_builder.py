"""Campaign creation/extension logic. Kept pure/testable: nothing here
touches the network. The Streamlit page collects form input, calls these
functions, and hands the result to GitHubClient.
commit_campaign_files_directly.

Commits DIRECTLY to main — no branch, no PR. Creating a new campaign (or
adding a stage) used to require going to GitHub and merging a pull
request; that's exactly the trip this now skips. The remaining safety net
is the in-app confirmation the Streamlit page requires before calling
this, plus the fact that only a logged-in user of this app can reach it.

Two modes, both funneling through the same file-building logic:
- A brand NEW campaign — starts at "intro", any 1-4 variant letters.
- The NEXT stage on an EXISTING campaign — the stage after whatever it
  already has, and (unlike a new campaign) the variant letters are NOT a
  free choice: they must exactly match the campaign's existing variants,
  because that's what outreach.discover_stages_and_variants requires (see
  its docstring in outreach.py). get_next_stage_for_campaign reuses that
  exact function rather than reimplementing the rule, so this can never
  produce a combination the core system would reject.
"""
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

import yaml

CAMPAIGN_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
VARIANT_LETTERS = ["A", "B", "C", "D"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402


def validate_campaign_name(name: str, existing_campaigns: List[str]) -> Optional[str]:
    """Returns an error message, or None if the name is valid."""
    if not name or not name.strip():
        return "Campaign name is required."
    if not CAMPAIGN_NAME_RE.match(name):
        return "Use only letters, numbers, and underscores — this becomes a folder name."
    if name in existing_campaigns:
        return f"A campaign named '{name}' already exists."
    return None


def validate_variant_content(subject: str, body: str, is_first_stage: bool = True) -> Optional[str]:
    """Subject is required for the FIRST stage only — outreach.py's own
    render_email raises if a first-stage template has a blank Subject
    (there's no previous thread to continue from a first message). For any
    later stage, a blank Subject is a legitimate, deliberate choice: it
    means "continue the existing thread" (Re: <previous subject>) instead
    of starting a new one — see render_email's docstring in outreach.py."""
    if is_first_stage and (not subject or not subject.strip()):
        return "Subject is required for the first stage (there's no previous thread to continue from)."
    if not body or not body.strip():
        return "Body is required."
    return None


def build_campaign_duplication_files(source_files: Dict[str, bytes], source_override: Optional[Dict],
                                      new_campaign_name: str) -> List[Dict]:
    """Pure — takes ALREADY-FETCHED source content (never reads a
    filesystem or calls GitHub itself) and returns the file list to
    commit for the new, duplicated campaign.

    Raises ValueError if source_files is empty — a duplicate is NEVER
    silently created with zero template files. This is the actual fix
    for the real bug: the original version of this feature read the
    source campaign from a local git checkout that could lag behind a
    very recent commit, and had no check for an empty result — it
    proceeded straight to a "success" message with an empty campaign
    folder. Fetching the source live (see fetch_campaign_files_for_duplication
    in sequences_logic.py) and validating non-empty HERE, in a pure
    function with no I/O of its own, means this check can never be
    accidentally skipped by a future caller.

    The new campaign's status is ALWAYS forced to "draft", regardless of
    the source's status — a duplicate should never come into existence
    already running, silently sending, before anyone has reviewed it.
    Every other override key (sending limits, schedule, Asana settings)
    is copied as-is; only 'status' is overridden.
    """
    if not source_files:
        raise ValueError(
            "The source campaign's file list came back empty — refusing to create a duplicate with "
            "nothing in it. This usually means the source campaign name is wrong, or GitHub's API "
            "briefly failed; it should NOT happen for a real, existing campaign."
        )

    files = []
    for filename, content_bytes in source_files.items():
        files.append({"path": f"outreach/templates/{new_campaign_name}/{filename}", "content": content_bytes})

    new_override = dict(source_override or {})
    new_override["status"] = "draft"
    files.append({
        "path": f"outreach/config/campaigns/{new_campaign_name}.yaml",
        "content": yaml.safe_dump(new_override, sort_keys=False, default_flow_style=False).encode("utf-8"),
    })

    return files


def build_template_file_content(subject: str, body: str) -> bytes:
    """Matches outreach.load_template's expected format exactly: a
    'Subject: ...' first line, a blank line, then the body."""
    return f"Subject: {subject.strip()}\n\n{body.strip()}\n".encode("utf-8")


def build_campaign_files(campaign_name: str, stage_prefix: str,
                          variants: Dict[str, Dict[str, str]]) -> List[Dict]:
    """variants: {'A': {'subject': ..., 'body': ...}, 'B': {...}, ...}.
    Returns the [{'path':..., 'content': bytes}] list GitHubClient.
    open_campaign_pull_request expects, for ONE stage of a campaign
    (stage_prefix is e.g. 'intro' or 'followup1')."""
    files = []
    for letter in VARIANT_LETTERS:
        if letter not in variants:
            continue
        content = build_template_file_content(variants[letter]["subject"], variants[letter]["body"])
        files.append({"path": f"outreach/templates/{campaign_name}/{stage_prefix}_{letter}.txt", "content": content})
    return files


def get_next_stage_for_campaign(campaign_name: str, templates_root: str) -> Optional[Tuple[str, List[str]]]:
    """For an EXISTING campaign, returns (next_stage_prefix,
    required_variant_letters) — the only stage/variant combination
    outreach.py's own auto-discovery would accept next — or None if the
    campaign already has all 5 stages built out.

    Reuses outreach.discover_stages_and_variants directly rather than
    re-deriving the rule, so this can never drift from what the core
    system actually enforces.
    """
    campaign_dir = os.path.join(templates_root, campaign_name)
    stages, variants = outreach.discover_stages_and_variants(campaign_dir, stage_wait_days={})
    existing_prefixes = [s["template_prefix"] for s in stages]
    for prefix in outreach.CANONICAL_STAGE_ORDER:
        if prefix not in existing_prefixes:
            return prefix, variants
    return None


def fetch_live_next_stage_for_campaign(client, campaign_name: str) -> Optional[Tuple[str, List[str]]]:
    """LIVE equivalent of get_next_stage_for_campaign — fetches the
    directory listing from GitHub's API instead of the local checkout.
    Suggesting the wrong next stage from a stale read (e.g. after a very
    recent Delete Stage that this page hasn't caught up with yet) risks
    creating a gap the same class of bug that corrupted a real campaign —
    see fetch_live_stages_and_variants's own docstring."""
    filenames = client.list_directory_files(f"outreach/templates/{campaign_name}")
    stages, variants = outreach.parse_stages_and_variants_from_filenames(
        filenames, stage_wait_days={}, source_description=f"campaign '{campaign_name}' (live from GitHub)")
    existing_prefixes = [s["template_prefix"] for s in stages]
    for prefix in outreach.CANONICAL_STAGE_ORDER:
        if prefix not in existing_prefixes:
            return prefix, variants
    return None


def commit_message_for_campaign(campaign_name: str, stage_prefix: str, variant_count: int,
                                 created_by: str, is_new_campaign: bool) -> str:
    """Commit message for the direct-to-main commit — see
    github_client.commit_campaign_files_directly. No branch/PR involved:
    this campaign (or stage) is live the moment this commit lands."""
    if is_new_campaign:
        return (
            f"Add campaign: {campaign_name} ({variant_count} Intro variant(s), "
            f"via Streamlit control panel by {created_by})"
        )
    return (
        f"Add {stage_prefix} to campaign: {campaign_name} ({variant_count} variant(s), "
        f"via Streamlit control panel by {created_by})"
    )


def confirmation_matches_campaign_name(typed_text: str, campaign_name: str) -> bool:
    """Exact match required — same deliberate friction as typing SEND,
    but here it's the campaign's own name, so a misclick or a generic
    confirm word can't accidentally delete the wrong campaign."""
    return typed_text == campaign_name


def list_campaign_files_to_delete(campaign_name: str, templates_root: str, campaigns_dir: str) -> List[str]:
    """Every repo file that makes this campaign exist at all — every
    template file (every stage x every variant) plus its config override
    file, if one was ever created. Returns repo-relative paths, ready for
    GitHubClient.delete_file, one call per path.

    Deliberately does NOT touch the Google Sheet — leads, sends, and
    replies all stay exactly where they are, still readable directly in
    the Sheet, even after the campaign's templates are gone. Only the
    files that make outreach.py's own auto-discovery recognize this as a
    campaign are removed; that's what "delete" means here, same
    soft-removal spirit as remove_leads never hard-deleting a lead row."""
    campaign_template_dir = os.path.join(templates_root, campaign_name)
    paths = []
    if os.path.isdir(campaign_template_dir):
        for filename in sorted(os.listdir(campaign_template_dir)):
            if filename.endswith(".txt"):
                paths.append(f"outreach/templates/{campaign_name}/{filename}")

    override_path = os.path.join(campaigns_dir, f"{campaign_name}.yaml")
    if os.path.isfile(override_path):
        paths.append(f"outreach/config/campaigns/{campaign_name}.yaml")

    return paths


def fetch_live_campaign_files_to_delete(client, campaign_name: str) -> List[str]:
    """LIVE equivalent of list_campaign_files_to_delete — fetches the
    directory listing and checks for the override file straight from
    GitHub's API instead of the local checkout. A stale read here risks
    MISSING a very recently added file (e.g. a variant added moments
    ago) from the deletion list, leaving it orphaned behind after the
    campaign is otherwise "deleted" — same staleness-during-a-narrow-
    window risk as everywhere else in this fix."""
    paths = []
    for filename in client.list_directory_files(f"outreach/templates/{campaign_name}"):
        if filename.endswith(".txt"):
            paths.append(f"outreach/templates/{campaign_name}/{filename}")

    override_path = f"outreach/config/campaigns/{campaign_name}.yaml"
    if client.get_file_sha(override_path) is not None:
        paths.append(override_path)

    return paths
