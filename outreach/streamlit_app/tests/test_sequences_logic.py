import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import outreach  # noqa: E402
from sequences_logic import (  # noqa: E402
    get_existing_stages_and_variants, load_variant_content, next_available_variant_letter,
    build_variant_edit_file, build_new_variant_files_for_all_stages, validate_new_variant_contents,
    has_content_changed, can_delete_stage, build_stage_deletion_paths, can_delete_variant,
    build_variant_deletion_paths, fetch_live_stages_and_variants, fetch_live_template_content,
)

TEMPLATES_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "templates")


def test_get_existing_stages_and_variants_against_real_campaign():
    stages, variants = get_existing_stages_and_variants("Kelson_Creators_Licensing", TEMPLATES_ROOT)
    assert len(stages) == 5
    assert variants == ["A", "B", "C", "D"]


def test_load_variant_content_against_real_campaign():
    content = load_variant_content("Kelson_Creators_Licensing", "intro", "A", TEMPLATES_ROOT)
    assert content["subject"]
    assert content["body"]


# ---------- next_available_variant_letter ----------

def test_next_available_variant_letter_with_room():
    assert next_available_variant_letter(["A", "B"]) == "C"


def test_next_available_variant_letter_at_maximum():
    assert next_available_variant_letter(["A", "B", "C", "D"]) is None


def test_next_available_variant_letter_empty():
    assert next_available_variant_letter([]) == "A"


# ---------- build_variant_edit_file ----------

def test_build_variant_edit_file_format():
    f = build_variant_edit_file("Foo", "intro", "A", "Hi {{FirstName}}", "Body text")
    assert f["path"] == "outreach/templates/Foo/intro_A.txt"
    assert f["content"] == b"Subject: Hi {{FirstName}}\n\nBody text\n"


def test_build_variant_edit_file_round_trips_with_outreach_load_template(tmp_path):
    import outreach
    f = build_variant_edit_file("Foo", "followup1", "B", "New subject", "New body")
    campaign_dir = tmp_path / "Foo"
    campaign_dir.mkdir()
    (campaign_dir / "followup1_B.txt").write_bytes(f["content"])
    tmpl = outreach.load_template(str(campaign_dir), "followup1", "B")
    assert tmpl["subject"] == "New subject"
    assert "New body" in tmpl["body"]


# ---------- build_new_variant_files_for_all_stages ----------

def test_new_variant_files_cover_every_stage():
    stages = [
        {"name": "intro", "template_prefix": "intro"},
        {"name": "followup1", "template_prefix": "followup1"},
    ]
    contents = {
        "intro": {"subject": "Intro E subject", "body": "Intro E body"},
        "followup1": {"subject": "", "body": "Followup1 E continues the thread"},
    }
    files = build_new_variant_files_for_all_stages("Foo", stages, "E", contents)
    assert len(files) == 2
    paths = {f["path"] for f in files}
    assert paths == {"outreach/templates/Foo/intro_E.txt", "outreach/templates/Foo/followup1_E.txt"}


def test_new_variant_files_preserves_blank_subject_for_continuation():
    stages = [{"name": "followup1", "template_prefix": "followup1"}]
    contents = {"followup1": {"subject": "", "body": "Continuing the thread"}}
    files = build_new_variant_files_for_all_stages("Foo", stages, "C", contents)
    assert files[0]["content"].startswith(b"Subject: \n\n")


# ---------- validate_new_variant_contents ----------

def test_validate_new_variant_all_valid():
    stages = [{"name": "intro", "template_prefix": "intro"}, {"name": "followup1", "template_prefix": "followup1"}]
    contents = {
        "intro": {"subject": "Real subject", "body": "Body"},
        "followup1": {"subject": "", "body": "Body"},  # blank subject OK — not first stage
    }
    assert validate_new_variant_contents(stages, contents) == []


