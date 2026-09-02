"""Tests for asana_client.py — every one mocks requests directly, since
there's no real Asana account available to test against. These confirm
the client builds correct requests and handles responses correctly; they
cannot confirm Asana's actual API behaves exactly as its docs describe.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from asana_client import AsanaClient, AsanaError


def test_requires_access_token():
    with pytest.raises(AsanaError):
        AsanaClient(access_token="", project_gid="123")


def test_requires_project_gid():
    with pytest.raises(AsanaError):
        AsanaClient(access_token="fake-token", project_gid="")


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def test_create_task_returns_gid(monkeypatch):
    captured = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(201, {"data": {"gid": "999"}})

    monkeypatch.setattr("asana_client.requests.post", _fake_post)
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    gid = client.create_task("dudedad — DudeRobe", notes="Channel: Email")

    assert gid == "999"
    assert captured["json"]["data"]["name"] == "dudedad — DudeRobe"
    assert captured["json"]["data"]["projects"] == ["proj-1"]


def test_create_task_raises_on_failure(monkeypatch):
    monkeypatch.setattr("asana_client.requests.post",
                         lambda *a, **k: _FakeResponse(400, text="Bad Request"))
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    with pytest.raises(AsanaError):
        client.create_task("x")


def test_update_task_only_sends_fields_actually_passed(monkeypatch):
    captured = {}

    def _fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(200)

    monkeypatch.setattr("asana_client.requests.put", _fake_put)
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    client.update_task("999", notes="Status: Replied")

    assert captured["json"]["data"] == {"notes": "Status: Replied"}  # name NOT included


def test_update_task_with_no_fields_makes_no_request(monkeypatch):
    def _fail_if_called(*a, **k):
        raise AssertionError("must not call the API with nothing to update")
    monkeypatch.setattr("asana_client.requests.put", _fail_if_called)
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    client.update_task("999")  # no name, no notes


def test_task_exists_true_for_200(monkeypatch):
    monkeypatch.setattr("asana_client.requests.get", lambda *a, **k: _FakeResponse(200))
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    assert client.task_exists("999") is True


def test_task_exists_false_for_404_not_an_error(monkeypatch):
    monkeypatch.setattr("asana_client.requests.get", lambda *a, **k: _FakeResponse(404))
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    assert client.task_exists("deleted-task") is False


def test_task_exists_raises_for_real_errors(monkeypatch):
    monkeypatch.setattr("asana_client.requests.get", lambda *a, **k: _FakeResponse(500, text="oops"))
    client = AsanaClient(access_token="fake-token", project_gid="proj-1")
    with pytest.raises(AsanaError):
        client.task_exists("999")
