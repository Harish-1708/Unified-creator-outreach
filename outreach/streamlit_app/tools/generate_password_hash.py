#!/usr/bin/env python3
"""Run this locally (never on a server, never commit the output to a public
repo) to generate a salted password hash for a colleague's login.

Usage:
    python tools/generate_password_hash.py

Paste the printed block into Streamlit Secrets under [auth_users].
"""
import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import hash_password  # noqa: E402


def main():
    username = input("Username (how they'll log in): ").strip()
    if not username:
        print("Username can't be blank.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password (input hidden): ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    salt = secrets.token_hex(16)
    password_hash = hash_password(password, salt)

    print("\nAdd this to Streamlit Secrets (never commit it to the repo):\n")
    print(f"[auth_users.{username}]")
    print(f'salt = "{salt}"')
    print(f'password_hash = "{password_hash}"')
    print()


if __name__ == "__main__":
    main()
