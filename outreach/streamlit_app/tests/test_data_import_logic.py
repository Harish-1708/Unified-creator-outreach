import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_import_logic import (
    parse_csv_bytes, build_default_mapping, apply_mapping, validate_mapping,
    count_valid_rows, build_import_payload, import_payload_path,
    build_removal_payload, removal_payload_path, payload_to_bytes,
    filter_leads, search_leads, validate_custom_field_name, find_duplicate_columns,
    build_full_lead_table,
    FILTER_ALL, FILTER_PENDING_APPROVAL, FILTER_IN_PROGRESS, FILTER_REPLIED,
    FILTER_BOUNCED, FILTER_REMOVED,
)


# ---------- parse_csv_bytes ----------

def test_parse_csv_bytes_basic():
    raw = b"First Name,Email,Company\nSam,sam@abc.com,Acme\nAlex,alex@abc.com,Beta\n"
    columns, rows = parse_csv_bytes(raw)
    assert columns == ["First Name", "Email", "Company"]
    assert len(rows) == 2
    assert rows[0]["Email"] == "sam@abc.com"


def test_parse_csv_bytes_handles_bom():
    raw = "\ufeffFirst Name,Email\nSam,sam@abc.com\n".encode("utf-8")
    columns, rows = parse_csv_bytes(raw)
    assert columns == ["First Name", "Email"]  # BOM stripped, not glued to first header


def test_parse_csv_bytes_empty_file():
    columns, rows = parse_csv_bytes(b"")
    assert columns == []
    assert rows == []


# ---------- build_default_mapping ----------

def test_default_mapping_matches_known_fields_case_and_punctuation_insensitive():
    mapping = build_default_mapping(["First Name", "Last-Name", "EMAIL", "company"], [])
    assert mapping["First Name"] == "FirstName"
    assert mapping["Last-Name"] == "LastName"
    assert mapping["EMAIL"] == "Email"
    assert mapping["company"] == "Company"


def test_default_mapping_matches_custom_columns():
    mapping = build_default_mapping(["Job Title", "Website"], ["Title", "Website"])
    assert mapping["Website"] == "Website"
    # "Job Title" doesn't normalize-match "Title" (extra word) — defaults to a
    # new custom field using its own name, not Skip (zero-click default).
    assert mapping["Job Title"] == "Job Title"


def test_default_mapping_unrecognized_columns_default_to_new_custom_field_named_after_themselves():
    """The zero-click-default fix: bringing in every column should take
    zero clicks for the common case, not one 'Skip' per unmatched
    column."""
    mapping = build_default_mapping(["Random Column"], [])
    assert mapping["Random Column"] == "Random Column"


def test_default_mapping_skips_when_own_name_collides_with_reserved_column():
    """The one deliberate exception: a column whose own name IS a
    reserved system column must default to Skip, not auto-map to
    itself — mapping a CSV's own 'Status' column onto the system's
    tracked Status column would silently corrupt real send-tracking
    data on the next import."""
    mapping = build_default_mapping(["Status", "Client"], [], reserved_names=["Status", "IntroSentAt"])
    assert mapping["Status"] == ""
    assert mapping["Client"] == "Client"  # unaffected — doesn't collide


def test_default_mapping_prefers_known_field_over_same_named_custom_column():
    mapping = build_default_mapping(["Email"], ["Email"])
    assert mapping["Email"] == "Email"  # still correct either way, but exercises the precedence path


# ---------- validate_custom_field_name ----------

def test_validate_custom_field_name_rejects_blank():
    assert validate_custom_field_name("", ["Status"]) is not None
    assert validate_custom_field_name("   ", ["Status"]) is not None


def test_validate_custom_field_name_rejects_reserved_name_case_insensitively():
    assert validate_custom_field_name("status", ["Status", "IntroSentAt"]) is not None
    assert validate_custom_field_name("STATUS", ["Status"]) is not None


def test_validate_custom_field_name_accepts_a_genuinely_new_name():
    assert validate_custom_field_name("Client", ["Status", "IntroSentAt"]) is None


# ---------- find_duplicate_columns ----------

def test_find_duplicate_columns_detects_repeated_name():
    assert find_duplicate_columns(["Email", "Last Contact Date", "Last Contact Date"]) == ["Last Contact Date"]


def test_find_duplicate_columns_empty_when_all_unique():
    assert find_duplicate_columns(["Email", "FirstName", "LastName"]) == []


def test_find_duplicate_columns_detects_multiple_distinct_duplicates():
    result = find_duplicate_columns(["A", "A", "B", "B", "C"])
    assert result == ["A", "B"]


# ---------- build_full_lead_table ----------

def test_build_full_lead_table_covers_every_field_across_all_leads():
    """The actual fix: a custom column present on SOME leads but not
    others must still appear as a column — with blanks, not be limited
    to a fixed hardcoded subset."""
    leads = [{"Email": "a@abc.com", "Client": "DudeRobe"}, {"Email": "b@abc.com", "Product": "Robe"}]
    table = build_full_lead_table(leads)
    assert set(table.keys()) == {"Email", "Client", "Product"}
    assert table["Client"] == ["DudeRobe", ""]
    assert table["Product"] == ["", "Robe"]


def test_build_full_lead_table_excludes_internal_row_field():
    leads = [{"Email": "a@abc.com", "_row": 2}]
    table = build_full_lead_table(leads)
    assert "_row" not in table


def test_build_full_lead_table_follows_given_header_order():
    leads = [{"Email": "a@abc.com", "Client": "DudeRobe", "FirstName": "Sam"}]
    table = build_full_lead_table(leads, header_order=["LeadID", "FirstName", "Email", "Client"])
    assert list(table.keys()) == ["FirstName", "Email", "Client"]  # LeadID absent from data, correctly omitted


