from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

KINDS = ("joiner", "mover", "leaver")


@dataclass
class Event:
    """One HR event: someone starts, changes role, or leaves."""

    kind: str
    employee_id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    department: str = ""
    title: str = ""
    manager_email: str = ""
    effective_date: date | None = None
    source: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email


@dataclass
class UserState:
    """What the directory says about a user right now."""

    id: str
    email: str
    enabled: bool
    groups: dict[str, str] = field(default_factory=dict)  # group name -> group id
    licences: set[str] = field(default_factory=set)  # SKU part numbers
    admin_roles: list[str] = field(default_factory=list)
    department: str = ""
    title: str = ""
    manager_email: str = ""


@dataclass
class Action:
    """One change to make. Every action is safe to run twice."""

    kind: str  # create_user, update_profile, set_manager, add_group, remove_group, add_licence, issue_credential,
    #             block_sign_in, revoke_sessions, set_auto_reply
    summary: str
    params: dict = field(default_factory=dict)


@dataclass
class Plan:
    event: Event
    actions: list[Action] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)  # steps a person must do
    warnings: list[str] = field(default_factory=list)
    blocked: str | None = None  # why nothing will be done
    needs_review: bool = False  # a person should approve before --apply
    scheduled: bool = False  # effective date is in the future
    before: UserState | None = None

    @property
    def actionable(self) -> bool:
        return not self.blocked and not self.scheduled and bool(self.actions)


@dataclass
class Outcome:
    action: Action
    ok: bool
    at: str  # ISO timestamp, UTC
    detail: str = ""
