"""Microsoft 365 / Entra ID through Microsoft Graph (v1.0), with app-only credentials.

App permissions this needs (admin consent):
    User.ReadWrite.All, Group.ReadWrite.All (or GroupMember.ReadWrite.All),
    Directory.Read.All (to see admin roles), MailboxSettings.ReadWrite (auto-reply),
    UserAuthenticationMethod.ReadWrite.All (Temporary Access Pass)
Licences are assigned by SKU part number (for example SPB), resolved through
/subscribedSkus, so the role map never needs GUIDs.
"""

from __future__ import annotations

import secrets
import string
import time

import httpx

from ..models import Action, UserState

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphError(RuntimeError):
    pass


class GraphDirectory:
    name = "Microsoft Graph"
    supports = {"create_user", "update_profile", "set_manager", "add_group", "remove_group", "add_licence",
                "issue_credential", "block_sign_in", "revoke_sessions", "set_auto_reply"}

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, *, credential: str = "tap",
                 client: httpx.Client | None = None, base: str = GRAPH,
                 login: str = "https://login.microsoftonline.com"):
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.credential = credential
        self.http = client or httpx.Client(timeout=30)
        self.base, self.login = base, login
        self._token: tuple[str, float] | None = None
        self._groups: dict[str, str] = {}
        self._skus: dict[str, str] | None = None
        self._users: dict[str, str] = {}

    # ---------- plumbing ----------

    def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        r = self.http.post(f"{self.login}/{self.tenant_id}/oauth2/v2.0/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"})
        if r.status_code != 200:
            raise GraphError(f"sign-in to Entra ID failed ({r.status_code}); check tenant, client id and secret")
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _req(self, method: str, path: str, ok=(200, 201, 204), **kw) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(4):
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self._auth()}"}, **kw)
            if r.status_code == 429 or r.status_code >= 500:  # throttled or transient: back off and retry
                time.sleep(min(int(r.headers.get("Retry-After", 2 ** attempt)), 30))
                continue
            break
        if r.status_code not in ok:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise GraphError(f"{method} {path.split('?')[0]} failed ({r.status_code}): {msg}")
        return r

    def _pages(self, path: str) -> list[dict]:
        out, url = [], path
        while url:
            body = self._req("GET", url).json()
            out += body.get("value", [])
            url = body.get("@odata.nextLink")
        return out

    def _group_id(self, name: str) -> str:
        if name not in self._groups:
            safe = name.replace("'", "''")
            found = self._req("GET", "/groups", params={"$filter": f"displayName eq '{safe}'", "$select": "id,displayName"}).json()
            vals = found.get("value", [])
            if len(vals) != 1:
                raise GraphError(f"group '{name}': expected exactly one match, found {len(vals)}")
            self._groups[name] = vals[0]["id"]
        return self._groups[name]

    def _sku_id(self, part: str) -> str:
        if self._skus is None:
            self._skus = {s["skuPartNumber"]: s["skuId"] for s in self._pages("/subscribedSkus")}
        if part not in self._skus:
            raise GraphError(f"licence '{part}' is not in this tenant's subscriptions ({', '.join(sorted(self._skus))})")
        return self._skus[part]

    def _user_id(self, email: str) -> str:
        if email not in self._users:
            r = self._req("GET", f"/users/{email}", ok=(200, 404), params={"$select": "id"})
            if r.status_code == 404:
                raise GraphError(f"user {email} not found")
            self._users[email] = r.json()["id"]
        return self._users[email]

    # ---------- provider interface ----------

    @staticmethod
    def canon(name: str) -> str:
        return name.strip().lower()

    def lookup(self, email: str) -> UserState | None:
        r = self._req("GET", f"/users/{email}", ok=(200, 404),
                      params={"$select": "id,accountEnabled,userPrincipalName,department,jobTitle"})
        if r.status_code == 404:
            return None
        u = r.json()
        self._users[email] = u["id"]
        groups, roles = {}, []
        for m in self._pages(f"/users/{u['id']}/memberOf?$select=id,displayName"):
            t = m.get("@odata.type", "")
            if t.endswith("directoryRole"):
                roles.append(m.get("displayName", "role"))
            elif t.endswith("group"):
                groups[m.get("displayName", m["id"])] = m["id"]
                self._groups.setdefault(m.get("displayName", m["id"]), m["id"])
        lic = {x["skuPartNumber"] for x in self._pages(f"/users/{u['id']}/licenseDetails")}
        mgr = self._req("GET", f"/users/{u['id']}/manager", ok=(200, 404), params={"$select": "userPrincipalName"})
        manager = mgr.json().get("userPrincipalName", "").lower() if mgr.status_code == 200 else ""
        return UserState(u["id"], email, bool(u.get("accountEnabled")), groups, lic, roles,
                         u.get("department") or "", u.get("jobTitle") or "", manager)

    def run(self, a: Action, user_id: str | None, email: str):
        p = a.params
        if a.kind == "create_user":
            body = {
                "accountEnabled": True, "displayName": p["display_name"], "givenName": p["first_name"], "surname": p["last_name"],
                "mailNickname": email.split("@")[0].replace(".", "")[:64], "userPrincipalName": email,
                "department": p["department"] or None, "jobTitle": p["title"] or None,
                "employeeId": p["employee_id"] or None, "usageLocation": p["usage_location"],
                # Required by Graph even when a Temporary Access Pass is issued; never shown or stored.
                "passwordProfile": {"forceChangePasswordNextSignIn": True, "password": _password()},
            }
            uid = self._req("POST", "/users", json={k: v for k, v in body.items() if v is not None}).json()["id"]
            self._users[email] = uid
            return uid, f"created with id {uid}", None
        uid = user_id or self._user_id(email)
        if a.kind == "update_profile":
            body = {"department": p.get("department"), "jobTitle": p.get("title")}
            self._req("PATCH", f"/users/{uid}", json={k: v for k, v in body.items() if v})
        elif a.kind == "set_manager":
            mid = self._user_id(p["manager"])
            self._req("PUT", f"/users/{uid}/manager/$ref", json={"@odata.id": f"{GRAPH}/users/{mid}"})
        elif a.kind == "add_group":
            gid = self._group_id(p["group"])
            r = self._req("POST", f"/groups/{gid}/members/$ref", ok=(204, 400),
                          json={"@odata.id": f"{GRAPH}/directoryObjects/{uid}"})
            if r.status_code == 400 and "already exist" not in r.text:
                raise GraphError(f"add to {p['group']} failed: {r.text[:200]}")
        elif a.kind == "remove_group":
            gid = p.get("group_id") or self._group_id(p["group"])
            self._req("DELETE", f"/groups/{gid}/members/{uid}/$ref", ok=(204, 404))
        elif a.kind == "add_licence":
            self._req("POST", f"/users/{uid}/assignLicense",
                      json={"addLicenses": [{"skuId": self._sku_id(p["licence"]), "disabledPlans": []}], "removeLicenses": []})
        elif a.kind == "issue_credential":
            if p.get("method") == "password":
                pw = _password()
                self._req("PATCH", f"/users/{uid}",
                          json={"passwordProfile": {"forceChangePasswordNextSignIn": True, "password": pw}})
                return uid, "temporary password set; change required at first sign-in", pw
            body = self._req("POST", f"/users/{uid}/authentication/temporaryAccessPassMethods",
                             json={"lifetimeInMinutes": 480, "isUsableOnce": False}).json()
            return uid, "Temporary Access Pass issued, valid 8 hours", body.get("temporaryAccessPass")
        elif a.kind == "block_sign_in":
            self._req("PATCH", f"/users/{uid}", json={"accountEnabled": False})
        elif a.kind == "revoke_sessions":
            self._req("POST", f"/users/{uid}/revokeSignInSessions")
        elif a.kind == "set_auto_reply":
            self._req("PATCH", f"/users/{uid}/mailboxSettings", json={"automaticRepliesSetting": {
                "status": "alwaysEnabled", "internalReplyMessage": p["text"], "externalReplyMessage": p["text"],
                "externalAudience": "all"}})
        else:
            raise GraphError(f"unsupported action {a.kind}")
        return uid, "done", None


def _password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^*-_=+"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(not c.isalnum() for c in pw)):
            return pw
