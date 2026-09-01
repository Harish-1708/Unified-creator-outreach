import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import auth


def test_hash_password_deterministic_for_same_salt():
    h1 = auth.hash_password("correct horse", "salt123")
    h2 = auth.hash_password("correct horse", "salt123")
    assert h1 == h2


def test_hash_password_differs_by_salt():
    h1 = auth.hash_password("correct horse", "salt123")
    h2 = auth.hash_password("correct horse", "salt456")
    assert h1 != h2


def test_hash_password_differs_by_password():
    h1 = auth.hash_password("correct horse", "salt123")
    h2 = auth.hash_password("wrong horse", "salt123")
    assert h1 != h2


def _set_users(monkeypatch, users):
    monkeypatch.setattr(auth, "st", auth.st)  # no-op, keeps reference stable
    monkeypatch.setattr(st, "secrets", {"auth_users": users})


def test_verify_accepts_correct_password(monkeypatch):
    salt = "abc123"
    users = {"alice": {"salt": salt, "password_hash": auth.hash_password("hunter2", salt)}}
    _set_users(monkeypatch, users)
    assert auth._verify("alice", "hunter2") is True


def test_verify_rejects_wrong_password(monkeypatch):
    salt = "abc123"
    users = {"alice": {"salt": salt, "password_hash": auth.hash_password("hunter2", salt)}}
    _set_users(monkeypatch, users)
    assert auth._verify("alice", "wrong") is False


def test_verify_rejects_unknown_username(monkeypatch):
    users = {"alice": {"salt": "s", "password_hash": "h"}}
    _set_users(monkeypatch, users)
    assert auth._verify("bob", "anything") is False


def test_verify_rejects_when_no_users_configured(monkeypatch):
    _set_users(monkeypatch, {})
    assert auth._verify("alice", "hunter2") is False


def test_get_configured_users_empty_when_not_set(monkeypatch):
    monkeypatch.setattr(st, "secrets", {})
    assert auth._get_configured_users() == {}
