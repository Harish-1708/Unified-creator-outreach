"""
asana_client.py — a thin, tested wrapper around Asana's REST API.

Deliberately uses ONLY basic task fields (name, notes, completed) for
v1 — not Asana custom fields. This is a real, deliberate scope decision:
using custom fields (e.g. a "Channel" or "Status" dropdown field visible
in Paula's board view) needs her project's actual custom field GIDs,
which aren't known yet ("Paula's Asana field structure" has been an open
dependency since early in this build). Basic fields need nothing beyond
an access token and a project GID — genuinely deployable today, without
waiting on that conversation. Upgrading to real custom fields later is an
additive change to build_task_payload() in asana_sync.py, not a redesign
of this client.

No real Asana account has been available to test this against — every
test here mocks the HTTP layer directly. That's an honest limit, not a
claim this has been proven against a live workspace.
"""
from typing import Dict, Optional

import requests

ASANA_API = "https://app.asana.com/api/1.0"


class AsanaError(Exception):
    pass


class AsanaClient:
    def __init__(self, access_token: str, project_gid: str, timeout: int = 15):
        if not access_token:
            raise AsanaError("AsanaClient needs a non-empty access_token.")
        if not project_gid:
            raise AsanaError("AsanaClient needs a non-empty project_gid.")
        self.project_gid = project_gid
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    def create_task(self, name: str, notes: str = "") -> str:
        """Returns the new task's gid."""
        payload = {"data": {"name": name, "notes": notes, "projects": [self.project_gid]}}
        resp = requests.post(f"{ASANA_API}/tasks", json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise AsanaError(f"Failed to create Asana task '{name}': {resp.status_code} {resp.text[:300]}")
        return resp.json()["data"]["gid"]

    def update_task(self, task_gid: str, name: Optional[str] = None, notes: Optional[str] = None) -> None:
        """Only fields actually passed get updated — None means "leave
        this field alone", not "clear it"."""
        fields: Dict[str, str] = {}
        if name is not None:
            fields["name"] = name
        if notes is not None:
            fields["notes"] = notes
        if not fields:
            return
        resp = requests.put(f"{ASANA_API}/tasks/{task_gid}", json={"data": fields},
                             headers=self._headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise AsanaError(f"Failed to update Asana task {task_gid}: {resp.status_code} {resp.text[:300]}")

    def task_exists(self, task_gid: str) -> bool:
        """Used before attempting an update — a task_id stored on the
        Sheet could point at a task someone deleted directly in Asana
        since the last sync. Returns False (not an error) for a 404,
        since "doesn't exist anymore" is an expected, handleable outcome,
        not a failure of this call itself."""
        resp = requests.get(f"{ASANA_API}/tasks/{task_gid}", headers=self._headers, timeout=self.timeout)
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise AsanaError(f"Failed to check Asana task {task_gid}: {resp.status_code} {resp.text[:300]}")
        return True