def test_validate_new_variant_first_stage_requires_subject():
    stages = [{"name": "intro", "template_prefix": "intro"}]
    contents = {"intro": {"subject": "", "body": "Body"}}
    errors = validate_new_variant_contents(stages, contents)
    assert len(errors) == 1
    assert "Subject is required" in errors[0]


def test_validate_new_variant_every_stage_needs_body():
    stages = [{"name": "intro", "template_prefix": "intro"}, {"name": "followup1", "template_prefix": "followup1"}]
    contents = {
        "intro": {"subject": "Subject", "body": ""},
        "followup1": {"subject": "", "body": ""},
    }
    errors = validate_new_variant_contents(stages, contents)
    assert len(errors) == 2


def test_validate_new_variant_missing_stage_entry_treated_as_empty():
    stages = [{"name": "intro", "template_prefix": "intro"}]
    errors = validate_new_variant_contents(stages, {})  # no entry for "intro" at all
    assert len(errors) == 2  # both subject and body missing


# ---------- has_content_changed ----------

def test_has_content_changed_true_when_subject_differs():
    original = {"subject": "Old", "body": "Same"}
    assert has_content_changed(original, "New", "Same") is True


def test_has_content_changed_true_when_body_differs():
    original = {"subject": "Same", "body": "Old"}
    assert has_content_changed(original, "Same", "New") is True


def test_has_content_changed_false_when_identical():
    original = {"subject": "Same", "body": "Same"}
    assert has_content_changed(original, "Same", "Same") is False


def test_has_content_changed_strips_whitespace_before_comparing():
    original = {"subject": "Same", "body": "Same"}
    assert has_content_changed(original, "Same  ", "  Same") is False


# ---------- can_delete_stage ----------

def _stage(prefix, name=None):
    return {"template_prefix": prefix, "name": name or prefix}


def test_can_delete_stage_allows_the_last_stage():
    stages = [_stage("intro"), _stage("followup1"), _stage("followup2")]
    ok, msg = can_delete_stage(stages, "followup2")
    assert ok is True
    assert msg == ""


def test_can_delete_stage_rejects_a_middle_stage():
    stages = [_stage("intro"), _stage("followup1"), _stage("followup2")]
    ok, msg = can_delete_stage(stages, "followup1")
    assert ok is False
    assert "contiguous" in msg


def test_can_delete_stage_rejects_deleting_intro_when_it_is_the_only_stage():
    stages = [_stage("intro")]
    ok, msg = can_delete_stage(stages, "intro")
    assert ok is False
    assert "at least one stage" in msg


def test_can_delete_stage_rejects_intro_even_when_named_as_last():
    """intro is technically stages[-1] when it's the only stage — the
    'must have at least one stage' check must be checked BEFORE the
    'is this the last stage' check, not after."""
    stages = [_stage("intro")]
    ok, msg = can_delete_stage(stages, "intro")
    assert ok is False


def test_build_stage_deletion_paths_one_per_variant():
    paths = build_stage_deletion_paths("Foo", "followup2", ["A", "B"])
    assert paths == ["outreach/templates/Foo/followup2_A.txt", "outreach/templates/Foo/followup2_B.txt"]


def test_build_stage_deletion_paths_single_variant():
    paths = build_stage_deletion_paths("Foo", "intro", ["A"])
    assert paths == ["outreach/templates/Foo/intro_A.txt"]


# ---------- can_delete_variant ----------

def test_can_delete_variant_allows_when_multiple_exist():
    ok, msg = can_delete_variant(["A", "B", "C"], "B")
    assert ok is True
    assert msg == ""


def test_can_delete_variant_rejects_the_only_remaining_variant():
    ok, msg = can_delete_variant(["A"], "A")
    assert ok is False
    assert "only remaining variant" in msg


def test_can_delete_variant_rejects_nonexistent_variant():
    ok, msg = can_delete_variant(["A", "B"], "D")
    assert ok is False
    assert "doesn't exist" in msg


