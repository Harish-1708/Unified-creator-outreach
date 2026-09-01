import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from responses_reply_logic import (
    find_lead_for_response, build_reply_defaults, build_reply_references,
    parse_email_list, validate_reply, build_reply_payload, reply_payload_path,
    build_attachment_entries, total_attachment_size_bytes, extract_email_address,
)


def _response(**overrides):
    r = {"ResponseID": "r1", "LeadID": "5", "From": "lead@abc.com", "Subject": "Re: Hi there",
         "MessageID": "<inbound1@mail.gmail.com>", "InReplyTo": "<our1@mail.gmail.com>"}
    r.update(overrides)
    return r


def _lead(**overrides):
    lead = {"LeadID": "5", "SenderAccount": "sales1", "ThreadReferences": "<our1@mail.gmail.com>"}
    lead.update(overrides)
    return lead


# ---------- find_lead_for_response ----------

def test_find_lead_for_response_matches_by_lead_id():
    leads = [_lead(LeadID="1"), _lead(LeadID="5"), _lead(LeadID="9")]
    found = find_lead_for_response(_response(LeadID="5"), leads)
    assert found["LeadID"] == "5"


def test_find_lead_for_response_no_match_returns_none():
    leads = [_lead(LeadID="1")]
    assert find_lead_for_response(_response(LeadID="999"), leads) is None


def test_find_lead_for_response_matches_across_str_int_mismatch():
    leads = [{"LeadID": 5, "SenderAccount": "sales1"}]  # int, not str
    found = find_lead_for_response(_response(LeadID="5"), leads)
    assert found is not None


# ---------- build_reply_defaults ----------

def test_reply_defaults_adds_re_prefix_when_missing():
    defaults = build_reply_defaults(_response(Subject="Hi there"), _lead())
    assert defaults["subject"] == "Re: Hi there"


def test_reply_defaults_does_not_double_prefix():
    defaults = build_reply_defaults(_response(Subject="Re: Hi there"), _lead())
    assert defaults["subject"] == "Re: Hi there"


def test_reply_defaults_case_insensitive_re_check():
    defaults = build_reply_defaults(_response(Subject="RE: Hi there"), _lead())
    assert defaults["subject"] == "RE: Hi there"  # already has it, case-insensitively


def test_reply_defaults_blank_subject_fallback():
    defaults = build_reply_defaults(_response(Subject=""), _lead())
    assert defaults["subject"] == "Re:"


def test_reply_defaults_to_from_response():
    defaults = build_reply_defaults(_response(From="someone@abc.com"), _lead())
    assert defaults["to"] == "someone@abc.com"


def test_reply_defaults_extracts_bare_address_from_display_name_format():
    """The actual reported bug: 'Rocky <harishdh16@gmail.com>' in the
    From field must pre-fill 'To' with just the address, not the whole
    display-name string, which is not a valid email address on its own
    and would fail validation as soon as the form loads."""
    defaults = build_reply_defaults(_response(From="Rocky <harishdh16@gmail.com>"), _lead())
    assert defaults["to"] == "harishdh16@gmail.com"


def test_extract_email_address_from_display_name_format():
    assert extract_email_address("Rocky <harishdh16@gmail.com>") == "harishdh16@gmail.com"


def test_extract_email_address_bare_address_unchanged():
    assert extract_email_address("harishdh16@gmail.com") == "harishdh16@gmail.com"


def test_extract_email_address_quoted_display_name():
    assert extract_email_address('"Rocky, D." <harishdh16@gmail.com>') == "harishdh16@gmail.com"


def test_extract_email_address_empty_string():
    assert extract_email_address("") == ""


def test_extract_email_address_none_input():
    assert extract_email_address(None) == ""


def test_reply_defaults_sender_account_from_lead():
    defaults = build_reply_defaults(_response(), _lead(SenderAccount="sales2"))
    assert defaults["sender_account"] == "sales2"


def test_reply_defaults_no_lead_found_blank_sender_account():
    defaults = build_reply_defaults(_response(), None)
    assert defaults["sender_account"] == ""


# ---------- build_reply_references ----------

