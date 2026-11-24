"""Directory providers. Each one can read a user's state and carry out the plan's actions."""

from __future__ import annotations

from typing import Protocol

from ..models import Action, UserState


class Provider(Protocol):
    name: str
    supports: set[str]  # action kinds this provider can do; others become manual steps

    def lookup(self, email: str) -> UserState | None: ...

    def run(self, action: Action, user_id: str | None, email: str) -> tuple[str | None, str, str | None]:
        """Do one action. Returns (user_id, human detail, secret or None).

        The secret (a temporary password or pass) is never logged; the caller
        writes it to a private file.
        """
