import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sequences_logic import (
    get_existing_stages_and_variants, load_variant_content, next_available_variant_letter,
    build_variant_edit_file, build_new_variant_files_for_all_stages, validate_new_variant_contents,
    has_content_changed, can_delete_stage, build_stage_deletion_paths, can_delete_variant,
    build_variant_deletion_paths,
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
    assert f["path"] == "templates/Foo/intro_A.txt"
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
    assert paths == {"templates/Foo/intro_E.txt", "templates/Foo/followup1_E.txt"}


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
    assert paths == ["templates/Foo/followup2_A.txt", "templates/Foo/followup2_B.txt"]


def test_build_stage_deletion_paths_single_variant():
    paths = build_stage_deletion_paths("Foo", "intro", ["A"])
    assert paths == ["templates/Foo/intro_A.txt"]


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
        "templates/Foo/intro_B.txt",
        "templates/Foo/followup1_B.txt",
        "templates/Foo/followup2_B.txt",
    ]


def test_build_variant_deletion_paths_single_stage():
    stages = [_stage("intro")]
    paths = build_variant_deletion_paths("Foo", stages, "A")
    assert paths == ["templates/Foo/intro_A.txt"]