def test_reply_references_chains_existing_and_inbound():
    refs = build_reply_references(_response(MessageID="<inbound1@mail.gmail.com>"),
                                   _lead(ThreadReferences="<our1@mail.gmail.com>"))
    assert refs == "<our1@mail.gmail.com> <inbound1@mail.gmail.com>"


def test_reply_references_no_lead_uses_only_inbound_id():
    refs = build_reply_references(_response(MessageID="<inbound1@mail.gmail.com>"), None)
    assert refs == "<inbound1@mail.gmail.com>"


def test_reply_references_no_inbound_id_uses_only_existing():
    refs = build_reply_references(_response(MessageID=""), _lead(ThreadReferences="<our1@mail.gmail.com>"))
    assert refs == "<our1@mail.gmail.com>"


def test_reply_references_both_blank_returns_empty_string():
    refs = build_reply_references(_response(MessageID=""), _lead(ThreadReferences=""))
    assert refs == ""


# ---------- parse_email_list ----------

def test_parse_email_list_comma_separated():
    assert parse_email_list("a@abc.com, b@abc.com") == ["a@abc.com", "b@abc.com"]


def test_parse_email_list_semicolon_separated():
    assert parse_email_list("a@abc.com; b@abc.com") == ["a@abc.com", "b@abc.com"]


def test_parse_email_list_mixed_whitespace():
    assert parse_email_list("  a@abc.com ,b@abc.com  ") == ["a@abc.com", "b@abc.com"]


def test_parse_email_list_empty_string():
    assert parse_email_list("") == []
    assert parse_email_list("   ") == []


def test_parse_email_list_drops_blank_entries():
    assert parse_email_list("a@abc.com,,b@abc.com,") == ["a@abc.com", "b@abc.com"]


def test_parse_email_list_single_address():
    assert parse_email_list("a@abc.com") == ["a@abc.com"]


# ---------- validate_reply ----------

def test_validate_reply_all_valid():
    assert validate_reply("lead@abc.com", "Thanks!", "sales1", [], []) == []


def test_validate_reply_rejects_missing_sender_account():
    errors = validate_reply("lead@abc.com", "Thanks!", "", [], [])
    assert any("sender account" in e.lower() for e in errors)


def test_validate_reply_rejects_invalid_to():
    errors = validate_reply("not-an-email", "Thanks!", "sales1", [], [])
    assert any("'To'" in e for e in errors)


def test_validate_reply_rejects_blank_body():
    errors = validate_reply("lead@abc.com", "   ", "sales1", [], [])
    assert any("Body is required" in e for e in errors)


def test_validate_reply_rejects_invalid_cc():
    errors = validate_reply("lead@abc.com", "Thanks!", "sales1", ["not-an-email"], [])
    assert any("Cc" in e for e in errors)


def test_validate_reply_rejects_invalid_bcc():
    errors = validate_reply("lead@abc.com", "Thanks!", "sales1", [], ["not-an-email"])
    assert any("Bcc" in e for e in errors)


def test_validate_reply_reports_multiple_errors_at_once():
    errors = validate_reply("", "", "", ["bad"], ["also-bad"])
    assert len(errors) == 5


def test_validate_reply_rejects_oversized_attachments():
    import outreach
    errors = validate_reply("lead@abc.com", "Thanks!", "sales1", [], [],
                             attachment_total_bytes=outreach.MAX_TOTAL_ATTACHMENT_BYTES + 1)
    assert any("MB" in e for e in errors)


def test_validate_reply_accepts_attachments_under_limit():
    errors = validate_reply("lead@abc.com", "Thanks!", "sales1", [], [], attachment_total_bytes=100)
    assert errors == []


# ---------- build_reply_payload ----------

def test_build_reply_payload_shape():
    payload = build_reply_payload(_response(), _lead(), "sales1", "lead@abc.com", "Re: Hi there", "Thanks!",
                                   ["cc@abc.com"], ["bcc@abc.com"])
    assert payload == {
        "sender_account": "sales1", "to": "lead@abc.com", "subject": "Re: Hi there", "body": "Thanks!",
        "in_reply_to": "<inbound1@mail.gmail.com>", "references": "<our1@mail.gmail.com> <inbound1@mail.gmail.com>",
        "cc": ["cc@abc.com"], "bcc": ["bcc@abc.com"], "lead_id": "5",
    }