def test_build_variant_deletion_paths_one_per_stage():
    stages = [_stage("intro"), _stage("followup1"), _stage("followup2")]
    paths = build_variant_deletion_paths("Foo", stages, "B")
    assert paths == [
        "outreach/templates/Foo/intro_B.txt",
        "outreach/templates/Foo/followup1_B.txt",
        "outreach/templates/Foo/followup2_B.txt",
    ]


def test_build_variant_deletion_paths_single_stage():
    stages = [_stage("intro")]
    paths = build_variant_deletion_paths("Foo", stages, "A")
    assert paths == ["outreach/templates/Foo/intro_A.txt"]


# ---------- fetch_live_stages_and_variants / fetch_live_template_content ----------
# The actual fix for the real corruption bug: these read from GitHub's API
# live, never from the local checkout that can lag behind a recent commit.

class _FakeClient:
    def __init__(self, files=None, contents=None):
        self._files = files or []
        self._contents = contents or {}

    def list_directory_files(self, path):
        return self._files

    def get_file_content(self, path):
        """Matches the real contract: bytes, raises when missing —
        never returns None."""
        if path not in self._contents:
            raise KeyError(f"no fake content registered for {path!r}")
        return self._contents[path]


def test_fetch_live_stages_and_variants_uses_the_correct_github_path():
    """The exact bug this whole fix exists for: the path must include the
    'outreach/' prefix — templates/ lives at outreach/templates/ in this
    repo, not at the repo root."""
    captured = {}

    class _CapturingClient(_FakeClient):
        def list_directory_files(self, path):
            captured["path"] = path
            return ["intro_A.txt"]

    fetch_live_stages_and_variants(_CapturingClient(), "DudeRobe")
    assert captured["path"] == "outreach/templates/DudeRobe"


def test_fetch_live_stages_and_variants_matches_local_version_for_the_same_files():
    """Live and local reads must agree when given the same underlying
    data — this is a different data SOURCE, not different logic."""
    client = _FakeClient(files=["intro_A.txt", "intro_B.txt", "followup1_A.txt", "followup1_B.txt"])
    live_stages, live_variants = fetch_live_stages_and_variants(client, "DudeRobe")
    assert [s["template_prefix"] for s in live_stages] == ["intro", "followup1"]
    assert live_variants == ["A", "B"]


def test_fetch_live_stages_and_variants_catches_inconsistency_from_live_data():
    """The actual real-world scenario: a live listing showing an
    inconsistent state (e.g. a Delete Stage action that landed between
    two reads) must raise, exactly like the local version does."""
    client = _FakeClient(files=["intro_A.txt", "intro_B.txt", "followup1_A.txt"])  # missing followup1_B
    with pytest.raises(outreach.ConfigError, match="missing variant"):
        fetch_live_stages_and_variants(client, "DudeRobe")


def test_fetch_live_template_content_uses_the_correct_github_path():
    captured = {}

    class _CapturingClient(_FakeClient):
        def get_file_content(self, path):
            captured["path"] = path
            return b"Subject: Hi\n\nBody"

    fetch_live_template_content(_CapturingClient(), "DudeRobe", "intro", "A")
    assert captured["path"] == "outreach/templates/DudeRobe/intro_A.txt"


def test_fetch_live_template_content_parses_correctly():
    client = _FakeClient(contents={"outreach/templates/DudeRobe/intro_A.txt": b"Subject: Hello\n\nBody text"})
    result = fetch_live_template_content(client, "DudeRobe", "intro", "A")
    assert result == {"subject": "Hello", "body": "Body text"}


def test_fetch_live_template_content_raises_clearly_when_missing():
    """get_file_content raises (never returns None) when the file
    doesn't exist — a template that's supposed to exist not being found
    is a genuine error, propagated rather than swallowed."""
    client = _FakeClient(contents={})
    with pytest.raises(KeyError):
        fetch_live_template_content(client, "DudeRobe", "intro", "A")
