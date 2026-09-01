import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from email_account_slots_logic import (
    parse_slot_mapping, serialize_slot_mapping, find_next_free_slot,
    add_account_to_mapping, remove_account_from_mapping, update_account_address_in_mapping,
    get_account_names, read_local_slot_mapping, build_account_secret_payload,
    parse_bulk_accounts_csv, BULK_ACCOUNT_CSV_COLUMNS,
)


# ---------- parse_slot_mapping ----------

def test_parse_slot_mapping_empty_string_returns_empty_dict():
    assert parse_slot_mapping("") == {}
    assert parse_slot_mapping("   ") == {}


def test_parse_slot_mapping_parses_real_yaml():
    raw = "sales1:\n  slot: 1\n  address: sales1@gmail.com\nsales2:\n  slot: 2\n  address: sales2@gmail.com\n"
    mapping = parse_slot_mapping(raw)
    assert mapping == {
        "sales1": {"slot": 1, "address": "sales1@gmail.com"},
        "sales2": {"slot": 2, "address": "sales2@gmail.com"},
    }


def test_parse_slot_mapping_missing_address_defaults_to_empty_string():
    raw = "sales1:\n  slot: 1\n"
    mapping = parse_slot_mapping(raw)
    assert mapping["sales1"]["address"] == ""


def test_parse_slot_mapping_coerces_slot_to_int():
    raw = "sales1:\n  slot: '1'\n  address: a@b.com\n"  # YAML string, not int
    mapping = parse_slot_mapping(raw)
    assert mapping["sales1"]["slot"] == 1
    assert isinstance(mapping["sales1"]["slot"], int)


# ---------- serialize_slot_mapping ----------

def test_serialize_slot_mapping_round_trips_through_parse():
    mapping = {"sales1": {"slot": 1, "address": "sales1@gmail.com"}}
    raw_bytes = serialize_slot_mapping(mapping)
    reparsed = parse_slot_mapping(raw_bytes.decode("utf-8"))
    assert reparsed == mapping


def test_serialize_slot_mapping_empty_dict():
    raw_bytes = serialize_slot_mapping({})
    assert parse_slot_mapping(raw_bytes.decode("utf-8")) == {}


# ---------- find_next_free_slot ----------

def test_find_next_free_slot_empty_mapping_returns_1():
    assert find_next_free_slot({}, slot_count=10) == 1


def test_find_next_free_slot_skips_used_slots():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    assert find_next_free_slot(mapping, slot_count=10) == 3


def test_find_next_free_slot_fills_gaps_not_just_appends():
    # slot 2 was freed by a removal — should be reused, not skipped.
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales3": {"slot": 3, "address": "c@d.com"}}
    assert find_next_free_slot(mapping, slot_count=10) == 2


def test_find_next_free_slot_returns_none_when_full():
    mapping = {f"acct{i}": {"slot": i, "address": f"a{i}@b.com"} for i in range(1, 11)}
    assert find_next_free_slot(mapping, slot_count=10) is None


# ---------- add_account_to_mapping ----------

def test_add_account_to_mapping_assigns_next_free_slot():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    updated = add_account_to_mapping(mapping, "sales2", "b@c.com", slot_count=10)
    assert updated["sales2"] == {"slot": 2, "address": "b@c.com"}


def test_add_account_to_mapping_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    add_account_to_mapping(mapping, "sales2", "b@c.com", slot_count=10)
    assert mapping == {"sales1": {"slot": 1, "address": "a@b.com"}}  # untouched


def test_add_account_to_mapping_rejects_duplicate_name():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    try:
        add_account_to_mapping(mapping, "sales1", "new@b.com", slot_count=10)
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_add_account_to_mapping_rejects_when_all_slots_full():
    mapping = {f"acct{i}": {"slot": i, "address": f"a{i}@b.com"} for i in range(1, 11)}
    try:
        add_account_to_mapping(mapping, "one_too_many", "x@y.com", slot_count=10)
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "slots are full" in str(exc)


# ---------- remove_account_from_mapping ----------

def test_remove_account_from_mapping_removes_entry():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    updated = remove_account_from_mapping(mapping, "sales1")
    assert "sales1" not in updated
    assert "sales2" in updated


def test_remove_account_from_mapping_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    remove_account_from_mapping(mapping, "sales1")
    assert mapping == {"sales1": {"slot": 1, "address": "a@b.com"}}


def test_remove_account_from_mapping_missing_name_is_a_noop_not_an_error():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    updated = remove_account_from_mapping(mapping, "never_existed")
    assert updated == mapping


def test_remove_then_add_reuses_freed_slot():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    after_remove = remove_account_from_mapping(mapping, "sales1")
    after_add = add_account_to_mapping(after_remove, "sales3", "c@d.com", slot_count=10)
    assert after_add["sales3"]["slot"] == 1  # reused the freed slot, not slot 3


# ---------- update_account_address_in_mapping ----------

def test_update_account_address_keeps_same_slot():
    mapping = {"sales1": {"slot": 1, "address": "old@b.com"}}
    updated = update_account_address_in_mapping(mapping, "sales1", "new@b.com")
    assert updated["sales1"] == {"slot": 1, "address": "new@b.com"}


def test_update_account_address_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "old@b.com"}}
    update_account_address_in_mapping(mapping, "sales1", "new@b.com")
    assert mapping["sales1"]["address"] == "old@b.com"


def test_update_account_address_raises_for_unknown_account():
    try:
        update_account_address_in_mapping({}, "ghost", "x@y.com")
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "ghost" in str(exc)