def test_build_reply_payload_omits_attachments_key_when_none():
    payload = build_reply_payload(_response(), _lead(), "sales1", "lead@abc.com", "Re: Hi", "Thanks!", [], [])
    assert "attachments" not in payload


def test_build_reply_payload_includes_attachments_when_given():
    attachments = [{"filename": "photo.png", "content_base64": "ZmFrZQ=="}]
    payload = build_reply_payload(_response(), _lead(), "sales1", "lead@abc.com", "Re: Hi", "Thanks!", [], [],
                                   attachments=attachments)
    assert payload["attachments"] == attachments


def test_build_reply_payload_uses_provided_to_email_not_raw_from_field():
    """The other real bug: even a corrected 'To' field value was
    previously ignored — the payload always re-derived 'to' from the
    raw, unedited response['From'], silently discarding any edit."""
    payload = build_reply_payload(_response(From="Rocky <harishdh16@gmail.com>"), _lead(), "sales1",
                                   "harishdh16@gmail.com", "Re: Hi", "Thanks!", [], [])
    assert payload["to"] == "harishdh16@gmail.com"


def test_build_reply_payload_respects_a_fully_different_to_address():
    """The person can edit 'To' to reply to someone else entirely — that
    edit has to actually take effect, not just fix formatting."""
    payload = build_reply_payload(_response(From="original@abc.com"), _lead(), "sales1",
                                   "someone-else@abc.com", "Re: Hi", "Thanks!", [], [])
    assert payload["to"] == "someone-else@abc.com"


def test_build_reply_payload_is_json_serializable():
    payload = build_reply_payload(_response(), _lead(), "sales1", "lead@abc.com", "Re: Hi", "Thanks!", [], [])
    json.dumps(payload)  # raises if not serializable


def test_build_reply_payload_with_attachments_is_json_serializable():
    attachments = [{"filename": "photo.png", "content_base64": "ZmFrZQ=="}]
    payload = build_reply_payload(_response(), _lead(), "sales1", "lead@abc.com", "Re: Hi", "Thanks!", [], [],
                                   attachments=attachments)
    json.dumps(payload)


class _FakeUploadedFile:
    def __init__(self, name, content):
        self.name = name
        self._content = content

    def getvalue(self):
        return self._content


def test_build_attachment_entries_encodes_correctly():
    files = [_FakeUploadedFile("photo.png", b"fake-png-bytes")]
    entries = build_attachment_entries(files)
    assert len(entries) == 1
    assert entries[0]["filename"] == "photo.png"
    import base64
    assert base64.b64decode(entries[0]["content_base64"]) == b"fake-png-bytes"


def test_build_attachment_entries_multiple_files():
    files = [_FakeUploadedFile("a.png", b"aaa"), _FakeUploadedFile("b.png", b"bbb")]
    entries = build_attachment_entries(files)
    assert len(entries) == 2
    assert {e["filename"] for e in entries} == {"a.png", "b.png"}


def test_build_attachment_entries_empty_list():
    assert build_attachment_entries([]) == []


def test_total_attachment_size_bytes_sums_correctly():
    files = [_FakeUploadedFile("a.png", b"x" * 100), _FakeUploadedFile("b.png", b"x" * 50)]
    assert total_attachment_size_bytes(files) == 150


def test_total_attachment_size_bytes_empty_list():
    assert total_attachment_size_bytes([]) == 0


# ---------- reply_payload_path ----------

def test_reply_payload_path_format():
    path = reply_payload_path("DudeRobe", "r1", timestamp="2026-08-29-143012")
    assert path == "replies/DudeRobe/r1-2026-08-29-143012.json"


def test_reply_payload_path_sanitizes_slashes_in_response_id():
    path = reply_payload_path("DudeRobe", "weird/id", timestamp="2026-08-29-143012")
    assert "/" not in path.split("/", 2)[-1].replace("-2026-08-29-143012.json", "")
    assert path == "replies/DudeRobe/weird_id-2026-08-29-143012.json"
