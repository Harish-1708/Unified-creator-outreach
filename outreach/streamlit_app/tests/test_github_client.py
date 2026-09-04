import base64
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import github_client
from github_client import GitHubClient, GitHubActionsError


def _client():
    return GitHubClient(token="tok", owner="acme", repo="outreach")


def _fake_response(status_code, json_data=None, text="", content=b"x"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.json.return_value = json_data or {}
    return resp


# ---------- dispatch_workflow ----------

def test_dispatch_workflow_returns_run_details_on_200(monkeypatch):
    resp = _fake_response(200, {"id": 42, "html_url": "https://github.com/acme/outreach/actions/runs/42"})
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    result = _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert result == {"id": 42, "html_url": "https://github.com/acme/outreach/actions/runs/42"}


def test_dispatch_workflow_returns_none_on_204(monkeypatch):
    resp = _fake_response(204, content=b"")
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    result = _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert result is None


def test_dispatch_workflow_raises_on_error_status(monkeypatch):
    resp = _fake_response(422, text="Invalid inputs")
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    with pytest.raises(GitHubActionsError, match="422"):
        _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})


def test_dispatch_workflow_sends_return_run_details_flag(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(204, content=b"")

    monkeypatch.setattr(github_client.requests, "post", fake_post)
    _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert captured["json"]["return_run_details"] is True
    assert captured["json"]["inputs"] == {"campaign": "Foo"}


# ---------- get_run ----------

def test_get_run_returns_json_on_200(monkeypatch):
    resp = _fake_response(200, {"status": "completed", "conclusion": "success"})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    run = _client().get_run(42)
    assert run["status"] == "completed"


def test_get_run_raises_on_404(monkeypatch):
    resp = _fake_response(404, text="Not Found")
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    with pytest.raises(GitHubActionsError, match="404"):
        _client().get_run(999)


# ---------- find_recent_run ----------

def test_find_recent_run_returns_first_run(monkeypatch):
    resp = _fake_response(200, {"workflow_runs": [{"id": 7}, {"id": 6}]})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    run = _client().find_recent_run("send_batch.yml")
    assert run == {"id": 7}


def test_find_recent_run_returns_none_when_empty(monkeypatch):
    resp = _fake_response(200, {"workflow_runs": []})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    assert _client().find_recent_run("send_batch.yml") is None


# ---------- campaign creation — direct commit ----------

def test_commit_campaign_files_directly_writes_every_file_to_main(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None, params=None):
        return _fake_response(404)  # neither file exists yet — brand new

    def fake_put(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "get", fake_get)
    monkeypatch.setattr(github_client.requests, "put", fake_put)

    client = _client()
    client.commit_campaign_files_directly(
        files=[
            {"path": "templates/Foo/intro_A.txt", "content": b"Subject: Hi\n\nBody A"},
            {"path": "templates/Foo/intro_B.txt", "content": b"Subject: Hi\n\nBody B"},
        ],
        commit_message="Add campaign: Foo",
    )

    assert len(calls) == 2
    assert calls[0][0].endswith("/contents/templates/Foo/intro_A.txt")
    assert calls[0][1]["branch"] == "main"
    assert calls[0][1]["message"] == "Add campaign: Foo"
    assert "sha" not in calls[0][1]  # new file — no sha should be sent
    assert calls[1][0].endswith("/contents/templates/Foo/intro_B.txt")


def test_commit_campaign_files_directly_raises_on_first_failure(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    monkeypatch.setattr(github_client.requests, "put",
                         lambda *a, **kw: _fake_response(422, text="Invalid content"))
    with pytest.raises(GitHubActionsError, match="Failed to create/update file"):
        _client().commit_campaign_files_directly(
            files=[{"path": "templates/Foo/intro_A.txt", "content": b"bad"}],
            commit_message="Add campaign: Foo",
        )


def test_create_file_defaults_to_main_branch(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)
    _client().create_file("templates/Foo/intro_A.txt", b"content", "msg")  # no branch arg
    assert captured["json"]["branch"] == "main"


def test_create_file_encodes_content_as_base64(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)
    _client().create_file("templates/Foo/intro_A.txt", b"Subject: Hi\n\nBody", "msg", "branch")

    import base64
    assert base64.b64decode(captured["json"]["content"]) == b"Subject: Hi\n\nBody"


def test_create_file_raises_on_error_status(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    monkeypatch.setattr(github_client.requests, "put", lambda *a, **kw: _fake_response(422, text="bad"))
    with pytest.raises(GitHubActionsError, match="Failed to create/update file"):
        _client().create_file("templates/Foo/intro_A.txt", b"content", "msg", "main")


# ---------- get_file_sha / create_file updating an EXISTING file ----------
# The real regression this covers: editing a template or updating campaign
# settings writes to a path that already exists. GitHub's contents API
# requires the current file's sha for that (a create-only payload gets a
# 422). This was missed originally because tests mocked create_file
# wholesale rather than exercising this real API contract.

def test_get_file_sha_returns_sha_when_file_exists(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, {"sha": "abc123"}))
    assert _client().get_file_sha("templates/Foo/intro_A.txt") == "abc123"


def test_get_file_sha_returns_none_when_file_does_not_exist(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    assert _client().get_file_sha("templates/Foo/intro_A.txt") is None


def test_get_file_sha_raises_on_other_error_status(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(401, text="Bad credentials"))
    with pytest.raises(GitHubActionsError, match="Failed to check existing file"):
        _client().get_file_sha("templates/Foo/intro_A.txt")


def test_create_file_includes_sha_when_file_already_exists(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, {"sha": "existing-sha"}))

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(200, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)
    _client().create_file("templates/Foo/intro_A.txt", b"updated content", "Edit intro_A")
    assert captured["json"]["sha"] == "existing-sha"


def test_create_file_omits_sha_when_file_is_new(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)
    _client().create_file("templates/Foo/intro_A.txt", b"new content", "Create intro_A")
    assert "sha" not in captured["json"]


def test_create_file_passes_correct_ref_when_checking_existing_sha(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None, params=None):
        captured["params"] = params
        return _fake_response(404)

    monkeypatch.setattr(github_client.requests, "get", fake_get)
    monkeypatch.setattr(github_client.requests, "put", lambda *a, **kw: _fake_response(201, {}))
    _client().create_file("templates/Foo/intro_A.txt", b"content", "msg", branch="a-feature-branch")
    assert captured["params"] == {"ref": "a-feature-branch"}


# ---------- delete_file ----------

def test_delete_file_fetches_sha_and_deletes(monkeypatch):
    captured = {}
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, {"sha": "abc123"}))

    def fake_delete(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response(200, {})

    monkeypatch.setattr(github_client.requests, "delete", fake_delete)
    _client().delete_file("templates/Foo/followup2_A.txt", "Delete stage")

    assert captured["url"].endswith("/contents/templates/Foo/followup2_A.txt")
    assert captured["json"]["sha"] == "abc123"
    assert captured["json"]["message"] == "Delete stage"


def test_delete_file_is_a_noop_when_file_already_gone(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    delete_called = []
    monkeypatch.setattr(github_client.requests, "delete", lambda *a, **kw: delete_called.append(1))
    _client().delete_file("templates/Foo/already_gone.txt", "msg")
    assert delete_called == []  # never even attempted — nothing to delete


def test_delete_file_raises_on_failure(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, {"sha": "abc123"}))
    monkeypatch.setattr(github_client.requests, "delete", lambda *a, **kw: _fake_response(422, text="conflict"))
    with pytest.raises(GitHubActionsError, match="Failed to delete file"):
        _client().delete_file("templates/Foo/intro_A.txt", "msg")


def test_delete_file_accepts_200_or_204(monkeypatch):
    for status in (200, 204):
        monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, {"sha": "abc"}))
        monkeypatch.setattr(github_client.requests, "delete", lambda *a, **kw: _fake_response(status, {}))
        _client().delete_file("templates/Foo/intro_A.txt", "msg")  # doesn't raise


