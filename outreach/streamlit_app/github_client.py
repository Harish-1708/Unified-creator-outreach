"""Thin GitHub REST API client. Every network call is isolated to this
module and goes through `requests`, so tests can mock `requests.*` directly
without touching real GitHub.

Token scope needed:
- actions: read, actions: write  -> dispatch_workflow, get_run, find_recent_run
- contents: write -> create_file / commit_campaign_files_directly (New
  Campaign page, template edits, campaign settings)
- secrets: write -> get_repo_public_key / set_secret / delete_secret (Email
  Accounts management ONLY — a materially larger grant than anything
  else in this app needs; see those methods' docstrings for what this
  can and can't do)
"""
import base64
from typing import Dict, List, Optional

import requests

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 20


class GitHubActionsError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, owner: str, repo: str, timeout: int = DEFAULT_TIMEOUT):
        self.owner = owner
        self.repo = repo
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ---------- Triggering + polling workflow runs ----------

    def dispatch_workflow(self, workflow_file: str, inputs: Dict[str, str],
                           ref: str = "main") -> Optional[Dict]:
        """Triggers workflow_dispatch. Returns {'id':..., 'html_url':...}
        directly when the API's return_run_details feature is available;
        returns None otherwise (caller should fall back to
        find_recent_run)."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_file}/dispatches"
        payload = {"ref": ref, "inputs": inputs, "return_run_details": True}
        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 204):
            raise GitHubActionsError(
                f"Failed to dispatch '{workflow_file}': {resp.status_code} {resp.text[:300]}"
            )
        if resp.status_code == 200 and resp.content:
            return resp.json()
        return None

    def get_run(self, run_id: int) -> Dict:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}"
        resp = requests.get(url, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to fetch run {run_id}: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def find_recent_run(self, workflow_file: str, branch: str = "main") -> Optional[Dict]:
        """Fallback correlation if dispatch_workflow returned None — most
        recent workflow_dispatch run for this workflow/branch. Best-effort;
        can theoretically race with a second concurrent trigger."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_file}/runs"
        resp = requests.get(
            url, headers=self._headers,
            params={"event": "workflow_dispatch", "branch": branch, "per_page": 1},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to list runs for '{workflow_file}': {resp.status_code} {resp.text[:300]}")
        runs = resp.json().get("workflow_runs", [])
        return runs[0] if runs else None

    # ---------- Campaign creation — direct commit, no branch/PR ----------
    #
    # Deliberately a direct commit to `base` (main), not a PR: the goal is
    # for campaign creation to never require a trip to GitHub. The
    # remaining safety net is the in-app confirmation the Streamlit page
    # requires before calling this — see campaign_builder.py.

    def get_file_sha(self, path: str, ref: str = "main") -> Optional[str]:
        """Current SHA of the file at `path` on `ref`, or None if it
        doesn't exist yet. GitHub's contents API requires this SHA when
        updating an existing file — omitting it (as an earlier version of
        create_file did) works fine for brand-new files but is rejected
        with a 422 for anything that already exists, which is exactly
        what editing a template or updating campaign settings does."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = requests.get(url, headers=self._headers, params={"ref": ref}, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to check existing file '{path}': {resp.status_code} {resp.text[:300]}")
        return resp.json().get("sha")

    def list_directory_files(self, path: str, ref: str = "main") -> List[str]:
        """Filenames (not full paths) in the directory at `path` on `ref`
        — empty list if the directory doesn't exist (a brand-new
        campaign that's never had anything committed yet is a normal,
        expected case, not an error).

        This is the fix for the exact class of bug that caused real,
        permanent data corruption: reading "what template files exist"
        from a local git checkout that can lag behind a very recent
        commit during a Streamlit Cloud redeploy. A duplicate-then-
        immediately-delete-stage sequence hit that lag window and wrote
        based on an inconsistent view of which variants existed.
        Fetching the live directory listing from GitHub's API immediately
        before any read OR any destructive action closes that window —
        this always reflects the actual latest commit."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = requests.get(url, headers=self._headers, params={"ref": ref}, timeout=self.timeout)
        if resp.status_code == 404:
            return []
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to list '{path}': {resp.status_code} {resp.text[:300]}")
        entries = resp.json()
        if not isinstance(entries, list):
            # GitHub returns a single object (not a list) if `path` is a
            # FILE rather than a directory — a caller mistake, not a
            # transient error, so this is worth a clear message.
            raise GitHubActionsError(f"'{path}' is a file, not a directory.")
        return [entry["name"] for entry in entries if entry.get("type") == "file"]

    def get_file_content(self, path: str, ref: str = "main") -> Optional[str]:
        """Current LIVE content of the file at `path` on `ref` — decoded
        text, or None if it doesn't exist yet.

        This is the fix for a real read-modify-write race: any caller
        that reads a file from a slow-to-update local disk copy (e.g. a
        Streamlit Cloud app between deploys) and uses THAT as the basis
        for a subsequent write can silently undo someone else's more
        recent change — the write recomputes from stale data and
        overwrites whatever changed in between. Fetching fresh from
        GitHub's API immediately before every mutation closes that
        window entirely, since this always reflects the actual latest
        commit, never a redeploy-lagged copy."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        resp = requests.get(url, headers=self._headers, params={"ref": ref}, timeout=self.timeout)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to read '{path}': {resp.status_code} {resp.text[:300]}")
        encoded = resp.json().get("content", "")
        return base64.b64decode(encoded).decode("utf-8")

    def create_file(self, path: str, content_bytes: bytes, message: str, branch: str = "main") -> None:
        """Creates OR updates a file at `path`. Every write in this app —
        new templates, edited templates, campaign settings — goes through
        this one method, so fetching the current SHA here (when the file
        already exists) fixes update-in-place for all of them at once."""
        existing_sha = self.get_file_sha(path, ref=branch)
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha
        resp = requests.put(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise GitHubActionsError(f"Failed to create/update file '{path}': {resp.status_code} {resp.text[:300]}")

    def delete_file(self, path: str, message: str, branch: str = "main") -> None:
        """Deletes a file at `path`. GitHub's contents API requires the
        current SHA to delete, same as create_file requires for an
        update — fetched fresh here. If the file is already gone, this is
        a no-op, not an error — the caller's goal ('this file shouldn't
        exist') is already satisfied, same philosophy as delete_secret."""
        existing_sha = self.get_file_sha(path, ref=branch)
        if existing_sha is None:
            return
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        payload = {"message": message, "sha": existing_sha, "branch": branch}
        resp = requests.delete(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 204):
            raise GitHubActionsError(f"Failed to delete file '{path}': {resp.status_code} {resp.text[:300]}")

    def commit_campaign_files_directly(self, files: List[Dict[str, bytes]], commit_message: str,
                                        base: str = "main") -> None:
        """files: [{'path': 'outreach/templates/Foo/intro_A.txt', 'content': b'...'}].
        Commits every file straight to `base`. Raises on the first failure —
        callers should treat a partial failure as "check the repo", since a
        prior file in the list may have already landed."""
        for f in files:
            self.create_file(f["path"], f["content"], message=commit_message, branch=base)

    # ---------- Repository secrets — set/delete only, NEVER read ----------
    #
    # GitHub Secrets are write-only by design: this API can set or delete a
    # secret's value, but there is no endpoint that returns an existing
    # value, to this token or any other. That's the actual security
    # property everything here relies on — Streamlit briefly holds a
    # plaintext password only for the instant it takes to encrypt and send
    # it below; it's never stored, logged, or displayed anywhere by this
    # client. Requires a token with `secrets: write` — a materially larger
    # grant than anything else in this app needs, used ONLY by the Email
    # Accounts management page.

    def get_repo_public_key(self) -> Dict[str, str]:
        """{'key_id': ..., 'key': <base64>} — GitHub's current public key
        for this repo, used to encrypt every secret value before it's ever
        sent over the wire. Fetched fresh each time rather than cached,
        since GitHub can rotate this key."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/secrets/public-key"
        resp = requests.get(url, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to fetch repo public key: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def encrypt_secret_value(self, plaintext: str, public_key_b64: str) -> str:
        """Libsodium sealed-box encryption, exactly as GitHub's API
        requires (https://docs.github.com/en/rest/actions/secrets) — a
        one-way encryption only GitHub's own private key can open. Kept as
        its own method (no network call) so it's directly unit-testable
        without touching the API."""
        from nacl import encoding, public
        public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
        sealed_box = public.SealedBox(public_key)
        encrypted = sealed_box.encrypt(plaintext.encode("utf-8"))
        return base64.b64encode(encrypted).decode("utf-8")

    def set_variable(self, name: str, value: str) -> None:
        """Repo-level Actions Variable — a DIFFERENT API from secrets
        above (no encryption; Variables aren't sensitive by definition).
        GitHub's Variables API distinguishes create from update at the
        HTTP-method level (POST vs PATCH), unlike secrets' single PUT —
        so this checks existence first via GET, then picks the right one,
        rather than guessing and retrying on failure."""
        base_url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/variables"
        exists_resp = requests.get(f"{base_url}/{name}", headers=self._headers, timeout=self.timeout)
        payload = {"name": name, "value": value}
        if exists_resp.status_code == 200:
            resp = requests.patch(f"{base_url}/{name}", json=payload, headers=self._headers,
                                   timeout=self.timeout)
            ok_codes = (204,)
        else:
            resp = requests.post(base_url, json=payload, headers=self._headers, timeout=self.timeout)
            ok_codes = (201,)
        if resp.status_code not in ok_codes:
            raise GitHubActionsError(f"Failed to set variable '{name}': {resp.status_code} {resp.text[:300]}")

    def set_secret(self, secret_name: str, plaintext_value: str) -> None:
        """Encrypts plaintext_value with the repo's CURRENT public key
        (fetched fresh, never cached — see get_repo_public_key) and sets
        it as a repository secret. Creates the secret if it doesn't exist,
        overwrites it if it does — GitHub's API doesn't distinguish the
        two operations."""
        key_info = self.get_repo_public_key()
        encrypted_value = self.encrypt_secret_value(plaintext_value, key_info["key"])
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/secrets/{secret_name}"
        payload = {"encrypted_value": encrypted_value, "key_id": key_info["key_id"]}
        resp = requests.put(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (201, 204):
            raise GitHubActionsError(f"Failed to set secret '{secret_name}': {resp.status_code} {resp.text[:300]}")

    def delete_secret(self, secret_name: str) -> None:
        """204 means deleted; 404 means it was already gone — both count
        as success here, since the caller's goal ("this secret should not
        exist") is satisfied either way."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/secrets/{secret_name}"
        resp = requests.delete(url, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (204, 404):
            raise GitHubActionsError(f"Failed to delete secret '{secret_name}': {resp.status_code} {resp.text[:300]}")
