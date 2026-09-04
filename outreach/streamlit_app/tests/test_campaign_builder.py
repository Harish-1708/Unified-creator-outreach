import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
import outreach  # noqa: E402
from campaign_builder import (
    validate_campaign_name, validate_variant_content, build_template_file_content,
    build_campaign_files, get_next_stage_for_campaign, commit_message_for_campaign,
    confirmation_matches_campaign_name, list_campaign_files_to_delete,
    fetch_live_next_stage_for_campaign, fetch_live_campaign_files_to_delete,
)

TEMPLATES_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "templates")


def test_validate_campaign_name_rejects_blank():
    assert validate_campaign_name("", []) is not None
    assert validate_campaign_name("   ", []) is not None


def test_validate_campaign_name_rejects_special_characters():
    assert validate_campaign_name("Foo Bar", []) is not None
    assert validate_campaign_name("Foo-Bar", []) is not None
    assert validate_campaign_name("Foo/Bar", []) is not None


def test_validate_campaign_name_accepts_letters_numbers_underscores():
    assert validate_campaign_name("Foo_Bar_123", []) is None


def test_validate_campaign_name_rejects_duplicates():
    assert validate_campaign_name("Existing", ["Existing", "Other"]) is not None


def test_validate_variant_content_requires_subject_and_body():
    assert validate_variant_content("", "body") is not None
    assert validate_variant_content("subject", "") is not None
    assert validate_variant_content("subject", "body") is None


def test_validate_variant_content_first_stage_requires_subject_by_default():
    # is_first_stage defaults to True — the "create new campaign" (Intro)
    # flow's existing calls don't pass it explicitly, so this default
    # must keep requiring a subject there.
    assert validate_variant_content("", "body") is not None
    assert validate_variant_content("", "body", is_first_stage=True) is not None


def test_validate_variant_content_non_first_stage_allows_blank_subject():
    # Blank subject on a later stage is the deliberate "continue the
    # existing thread" convention — not a validation error.
    assert validate_variant_content("", "body", is_first_stage=False) is None


def test_validate_variant_content_non_first_stage_still_requires_body():
    assert validate_variant_content("", "", is_first_stage=False) is not None
    assert validate_variant_content("Subject", "", is_first_stage=False) is not None


def test_build_template_file_content_matches_outreach_expected_format():
    content = build_template_file_content("Hi {{FirstName}}", "Body text here")
    text = content.decode("utf-8")
    assert text.startswith("Subject: Hi {{FirstName}}\n\n")
    assert "Body text here" in text


def test_build_template_file_content_round_trips_with_outreach_load_template(tmp_path):
    # Prove the file this builds is actually readable by outreach.load_template,
    # not just "looks right" — this is the real compatibility contract.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    import outreach

    content = build_template_file_content("Quick idea for {{CompanyName}}", "Hi {{FirstName}},\n\nBody.")
    campaign_dir = tmp_path / "TestCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_bytes(content)

    tmpl = outreach.load_template(str(campaign_dir), "intro", "A")
    assert tmpl["subject"] == "Quick idea for {{CompanyName}}"
    assert "Hi {{FirstName}}," in tmpl["body"]


def test_build_template_file_content_blank_subject_round_trips_as_blank(tmp_path):
    # Proves the "leave Subject blank in the New Campaign UI" path produces
    # a file outreach.render_email correctly reads as "continue the
    # thread" — the same file format outreach.load_template already
    # parses a bare "Subject:" line as subject="".
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    import outreach

    content = build_template_file_content("", "Just following up, {{FirstName}}.")
    campaign_dir = tmp_path / "TestCampaign2"
    campaign_dir.mkdir()
    (campaign_dir / "followup1_A.txt").write_bytes(content)

    tmpl = outreach.load_template(str(campaign_dir), "followup1", "A")
    assert tmpl["subject"] == ""

    rendered = outreach.render_email(str(campaign_dir), "followup1", "A",
                                      {"FirstName": "Sam", "ThreadSubject": "Original"}, is_first_stage=False)
    assert rendered["subject"] == "Re: Original"
    assert rendered["is_continuation"] is True


def test_build_campaign_files_only_includes_provided_variants():
    files = build_campaign_files("Foo", "intro", {"A": {"subject": "S", "body": "B"}})
    assert len(files) == 1
    assert files[0]["path"] == "outreach/templates/Foo/intro_A.txt"


