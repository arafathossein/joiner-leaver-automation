"""Turn an HR event plus the directory's current state into a plan.

Plans are computed against what is already there, so running the same
event twice produces an empty second plan: nothing is created twice and
nobody is added to a group they're already in.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from .config import RoleMap
from .models import Action, Event, Plan, UserState


Canon = Callable[[str], str]


def _same(name: str) -> str:
    return name


def _joiner(e: Event, cur: UserState | None, rm: RoleMap, plan: Plan, canon: Canon) -> None:
    groups, licences, review = rm.target(e.department, e.title)
    plan.needs_review = review
    if cur is None:
        plan.actions.append(Action("create_user", f"Create {e.email} ({e.display_name})", {
            "email": e.email, "first_name": e.first_name, "last_name": e.last_name, "display_name": e.display_name,
            "department": e.department, "title": e.title, "employee_id": e.employee_id, "usage_location": rm.usage_location}))
        have_groups, have_lic = set(), set()
    else:
        if not cur.enabled:
            plan.warnings.append(f"{e.email} already exists and is disabled (a returning employee?). Re-enable it by hand "
                                 "after checking why it was disabled; this plan won't.")
            plan.blocked = "account exists and is disabled"
            return
        plan.warnings.append(f"{e.email} already exists; only missing groups and licences will be added.")
        have_groups, have_lic = {canon(g) for g in cur.groups}, set(cur.licences)
    if e.manager_email and (cur is None or cur.manager_email != e.manager_email):
        plan.actions.append(Action("set_manager", f"Set manager to {e.manager_email}", {"manager": e.manager_email}))
    for lic in licences:
        if lic not in have_lic:
            plan.actions.append(Action("add_licence", f"Assign licence {lic}", {"licence": lic}))
    for g in groups:
        if canon(g) not in have_groups:
            plan.actions.append(Action("add_group", f"Add to {g}", {"group": g}))
    if cur is None:
        kind = "Temporary Access Pass" if rm.credential == "tap" else "temporary password (change at first sign-in)"
        plan.actions.append(Action("issue_credential", f"Issue a {kind}", {"method": rm.credential}))
        plan.manual += [
            "Give the sign-in credential to the manager through a separate channel, then delete the secrets file.",
            "Prepare the laptop and confirm the user enrols MFA on day one.",
        ]
    if review:
        plan.manual.append("This role is marked for review: a second person approves the access before --apply.")


def _mover(e: Event, cur: UserState | None, rm: RoleMap, plan: Plan, canon: Canon) -> None:
    if cur is None:
        plan.blocked = "no account found to move"
        return
    if not cur.enabled:
        plan.blocked = "account is disabled"
        return
    groups, licences, review = rm.target(e.department, e.title)
    plan.needs_review = review
    changes = {}
    if e.department and e.department != cur.department:
        changes["department"] = e.department
    if e.title and e.title != cur.title:
        changes["title"] = e.title
    if changes:
        plan.actions.append(Action("update_profile", "Update " + ", ".join(f"{k} to '{v}'" for k, v in changes.items()), changes))
    if e.manager_email and e.manager_email != cur.manager_email:
        plan.actions.append(Action("set_manager", f"Set manager to {e.manager_email}", {"manager": e.manager_email}))
    for lic in licences:
        if lic not in cur.licences:
            plan.actions.append(Action("add_licence", f"Assign licence {lic}", {"licence": lic}))
    have = {canon(g) for g in cur.groups}
    want = {canon(g) for g in groups}
    for g in groups:
        if canon(g) not in have:
            plan.actions.append(Action("add_group", f"Add to {g}", {"group": g}))
    managed = {canon(g) for g in rm.managed_groups}
    for g, gid in sorted(cur.groups.items()):
        if canon(g) in managed and canon(g) not in want:
            plan.actions.append(Action("remove_group", f"Remove from {g} (not part of the new role)", {"group": g, "group_id": gid}))
    unmanaged = sorted(g for g in cur.groups if canon(g) not in managed)
    if unmanaged:
        plan.manual.append("Check groups outside the role map, which this tool leaves alone: " + ", ".join(unmanaged))
    if cur.admin_roles:
        plan.warnings.append(f"Holds admin roles: {', '.join(cur.admin_roles)}. Confirm they still fit the new job.")
        plan.needs_review = True


def _leaver(e: Event, cur: UserState | None, rm: RoleMap, plan: Plan, canon: Canon) -> None:
    if cur is None:
        plan.blocked = "no account found"
        return
    if not cur.enabled and not cur.groups:
        plan.warnings.append("Already offboarded: sign-in is blocked and the account is in no groups.")
        return
    if cur.enabled:
        plan.actions.append(Action("block_sign_in", "Block sign-in", {}))
    plan.actions.append(Action("revoke_sessions", "Sign out of every session and revoke refresh tokens", {}))
    for g, gid in sorted(cur.groups.items()):
        plan.actions.append(Action("remove_group", f"Remove from {g}", {"group": g, "group_id": gid}))
    if rm.auto_reply and cur.enabled:  # set once, when the account is first blocked
        mgr = e.manager_email or cur.manager_email or "your usual contact"
        text = rm.auto_reply_text.format(name=e.display_name if e.first_name else cur.email, company=rm.company, manager=mgr)
        plan.actions.append(Action("set_auto_reply", "Set an automatic reply pointing to the manager", {"text": text}))
    if cur.admin_roles:
        plan.warnings.append(f"Had admin roles: {', '.join(cur.admin_roles)}. Remove them and rotate any shared "
                             "credentials or API keys this person knew.")
    plan.manual += [
        "Collect the laptop, phone and any tokens or keycards.",
        "Transfer ownership of files and shared mailboxes the manager needs.",
        f"Remove licences after {rm.licence_removal_after_days} days, once mail and files are handed over.",
        f"Delete the account after {rm.delete_after_days} days, if your retention policy allows. This tool never deletes accounts.",
        "Check apps that don't use single sign-on (VPN, firewall admin, cloud consoles, finance systems).",
    ]


def build(e: Event, cur: UserState | None, rm: RoleMap, today: date | None = None, canon: Canon = _same,
          supports: set[str] | None = None) -> Plan:
    """canon maps a group name to the form the directory uses (Google: an email address).
    supports lists the actions the provider can do; anything else becomes a manual step."""
    plan = Plan(event=e, before=cur)
    if e.email in rm.protected:
        plan.blocked = "protected account (listed under 'protected' in the role map)"
        return plan
    if rm.domain and not e.email.endswith("@" + rm.domain):
        plan.blocked = f"{e.email} is outside {rm.domain}"
        return plan
    {"joiner": _joiner, "mover": _mover, "leaver": _leaver}[e.kind](e, cur, rm, plan, canon)
    if supports is not None:
        for a in [a for a in plan.actions if a.kind not in supports]:
            plan.actions.remove(a)
            plan.manual.insert(0, f"Do by hand (not available through this directory's API): {a.summary}.")
    today = today or date.today()
    # Joiners can be prepared in advance; leavers and movers wait for their date so access isn't cut early.
    if e.effective_date and e.effective_date > today and e.kind in ("leaver", "mover"):
        plan.scheduled = True
    return plan
