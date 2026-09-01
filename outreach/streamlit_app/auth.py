"""Simple username/password login — no Google/OAuth involved, so any
colleague can log in with credentials you hand them directly.

Passwords are never stored in plaintext, anywhere. secrets.toml holds a
PBKDF2 hash + a per-user salt, generated once with
tools/generate_password_hash.py. This module only ever compares hashes.

Trade-off, stated plainly: without an OAuth provider there's no "forgot
password" flow and no persistent cross-session cookie — closing the tab (or
a hard refresh in some cases) ends the session and the user logs in again.
If that becomes annoying, streamlit-authenticator (cookie-based sessions)
or Streamlit's native st.login()/OIDC are the upgrade paths — this module
is intentionally the simple version.
"""
import hashlib
import hmac
import time
from typing import Dict, Optional

import streamlit as st

PBKDF2_ITERATIONS = 200_000

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()


def _get_configured_users() -> Dict[str, Dict[str, str]]:
    """Reads [auth_users] from secrets.toml:

        [auth_users.alice]
        salt = "..."
        password_hash = "..."

    Returns {} if not configured (caller should treat that as a setup
    error, not silently let anyone in).
    """
    return dict(st.secrets.get("auth_users", {}))


def _verify(username: str, password: str) -> bool:
    users = _get_configured_users()
    user = users.get(username)
    if not user:
        return False
    salt = user.get("salt", "")
    expected = user.get("password_hash", "")
    if not salt or not expected:
        return False
    computed = hash_password(password, salt)
    # constant-time comparison — don't let response timing leak whether
    # the username existed or how much of the hash matched
    return hmac.compare_digest(computed, expected)


def _init_session_state() -> None:
    st.session_state.setdefault("auth_user", None)
    st.session_state.setdefault("auth_failed_attempts", 0)
    st.session_state.setdefault("auth_locked_until", 0.0)


def is_authenticated() -> bool:
    _init_session_state()
    return st.session_state["auth_user"] is not None


def current_user() -> Optional[str]:
    return st.session_state.get("auth_user")


def logout() -> None:
    st.session_state["auth_user"] = None


def login_gate() -> bool:
    """Renders a login form if not already authenticated. Returns True once
    logged in (caller should render the rest of the page only when this
    returns True, and should call this at the top of EVERY page — Streamlit
    multipage apps run each page's script independently)."""
    _init_session_state()

    if is_authenticated():
        return True

    if not _get_configured_users():
        st.error(
            "No users are configured in secrets.toml under [auth_users]. "
            "Run tools/generate_password_hash.py to create one, then add it "
            "to Streamlit Secrets before anyone can log in."
        )
        return False

    now = time.time()
    locked_until = st.session_state["auth_locked_until"]
    if now < locked_until:
        remaining = int(locked_until - now)
        st.error(f"Too many failed attempts. Try again in {remaining}s.")
        return False

    st.title("🔒 Outreach Control Panel — Login")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        if _verify(username.strip(), password):
            st.session_state["auth_user"] = username.strip()
            st.session_state["auth_failed_attempts"] = 0
            st.rerun()
        else:
            st.session_state["auth_failed_attempts"] += 1
            if st.session_state["auth_failed_attempts"] >= MAX_FAILED_ATTEMPTS:
                st.session_state["auth_locked_until"] = time.time() + LOCKOUT_SECONDS
                st.session_state["auth_failed_attempts"] = 0
                st.error(f"Too many failed attempts. Locked for {LOCKOUT_SECONDS}s.")
            else:
                st.error("Invalid username or password.")

    return False