def test_build_campaign_files_multiple_variants_in_order():
    variants = {
        "A": {"subject": "SA", "body": "BA"},
        "B": {"subject": "SB", "body": "BB"},
    }
    files = build_campaign_files("Foo", "intro", variants)
    paths = [f["path"] for f in files]
    assert paths == ["outreach/templates/Foo/intro_A.txt", "outreach/templates/Foo/intro_B.txt"]


def test_build_campaign_files_uses_given_stage_prefix():
    files = build_campaign_files("Foo", "followup1", {"A": {"subject": "S", "body": "B"}})
    assert files[0]["path"] == "outreach/templates/Foo/followup1_A.txt"


# ---------- get_next_stage_for_campaign — against the REAL sample campaign ----------

def test_get_next_stage_for_fully_built_campaign_returns_none():
    # Kelson_Creators_Licensing already has all 5 stages in the repo fixture.
    result = get_next_stage_for_campaign("Kelson_Creators_Licensing", TEMPLATES_ROOT)
    assert result is None


def test_get_next_stage_for_partial_campaign_returns_next_stage_and_matching_variants(tmp_path):
    campaign_dir = tmp_path / "PartialCampaign"
    campaign_dir.mkdir()
    for letter in ["A", "B"]:
        (campaign_dir / f"intro_{letter}.txt").write_text(f"Subject: Hi {letter}\n\nBody {letter}")

    result = get_next_stage_for_campaign("PartialCampaign", str(tmp_path))
    assert result == ("followup1", ["A", "B"])


def test_get_next_stage_requires_exact_variant_match_downstream(tmp_path):
    # Sanity-check the underlying contract this function relies on: adding
    # followup1 with a MISMATCHED variant set is what outreach.py itself
    # would reject — get_next_stage_for_campaign's return value is exactly
    # what avoids ever attempting that combination in the first place.
    import outreach

    campaign_dir = tmp_path / "MismatchCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "intro_B.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "followup1_A.txt").write_text("Subject: Hi\n\nBody")  # missing B — mismatched

    with pytest.raises(outreach.ConfigError, match="Inconsistent variants"):
        outreach.discover_stages_and_variants(str(campaign_dir), stage_wait_days={})


def test_commit_message_for_new_campaign_mentions_campaign_variant_count_and_creator():
    msg = commit_message_for_campaign("MyCampaign", "intro", 2, "alice", is_new_campaign=True)
    assert "MyCampaign" in msg
    assert "2 Intro variant" in msg
    assert "alice" in msg


def test_commit_message_for_add_stage_mentions_stage_not_campaign_wording():
    msg = commit_message_for_campaign("MyCampaign", "followup1", 2, "alice", is_new_campaign=False)
    assert "followup1" in msg
    assert "MyCampaign" in msg
    assert "alice" in msg
    assert "Add campaign:" not in msg  # distinct wording from the new-campaign case


def test_commit_message_new_campaign_and_add_stage_are_distinguishable():
    new_msg = commit_message_for_campaign("Foo", "intro", 1, "bob", is_new_campaign=True)
    stage_msg = commit_message_for_campaign("Foo", "followup1", 1, "bob", is_new_campaign=False)
    assert new_msg != stage_msg


# ---------- confirmation_matches_campaign_name ----------

def test_confirmation_matches_exact_name():
    assert confirmation_matches_campaign_name("MyCampaign", "MyCampaign") is True


def test_confirmation_rejects_generic_word():
    assert confirmation_matches_campaign_name("DELETE", "MyCampaign") is False


def test_confirmation_rejects_partial_or_case_mismatch():
    assert confirmation_matches_campaign_name("mycampaign", "MyCampaign") is False
    assert confirmation_matches_campaign_name("MyCampaig", "MyCampaign") is False


def test_confirmation_rejects_empty_string():
    assert confirmation_matches_campaign_name("", "MyCampaign") is False


# ---------- list_campaign_files_to_delete ----------

def test_list_campaign_files_to_delete_includes_every_template_file(tmp_path):
    campaign_dir = tmp_path / "templates" / "Foo"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "intro_B.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nBody")
    (campaign_dir / "followup1_B.txt").write_text("Subject: \n\nBody")

    campaigns_dir = tmp_path / "config" / "campaigns"
    campaigns_dir.mkdir(parents=True)

    paths = list_campaign_files_to_delete("Foo", str(tmp_path / "templates"), str(campaigns_dir))
    assert set(paths) == {
        "outreach/templates/Foo/intro_A.txt", "outreach/templates/Foo/intro_B.txt",
        "outreach/templates/Foo/followup1_A.txt", "outreach/templates/Foo/followup1_B.txt",
    }