def test_build_full_lead_table_falls_back_to_alphabetical_for_fields_not_in_header_order():
    leads = [{"Email": "a@abc.com", "Zebra": "z", "Apple": "a"}]
    table = build_full_lead_table(leads, header_order=["Email"])
    keys_after_email = list(table.keys())[1:]
    assert keys_after_email == ["Apple", "Zebra"]  # alphabetical, not CSV/dict order


# ---------- apply_mapping ----------

def test_apply_mapping_maps_and_strips_whitespace():
    rows = [{"First Name": "  Sam  ", "Email": "sam@abc.com "}]
    mapping = {"First Name": "FirstName", "Email": "Email"}
    mapped = apply_mapping(rows, mapping)
    assert mapped == [{"FirstName": "Sam", "Email": "sam@abc.com"}]


def test_apply_mapping_drops_unmapped_columns():
    rows = [{"First Name": "Sam", "Junk": "ignore me"}]
    mapping = {"First Name": "FirstName", "Junk": ""}
    mapped = apply_mapping(rows, mapping)
    assert mapped == [{"FirstName": "Sam"}]


def test_apply_mapping_empty_rows():
    assert apply_mapping([], {"Email": "Email"}) == []


# ---------- validate_mapping ----------

def test_validate_mapping_requires_email():
    assert validate_mapping({"First Name": "FirstName"}) is not None
    assert validate_mapping({"E-mail": "Email"}) is None


# ---------- count_valid_rows ----------

def test_count_valid_rows_only_counts_rows_with_email():
    mapped = [{"Email": "a@abc.com"}, {"FirstName": "NoEmail"}, {"Email": ""}]
    assert count_valid_rows(mapped) == 1


# ---------- payload builders ----------

def test_build_import_payload_shape():
    payload = build_import_payload([{"Email": "a@abc.com"}])
    assert payload == {"leads": [{"Email": "a@abc.com"}]}


def test_import_payload_path_format():
    path = import_payload_path("DudeRobe", timestamp="2026-08-29-143012")
    assert path == "outreach/imports/DudeRobe/2026-08-29-143012.json"


def test_build_removal_payload_stringifies_ids():
    payload = build_removal_payload([5, "8"])
    assert payload == {"lead_ids": ["5", "8"]}


def test_removal_payload_path_format():
    path = removal_payload_path("DudeRobe", timestamp="2026-08-29-143012")
    assert path == "outreach/removals/DudeRobe/2026-08-29-143012.json"


def test_payload_to_bytes_round_trips_as_valid_json():
    payload = {"leads": [{"Email": "a@abc.com"}]}
    raw = payload_to_bytes(payload)
    assert json.loads(raw.decode("utf-8")) == payload


# ---------- filter_leads ----------

def _lead(**overrides):
    lead = {"FirstName": "Sam", "LastName": "Lee", "Email": "sam@abc.com", "Company": "Acme",
            "Approval": "Yes", "Status": ""}
    lead.update(overrides)
    return lead


def test_filter_all_returns_everything():
    leads = [_lead(), _lead(Approval="")]
    assert filter_leads(leads, FILTER_ALL) == leads


def test_filter_pending_approval():
    leads = [_lead(Approval="Yes"), _lead(Approval=""), _lead(Approval="No")]
    result = filter_leads(leads, FILTER_PENDING_APPROVAL)
    assert len(result) == 2
    assert all(l["Approval"] != "Yes" for l in result)


def test_filter_in_progress_excludes_terminal_and_removed():
    leads = [
        _lead(IntroSentAt="2026-01-01 09:00:00", Status=""),                     # in progress
        _lead(IntroSentAt="2026-01-01 09:00:00", Status="Stopped - Replied"),    # terminal, excluded
        _lead(IntroSentAt="2026-01-01 09:00:00", Status="Removed"),              # removed, excluded
        _lead(IntroSentAt="", Status=""),                                       # nothing sent yet, excluded
    ]
    result = filter_leads(leads, FILTER_IN_PROGRESS)
    assert len(result) == 1


def test_filter_replied():
    leads = [_lead(Status="Stopped - Replied"), _lead(Status="")]
    result = filter_leads(leads, FILTER_REPLIED)
    assert len(result) == 1
    assert result[0]["Status"] == "Stopped - Replied"


def test_filter_bounced():
    leads = [_lead(Status="Stopped - Bounced"), _lead(Status="")]
    result = filter_leads(leads, FILTER_BOUNCED)
    assert len(result) == 1


def test_filter_removed():
    leads = [_lead(Status="Removed"), _lead(Status="")]
    result = filter_leads(leads, FILTER_REMOVED)
    assert len(result) == 1


# ---------- search_leads ----------

def test_search_matches_first_last_email_company_case_insensitive():
    leads = [_lead(FirstName="Sam", LastName="Lee", Email="sam@abc.com", Company="Acme"),
             _lead(FirstName="Alex", LastName="Kim", Email="alex@xyz.com", Company="Beta")]
    assert len(search_leads(leads, "SAM")) == 1
    assert len(search_leads(leads, "kim")) == 1
    assert len(search_leads(leads, "beta")) == 1
    assert len(search_leads(leads, "xyz.com")) == 1


def test_search_empty_query_returns_all():
    leads = [_lead()]
    assert search_leads(leads, "") == leads
    assert search_leads(leads, "   ") == leads


def test_search_no_match_returns_empty():
    leads = [_lead()]
    assert search_leads(leads, "nomatch") == []
