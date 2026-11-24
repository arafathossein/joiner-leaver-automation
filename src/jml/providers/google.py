"""Google Workspace through the Admin SDK Directory API.

Uses a service account with domain-wide delegation, acting as an admin:
    scopes: https://www.googleapis.com/auth/admin.directory.user
            https://www.googleapis.com/auth/admin.directory.group.member
    pip install "jml[google]"

Groups in the role map are group email addresses; a bare name like "finance"
becomes finance@<domain>. Licences and Gmail auto-replies are not changed
through this API, so they appear in the plan as manual steps.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from ..models import Action, UserState
from .graph import _password

DIRECTORY = "https://admin.googleapis.com/admin/directory/v1"
SCOPES = ["https://www.googleapis.com/auth/admin.directory.user",
          "https://www.googleapis.com/auth/admin.directory.group.member"]


class GoogleError(RuntimeError):
    pass


def service_account_token(key_file: str, admin_email: str) -> Callable[[], str]:  # pragma: no cover - needs Google
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES, subject=admin_email)

    def token() -> str:
        if not creds.valid:
            creds.refresh(Request())
        return creds.token

    return token


class GoogleDirectory:
    name = "Google Workspace"
    supports = {"create_user", "update_profile", "set_manager", "add_group", "remove_group", "issue_credential",
                "block_sign_in", "revoke_sessions"}

    def __init__(self, token: Callable[[], str], domain: str, client: httpx.Client | None = None, base: str = DIRECTORY,
                 org_unit: str = "/"):
        self.token, self.domain, self.base, self.org_unit = token, domain, base, org_unit
        self.http = client or httpx.Client(timeout=30)

    def _req(self, method: str, path: str, ok=(200, 204), **kw) -> httpx.Response:
        for attempt in range(4):
            r = self.http.request(method, f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token()}"}, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            break
        if r.status_code not in ok:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise GoogleError(f"{method} {path} failed ({r.status_code}): {msg}")
        return r

    def _group(self, name: str) -> str:
        return (name if "@" in name else f"{name.strip().lower().replace(' ', '-')}@{self.domain}").lower()

    canon = _group

    def lookup(self, email: str) -> UserState | None:
        r = self._req("GET", f"/users/{email}", ok=(200, 404))
        if r.status_code == 404:
            return None
        u = r.json()
        groups, token = {}, None
        while True:
            params = {"userKey": email, **({"pageToken": token} if token else {})}
            body = self._req("GET", "/groups", params=params).json()
            for g in body.get("groups", []):
                groups[g["email"]] = g["id"]
            token = body.get("nextPageToken")
            if not token:
                break
        org = next((o for o in u.get("organizations", []) if o.get("primary")), (u.get("organizations") or [{}])[0])
        mgr = next((x.get("value", "") for x in u.get("relations", []) if x.get("type") == "manager"), "")
        roles = ["Super Admin"] if u.get("isAdmin") else (["Delegated admin"] if u.get("isDelegatedAdmin") else [])
        return UserState(u["id"], email, not u.get("suspended", False), groups, set(), roles,
                         org.get("department", ""), org.get("title", ""), mgr.lower())

    def run(self, a: Action, user_id: str | None, email: str):
        p = a.params
        if a.kind == "create_user":
            body = {"primaryEmail": email, "name": {"givenName": p["first_name"], "familyName": p["last_name"]},
                    "password": _password(), "changePasswordAtNextLogin": True, "orgUnitPath": self.org_unit,
                    "organizations": [{"department": p["department"], "title": p["title"], "primary": True}],
                    "externalIds": [{"value": p["employee_id"], "type": "organization"}] if p["employee_id"] else []}
            uid = self._req("POST", "/users", json=body).json()["id"]
            return uid, f"created with id {uid}", None
        if a.kind == "update_profile":
            self._req("PATCH", f"/users/{email}", json={"organizations": [
                {"department": p.get("department", ""), "title": p.get("title", ""), "primary": True}]})
        elif a.kind == "set_manager":
            self._req("PATCH", f"/users/{email}", json={"relations": [{"type": "manager", "value": p["manager"]}]})
        elif a.kind == "add_group":
            r = self._req("POST", f"/groups/{self._group(p['group'])}/members", ok=(200, 409),
                          json={"email": email, "role": "MEMBER"})
            return user_id, "already a member" if r.status_code == 409 else "done", None
        elif a.kind == "remove_group":
            self._req("DELETE", f"/groups/{self._group(p['group'])}/members/{email}", ok=(204, 404))
        elif a.kind == "issue_credential":
            pw = _password()
            self._req("PATCH", f"/users/{email}", json={"password": pw, "changePasswordAtNextLogin": True})
            return user_id, "temporary password set; change required at first sign-in", pw
        elif a.kind == "block_sign_in":
            self._req("PATCH", f"/users/{email}", json={"suspended": True})
        elif a.kind == "revoke_sessions":
            self._req("POST", f"/users/{email}/signOut")
        else:
            raise GoogleError(f"unsupported action {a.kind}")
        return user_id, "done", None