def test_list_campaign_files_to_delete_includes_override_file_when_present(tmp_path):
    campaign_dir = tmp_path / "templates" / "Foo"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")

    campaigns_dir = tmp_path / "config" / "campaigns"
    campaigns_dir.mkdir(parents=True)
    (campaigns_dir / "Foo.yaml").write_text("sending:\n  daily_limit: 50\n")

    paths = list_campaign_files_to_delete("Foo", str(tmp_path / "templates"), str(campaigns_dir))
    assert "outreach/config/campaigns/Foo.yaml" in paths


def test_list_campaign_files_to_delete_omits_override_file_when_absent(tmp_path):
    campaign_dir = tmp_path / "templates" / "Foo"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")

    campaigns_dir = tmp_path / "config" / "campaigns"
    campaigns_dir.mkdir(parents=True)

    paths = list_campaign_files_to_delete("Foo", str(tmp_path / "templates"), str(campaigns_dir))
    assert not any("campaigns/Foo.yaml" in p for p in paths)


def test_list_campaign_files_to_delete_never_touches_sheet_data():
    """Not a code assertion so much as a documentation check — this
    function's docstring must be explicit that Sheet data is untouched,
    since that's the one thing worth being extremely clear about."""
    assert "does NOT touch the Google Sheet" in list_campaign_files_to_delete.__doc__


def test_list_campaign_files_to_delete_ignores_non_txt_files(tmp_path):
    campaign_dir = tmp_path / "templates" / "Foo"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / ".DS_Store").write_text("junk")

    campaigns_dir = tmp_path / "config" / "campaigns"
    campaigns_dir.mkdir(parents=True)

    paths = list_campaign_files_to_delete("Foo", str(tmp_path / "templates"), str(campaigns_dir))
    assert paths == ["outreach/templates/Foo/intro_A.txt"]


# ---------- fetch_live_next_stage_for_campaign / fetch_live_campaign_files_to_delete ----------

class _FakeClient:
    def __init__(self, files=None, sha_map=None):
        self._files = files or []
        self._sha_map = sha_map or {}

    def list_directory_files(self, path):
        return self._files

    def get_file_sha(self, path):
        return self._sha_map.get(path)


def test_fetch_live_next_stage_uses_correct_github_path():
    captured = {}

    class _CapturingClient(_FakeClient):
        def list_directory_files(self, path):
            captured["path"] = path
            return ["intro_A.txt"]

    fetch_live_next_stage_for_campaign(_CapturingClient(), "DudeRobe")
    assert captured["path"] == "outreach/templates/DudeRobe"


def test_fetch_live_next_stage_matches_local_version():
    client = _FakeClient(files=["intro_A.txt"])
    prefix, variants = fetch_live_next_stage_for_campaign(client, "DudeRobe")
    assert prefix == "followup1"
    assert variants == ["A"]


def test_fetch_live_next_stage_returns_none_when_all_five_stages_exist():
    all_stage_files = []
    for prefix in outreach.CANONICAL_STAGE_ORDER:
        all_stage_files.append(f"{prefix}_A.txt")
    client = _FakeClient(files=all_stage_files)
    assert fetch_live_next_stage_for_campaign(client, "DudeRobe") is None


def test_fetch_live_campaign_files_to_delete_uses_correct_github_paths():
    client = _FakeClient(
        files=["intro_A.txt", "intro_B.txt"],
        sha_map={"outreach/config/campaigns/DudeRobe.yaml": "abc123"},
    )
    paths = fetch_live_campaign_files_to_delete(client, "DudeRobe")
    assert "outreach/templates/DudeRobe/intro_A.txt" in paths
    assert "outreach/templates/DudeRobe/intro_B.txt" in paths
    assert "outreach/config/campaigns/DudeRobe.yaml" in paths


def test_fetch_live_campaign_files_to_delete_omits_override_when_it_does_not_exist():
    client = _FakeClient(files=["intro_A.txt"], sha_map={})
    paths = fetch_live_campaign_files_to_delete(client, "DudeRobe")
    assert "outreach/config/campaigns/DudeRobe.yaml" not in paths


def test_fetch_live_campaign_files_to_delete_ignores_non_txt_files():
    client = _FakeClient(files=["intro_A.txt", "readme.md"])
    paths = fetch_live_campaign_files_to_delete(client, "DudeRobe")
    assert "outreach/templates/DudeRobe/readme.md" not in paths
