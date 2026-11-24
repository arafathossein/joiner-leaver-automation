"""Carrying out a plan: one action at a time, every step logged, secrets kept out of logs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .models import Outcome, Plan
from .providers import Provider


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    """Append-only JSON lines: who, what, when, result. Never contains a password or pass."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **record) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save_secret(folder: Path, email: str, method: str, secret: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    p = folder / f"{email}.txt"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        label = "Temporary Access Pass (valid 8 hours)" if method == "tap" else "Temporary password (must be changed at first sign-in)"
        f.write(f"{email}\n{label}: {secret}\nDeliver through a separate channel, then delete this file.\n")
    return p


def apply(plan: Plan, provider: Provider, audit: AuditLog, secrets_dir: Path, actor: str = "jml") -> list[Outcome]:
    e = plan.event
    uid = plan.before.id if plan.before else None
    out: list[Outcome] = []
    for a in plan.actions:
        at = _now()
        try:
            uid, detail, secret = provider.run(a, uid, e.email)
            if secret:
                path = _save_secret(secrets_dir, e.email, a.params.get("method", "password"), secret)
                detail = f"{detail}; saved to {path} (readable only by you)"
            out.append(Outcome(a, True, at, detail))
        except Exception as exc:  # noqa: BLE001 - record and decide whether to continue
            out.append(Outcome(a, False, at, str(exc)))
            audit.write(at=at, actor=actor, provider=provider.name, event=e.kind, employee_id=e.employee_id, user=e.email,
                        action=a.kind, summary=a.summary, ok=False, detail=str(exc))
            if a.kind == "create_user":  # nothing else can work without the account
                break
            continue
        audit.write(at=at, actor=actor, provider=provider.name, event=e.kind, employee_id=e.employee_id, user=e.email,
                    action=a.kind, summary=a.summary, ok=True, detail=out[-1].detail)
    return out


def verify_leaver(plan: Plan, provider: Provider) -> list[str]:
    """Read the account back and confirm access is really gone. Returns problems; empty means verified."""
    after = provider.lookup(plan.event.email)
    if after is None:
        return []
    problems = []
    if after.enabled:
        problems.append("sign-in is still enabled")
    if after.groups:
        problems.append("still in groups: " + ", ".join(sorted(after.groups)))
    return problems
