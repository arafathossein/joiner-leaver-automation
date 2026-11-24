"""An in-memory directory for the demo and tests. Loads and saves a JSON file."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from ..models import Action, UserState

ALL = {"create_user", "update_profile", "set_manager", "add_group", "remove_group", "add_licence", "issue_credential",
       "block_sign_in", "revoke_sessions", "set_auto_reply"}


class MemoryDirectory:
    name = "demo directory"
    supports = ALL

    def __init__(self, users: dict | None = None, path: Path | None = None):
        self.users: dict[str, dict] = users or {}
        self.path = path
        self.calls: list[tuple[str, str]] = []

    @classmethod
    def load(cls, path: str | Path) -> "MemoryDirectory":
        p = Path(path)
        return cls(json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}, p)

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.users, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def canon(name: str) -> str:
        return name.strip().lower()

    def lookup(self, email: str) -> UserState | None:
        u = self.users.get(email)
        if not u:
            return None
        return UserState(u["id"], email, u.get("enabled", True), {g: f"grp-{g}" for g in u.get("groups", [])},
                         set(u.get("licences", [])), list(u.get("admin_roles", [])), u.get("department", ""),
                         u.get("title", ""), u.get("manager", ""))

    def run(self, a: Action, user_id: str | None, email: str):
        self.calls.append((a.kind, email))
        p = a.params
        if a.kind == "create_user":
            uid = f"u-{len(self.users) + 1:04d}"
            self.users[email] = {"id": uid, "enabled": True, "groups": [], "licences": [], "department": p["department"],
                                 "title": p["title"], "manager": "", "name": p["display_name"]}
            return uid, f"created with id {uid}", None
        u = self.users[email]
        if a.kind == "update_profile":
            u.update({k: v for k, v in p.items() if k in ("department", "title")})
        elif a.kind == "set_manager":
            u["manager"] = p["manager"]
        elif a.kind == "add_group":
            if p["group"] not in u["groups"]:
                u["groups"].append(p["group"])
        elif a.kind == "remove_group":
            u["groups"] = [g for g in u["groups"] if g != p["group"]]
        elif a.kind == "add_licence":
            if p["licence"] not in u["licences"]:
                u["licences"].append(p["licence"])
        elif a.kind == "issue_credential":
            return u["id"], f"{p['method']} issued", secrets.token_urlsafe(12)
        elif a.kind == "block_sign_in":
            u["enabled"] = False
        elif a.kind == "revoke_sessions":
            u["sessions_revoked"] = True
        elif a.kind == "set_auto_reply":
            u["auto_reply"] = p["text"]
        return u["id"], "done", None