# ---------- get_account_names ----------

def test_get_account_names_sorted():
    mapping = {"sales2": {"slot": 2, "address": "b@c.com"}, "sales1": {"slot": 1, "address": "a@b.com"}}
    assert get_account_names(mapping) == ["sales1", "sales2"]


def test_get_account_names_empty_mapping():
    assert get_account_names({}) == []


# ---------- read_local_slot_mapping ----------

def test_read_local_slot_mapping_returns_empty_when_file_missing(tmp_path):
    missing_path = str(tmp_path / "config" / "email_account_slots.yaml")
    assert read_local_slot_mapping(missing_path) == {}


def test_read_local_slot_mapping_reads_real_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "email_account_slots.yaml"
    path.write_text("sales1:\n  slot: 1\n  address: sales1@gmail.com\n")
    mapping = read_local_slot_mapping(str(path))
    assert mapping == {"sales1": {"slot": 1, "address": "sales1@gmail.com"}}


# ---------- build_account_secret_payload ----------

def test_build_account_secret_payload_minimal_gmail_style():
    payload = json.loads(build_account_secret_payload("sales1", "sales1@gmail.com", "app-pass"))
    assert payload == {"name": "sales1", "address": "sales1@gmail.com", "app_password": "app-pass"}


def test_build_account_secret_payload_includes_custom_provider_fields():
    payload = json.loads(build_account_secret_payload(
        "hostinger1", "sales@example.com", "smtp-pass",
        imap_password="imap-pass", smtp_host="smtp.hostinger.com", smtp_port="587",
        smtp_username="login@example.com", imap_host="imap.hostinger.com", imap_port="993",
        imap_username="login@example.com",
    ))
    assert payload["smtp_host"] == "smtp.hostinger.com"
    assert payload["smtp_port"] == 587
    assert isinstance(payload["smtp_port"], int)
    assert payload["imap_password"] == "imap-pass"


def test_build_account_secret_payload_omits_unset_optional_fields():
    payload = json.loads(build_account_secret_payload("sales1", "sales1@gmail.com", "app-pass"))
    for field in ("imap_password", "smtp_host", "smtp_port", "smtp_username",
                  "imap_host", "imap_port", "imap_username"):
        assert field not in payload


# ---------- parse_bulk_accounts_csv ----------

def test_parse_bulk_accounts_csv_simple_gmail_rows():
    columns = ["Name", "Email", "Password"]
    rows = [
        {"Name": "sales1", "Email": "sales1@gmail.com", "Password": "pass1"},
        {"Name": "sales2", "Email": "sales2@gmail.com", "Password": "pass2"},
    ]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert errors == []
    assert len(parsed) == 2
    assert parsed[0]["name"] == "sales1"
    assert parsed[0]["address"] == "sales1@gmail.com"
    assert parsed[0]["password"] == "pass1"
    assert parsed[0]["smtp_host"] is None


def test_parse_bulk_accounts_csv_custom_provider_rows():
    columns = BULK_ACCOUNT_CSV_COLUMNS
    rows = [{
        "Name": "hostinger1", "Email": "sales@example.com", "Password": "smtp-pass",
        "IMAP Password": "imap-pass", "SMTP Host": "smtp.hostinger.com", "SMTP Port": "587",
        "SMTP Username": "login@example.com", "IMAP Host": "imap.hostinger.com", "IMAP Port": "993",
        "IMAP Username": "login@example.com",
    }]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert errors == []
    assert parsed[0]["smtp_host"] == "smtp.hostinger.com"
    assert parsed[0]["imap_password"] == "imap-pass"


def test_parse_bulk_accounts_csv_requires_email_and_password_columns():
    parsed, errors = parse_bulk_accounts_csv(["Name"], [{"Name": "x"}])
    assert parsed == []
    assert any("Email" in e or "Password" in e for e in errors)


def test_parse_bulk_accounts_csv_skips_row_missing_email():
    columns = ["Name", "Email", "Password"]
    rows = [{"Name": "sales1", "Email": "", "Password": "pass1"}]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert parsed == []
    assert "Row 1" in errors[0]
    assert "Email is required" in errors[0]


def test_parse_bulk_accounts_csv_skips_row_missing_password():
    columns = ["Name", "Email", "Password"]
    rows = [{"Name": "sales1", "Email": "a@b.com", "Password": ""}]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert parsed == []
    assert "Password is required" in errors[0]


def test_parse_bulk_accounts_csv_defaults_name_to_email_local_part():
    columns = ["Email", "Password"]
    rows = [{"Email": "sales1@gmail.com", "Password": "pass1"}]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert errors == []
    assert parsed[0]["name"] == "sales1"


def test_parse_bulk_accounts_csv_one_bad_row_does_not_block_valid_rows():
    columns = ["Name", "Email", "Password"]
    rows = [
        {"Name": "sales1", "Email": "sales1@gmail.com", "Password": "pass1"},
        {"Name": "bad", "Email": "", "Password": "pass2"},
        {"Name": "sales3", "Email": "sales3@gmail.com", "Password": "pass3"},
    ]
    parsed, errors = parse_bulk_accounts_csv(columns, rows)
    assert len(parsed) == 2
    assert len(errors) == 1
    assert {p["name"] for p in parsed} == {"sales1", "sales3"}


def test_parse_bulk_accounts_csv_empty_rows_list():
    parsed, errors = parse_bulk_accounts_csv(["Name", "Email", "Password"], [])
    assert parsed == []
    assert errors == []
