"""HR events and the role map.

events.csv (or a JSON list with the same keys):

    kind,employee_id,email,first_name,last_name,department,title,manager_email,effective_date
    joiner,E1042,sara.khan@example.com,Sara,Khan,Finance,Accounts Payable Clerk,omar.ali@example.com,2026-11-30

roles.yaml says which groups and licences each role gets. See samples/roles.yaml.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .models import KINDS, Event

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _date(v: str | None, where: str) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v.strip())
    except ValueError as exc:
        raise ValueError(f"{where}: effective_date '{v}' is not YYYY-MM-DD") from exc


def load_events(path: str | Path) -> list[Event]:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    rows = json.loads(text) if path.suffix.lower() == ".json" else list(csv.DictReader(text.splitlines()))
    events, problems = [], []
    for n, row in enumerate(rows, 2 if path.suffix.lower() != ".json" else 1):
        row = {(k or "").strip().lower(): (str(v).strip() if v is not None else "") for k, v in row.items()}
        where = f"{path.name} row {n}"
        kind = row.get("kind", "").lower()
        email = row.get("email", "").lower()
        if kind not in KINDS:
            problems.append(f"{where}: kind '{kind}' must be one of {', '.join(KINDS)}")
            continue
        if not _EMAIL.match(email):
            problems.append(f"{where}: '{email}' is not an email address")
            continue
        mgr = row.get("manager_email", "").lower()
        if mgr and not _EMAIL.match(mgr):
            problems.append(f"{where}: manager '{mgr}' is not an email address")
            continue
        if kind == "joiner" and not (row.get("first_name") and row.get("last_name")):
            problems.append(f"{where}: a joiner needs first_name and last_name")
            continue
        try:
            eff = _date(row.get("effective_date"), where)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        events.append(Event(kind, row.get("employee_id", ""), email, row.get("first_name", ""), row.get("last_name", ""),
                            row.get("department", ""), row.get("title", ""), mgr, eff, path.name))
    if problems:
        raise ValueError("\n".join(problems))
    return events


@dataclass
class Rule:
    department: str = "*"
    title: str = "*"
    groups: list[str] = field(default_factory=list)
    licences: list[str] = field(default_factory=list)
    review: bool = False  # privileged role: a person approves before changes are applied

    def matches(self, department: str, title: str) -> bool:
        return (fnmatch.fnmatch(department.lower(), self.department.lower())
                and fnmatch.fnmatch(title.lower(), self.title.lower()))


@dataclass
class RoleMap:
    domain: str
    baseline_groups: list[str]
    baseline_licences: list[str]
    rules: list[Rule]
    protected: set[str]
    credential: str = "tap"  # "tap" (Entra Temporary Access Pass) or "password"
    usage_location: str = "US"
    auto_reply: bool = True
    auto_reply_text: str = "{name} has left {company}. Please contact {manager} instead."
    company: str = "the company"
    delete_after_days: int = 90
    licence_removal_after_days: int = 30

    @classmethod
    def load(cls, path: str | Path) -> "RoleMap":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        base = data.get("baseline", {}) or {}
        rules = []
        for i, r in enumerate(data.get("roles", []) or [], 1):
            m = r.get("match", {}) or {}
            unknown = set(m) - {"department", "title"}
            if unknown:
                raise ValueError(f"roles[{i}]: unknown match keys {sorted(unknown)}; use department and title")
            rules.append(Rule(m.get("department", "*"), m.get("title", "*"), list(r.get("groups", []) or []),
                              list(r.get("licences", []) or []), bool(r.get("review", False))))
        leaver = data.get("leaver", {}) or {}
        cred = data.get("credential", "tap")
        if cred not in ("tap", "password"):
            raise ValueError("credential must be 'tap' or 'password'")
        return cls(
            domain=data.get("domain", ""),
            baseline_groups=list(base.get("groups", []) or []),
            baseline_licences=list(base.get("licences", []) or []),
            rules=rules,
            protected={p.lower() for p in data.get("protected", []) or []},
            credential=cred,
            usage_location=data.get("usage_location", "US"),
            auto_reply=bool(leaver.get("auto_reply", True)),
            auto_reply_text=leaver.get("auto_reply_text", cls.auto_reply_text),
            company=data.get("company", "the company"),
            delete_after_days=int(leaver.get("delete_after_days", 90)),
            licence_removal_after_days=int(leaver.get("licence_removal_after_days", 30)),
        )

    def target(self, department: str, title: str) -> tuple[list[str], list[str], bool]:
        """Groups, licences and whether review is needed for a role."""
        groups, licences, review = list(self.baseline_groups), list(self.baseline_licences), False
        for r in self.rules:
            if r.matches(department, title):
                groups += [g for g in r.groups if g not in groups]
                licences += [x for x in r.licences if x not in licences]
                review = review or r.review
        return groups, licences, review

    @property
    def managed_groups(self) -> set[str]:
        """Groups this tool may remove someone from when they change role. Anything else is left alone."""
        out = set(self.baseline_groups)
        for r in self.rules:
            out |= set(r.groups)
        return out