# =============================================================================
# Repository secrets — set/delete only, matching GitHub's write-only API.
# encrypt_secret_value's tests use a REAL generated keypair and verify a
# genuine decrypt round-trip, not just "produces some bytes" — this is the
# one piece of crypto in the whole app, so it gets proven correct against
# the actual algorithm, not just exercised.
# =============================================================================

def _generate_test_keypair():
    from nacl import encoding, public
    private_key = public.PrivateKey.generate()
    public_key_b64 = private_key.public_key.encode(encoder=encoding.Base64Encoder).decode("utf-8")
    return private_key, public_key_b64


def test_encrypt_secret_value_round_trips_correctly():
    """The real proof: what this method produces can actually be decrypted
    back to the original plaintext by the holder of the matching private
    key — exactly GitHub's own position when it receives this value."""
    from nacl import public
    private_key, public_key_b64 = _generate_test_keypair()

    encrypted_b64 = _client().encrypt_secret_value("super-secret-app-password", public_key_b64)

    sealed_box = public.SealedBox(private_key)
    decrypted = sealed_box.decrypt(base64.b64decode(encrypted_b64))
    assert decrypted.decode("utf-8") == "super-secret-app-password"


def test_encrypt_secret_value_different_each_time():
    """Sealed-box encryption is randomized (a fresh ephemeral key per
    call) — encrypting the same plaintext twice must NOT produce identical
    ciphertext, or the scheme would leak whether two secrets are equal."""
    _, public_key_b64 = _generate_test_keypair()
    first = _client().encrypt_secret_value("same-value", public_key_b64)
    second = _client().encrypt_secret_value("same-value", public_key_b64)
    assert first != second


def test_encrypt_secret_value_handles_unicode_plaintext():
    from nacl import public
    private_key, public_key_b64 = _generate_test_keypair()
    encrypted_b64 = _client().encrypt_secret_value("pässwörd-测试-🔒", public_key_b64)
    sealed_box = public.SealedBox(private_key)
    decrypted = sealed_box.decrypt(base64.b64decode(encrypted_b64))
    assert decrypted.decode("utf-8") == "pässwörd-测试-🔒"


def test_get_repo_public_key_returns_key_and_id(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(200, {"key_id": "abc123", "key": "base64keydata"}))
    result = _client().get_repo_public_key()
    assert result == {"key_id": "abc123", "key": "base64keydata"}


def test_get_repo_public_key_raises_on_failure(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(403, text="Forbidden"))
    with pytest.raises(GitHubActionsError, match="Failed to fetch repo public key"):
        _client().get_repo_public_key()


def test_set_secret_fetches_fresh_key_encrypts_and_puts(monkeypatch):
    _, public_key_b64 = _generate_test_keypair()
    captured = {}

    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(200, {"key_id": "key-1", "key": public_key_b64}))

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)

    _client().set_secret("EMAIL_ACCOUNT_SLOT_1", "my-app-password")

    assert captured["url"].endswith("/actions/secrets/EMAIL_ACCOUNT_SLOT_1")
    assert captured["json"]["key_id"] == "key-1"
    assert "encrypted_value" in captured["json"]
    assert "my-app-password" not in str(captured["json"])  # never sent in plaintext


def test_set_secret_raises_on_put_failure(monkeypatch):
    _, public_key_b64 = _generate_test_keypair()
    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(200, {"key_id": "key-1", "key": public_key_b64}))
    monkeypatch.setattr(github_client.requests, "put", lambda *a, **kw: _fake_response(422, text="bad request"))
    with pytest.raises(GitHubActionsError, match="Failed to set secret"):
        _client().set_secret("EMAIL_ACCOUNT_SLOT_1", "pass")


def test_set_secret_accepts_201_or_204():
    for status in (201, 204):
        _, public_key_b64 = _generate_test_keypair()
        import unittest.mock as mock
        with mock.patch.object(github_client.requests, "get",
                                return_value=_fake_response(200, {"key_id": "k", "key": public_key_b64})), \
             mock.patch.object(github_client.requests, "put", return_value=_fake_response(status, {})):
            _client().set_secret("SLOT_1", "value")  # doesn't raise


def test_delete_secret_success_on_204(monkeypatch):
    captured = {}

    def fake_delete(url, headers=None, timeout=None):
        captured["url"] = url
        return _fake_response(204)

    monkeypatch.setattr(github_client.requests, "delete", fake_delete)
    _client().delete_secret("EMAIL_ACCOUNT_SLOT_3")
    assert captured["url"].endswith("/actions/secrets/EMAIL_ACCOUNT_SLOT_3")


def test_delete_secret_treats_404_as_success_not_error(monkeypatch):
    """The secret was already gone — the caller's goal ('this shouldn't
    exist') is already satisfied, so this must NOT raise."""
    monkeypatch.setattr(github_client.requests, "delete", lambda *a, **kw: _fake_response(404))
    _client().delete_secret("EMAIL_ACCOUNT_SLOT_3")  # doesn't raise


def test_delete_secret_raises_on_other_failure(monkeypatch):
    monkeypatch.setattr(github_client.requests, "delete", lambda *a, **kw: _fake_response(403, text="Forbidden"))
    with pytest.raises(GitHubActionsError, match="Failed to delete secret"):
        _client().delete_secret("EMAIL_ACCOUNT_SLOT_3")


# ---------- set_variable ----------

def test_set_variable_updates_via_patch_when_already_exists(monkeypatch):
    calls = {}

    def _fake_get(url, headers=None, timeout=None):
        return _fake_response(200, {"name": "DEEP_RESEARCH_REPORT", "value": "old"})

    def _fake_patch(url, json=None, headers=None, timeout=None):
        calls["method"] = "patch"
        calls["url"] = url
        calls["json"] = json
        return _fake_response(204)

    def _fail_if_post_called(*a, **kw):
        raise AssertionError("must not POST (create) when the variable already exists")

    monkeypatch.setattr(github_client.requests, "get", _fake_get)
    monkeypatch.setattr(github_client.requests, "patch", _fake_patch)
    monkeypatch.setattr(github_client.requests, "post", _fail_if_post_called)

    _client().set_variable("DEEP_RESEARCH_REPORT", "new value")

    assert calls["method"] == "patch"
    assert calls["json"] == {"name": "DEEP_RESEARCH_REPORT", "value": "new value"}
    assert calls["url"].endswith("/actions/variables/DEEP_RESEARCH_REPORT")


def test_set_variable_creates_via_post_when_missing(monkeypatch):
    calls = {}

    def _fake_get(url, headers=None, timeout=None):
        return _fake_response(404)

    def _fake_post(url, json=None, headers=None, timeout=None):
        calls["method"] = "post"
        calls["json"] = json
        return _fake_response(201)

    def _fail_if_patch_called(*a, **kw):
        raise AssertionError("must not PATCH (update) when the variable doesn't exist yet")

    monkeypatch.setattr(github_client.requests, "get", _fake_get)
    monkeypatch.setattr(github_client.requests, "post", _fake_post)
    monkeypatch.setattr(github_client.requests, "patch", _fail_if_patch_called)

    _client().set_variable("DEEP_RESEARCH_REPORT", "first value")

    assert calls["method"] == "post"
    assert calls["json"] == {"name": "DEEP_RESEARCH_REPORT", "value": "first value"}


def test_set_variable_raises_on_failure(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: _fake_response(422, text="Bad Request"))

    with pytest.raises(GitHubActionsError):
        _client().set_variable("BAD_NAME", "value")


# ---------- get_file_content ----------

def test_get_file_content_decodes_base64_correctly(monkeypatch):
    import base64
    real_content = "sales2:\n  slot: 1\n  address: a@b.com\n"
    encoded = base64.b64encode(real_content.encode("utf-8")).decode("ascii")

    def _fake_get(url, headers=None, params=None, timeout=None):
        return _fake_response(200, {"content": encoded, "sha": "abc123"})

    monkeypatch.setattr(github_client.requests, "get", _fake_get)
    result = _client().get_file_content("outreach/config/email_account_slots.yaml")
    assert result == real_content


def test_get_file_content_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    result = _client().get_file_content("outreach/config/email_account_slots.yaml")
    assert result is None


def test_get_file_content_raises_on_other_failures(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(500, text="Internal Server Error"))
    with pytest.raises(GitHubActionsError):
        _client().get_file_content("outreach/config/email_account_slots.yaml")


# ---------- list_directory_files ----------

def test_list_directory_files_returns_filenames_only(monkeypatch):
    entries = [
        {"name": "intro_A.txt", "type": "file"},
        {"name": "intro_B.txt", "type": "file"},
        {"name": "subfolder", "type": "dir"},
    ]
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(200, entries))
    result = _client().list_directory_files("outreach/streamlit_app/campaigns/X")
    assert result == ["intro_A.txt", "intro_B.txt"]


def test_list_directory_files_returns_empty_list_when_directory_missing(monkeypatch):
    """A brand-new campaign with nothing committed yet is normal, not an
    error — must return [], never raise."""
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404))
    result = _client().list_directory_files("outreach/streamlit_app/campaigns/NewCampaign")
    assert result == []


def test_list_directory_files_raises_when_path_is_a_file_not_a_directory(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(200, {"name": "intro_A.txt", "type": "file"}))
    with pytest.raises(GitHubActionsError, match="is a file, not a directory"):
        _client().list_directory_files("outreach/streamlit_app/campaigns/X/intro_A.txt")


def test_list_directory_files_raises_on_other_failures(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get",
                         lambda *a, **kw: _fake_response(500, text="Internal Server Error"))
    with pytest.raises(GitHubActionsError):
        _client().list_directory_files("outreach/streamlit_app/campaigns/X")
