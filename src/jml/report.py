"""Plan and evidence reports in Markdown and HTML. Secrets never appear here."""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from .models import Outcome, Plan

ICON = {"joiner": "➕", "mover": "↔️", "leaver": "➖"}


@dataclass
class Entry:
    plan: Plan
    outcomes: list[Outcome] = field(default_factory=list)
    verified: list[str] | None = None  # leaver check after apply: [] means verified


def _status(x: Entry, applied: bool) -> str:
    p = x.plan
    if p.blocked:
        return f"Skipped: {p.blocked}"
    if p.scheduled:
        return f"Scheduled for {p.event.effective_date}"
    if not p.actions:
        return "Nothing to change"
    if not applied:
        return "Needs review before --apply" if p.needs_review else "Ready"
    failed = [o for o in x.outcomes if not o.ok]
    if failed:
        return f"{len(failed)} step(s) failed"
    if x.verified:
        return "Applied, but verification failed"
    return "Applied and verified" if x.verified == [] else "Applied"


def markdown(entries: list[Entry], applied: bool, provider: str) -> str:
    title = "Access changes applied" if applied else "Access change plan (dry run)"
    out = [f"# {title}", "", f"Directory: {provider}", "", "| Event | Person | Effective | Status |", "| --- | --- | --- | --- |"]
    for x in entries:
        e = x.plan.event
        out.append(f"| {ICON[e.kind]} {e.kind} | {e.display_name} ({e.email}) | {e.effective_date or 'now'} | {_status(x, applied)} |")
    for x in entries:
        p, e = x.plan, x.plan.event
        out += ["", f"## {ICON[e.kind]} {e.kind.title()}: {e.display_name}", ""]
        meta = [f"Employee ID {e.employee_id}" if e.employee_id else "", e.department, e.title,
                f"manager {e.manager_email}" if e.manager_email else ""]
        out.append(" · ".join(m for m in meta if m) or e.email)
        out.append("")
        if p.blocked:
            out += [f"**Skipped:** {p.blocked}", ""]
        for w in p.warnings:
            out.append(f"> ⚠️ {w}")
        if p.warnings:
            out.append("")
        done = {id(o.action): o for o in x.outcomes}
        if p.actions:
            out.append("**Steps**" if applied else "**Planned steps**")
            out.append("")
            for a in p.actions:
                o = done.get(id(a))
                mark = "" if o is None else ("✅ " if o.ok else "❌ ")
                tail = f" ({o.at}{': ' + o.detail if o.detail and o.detail != 'done' else ''})" if o else ""
                out.append(f"- {mark}{a.summary}{tail}")
            out.append("")
        if x.verified is not None:
            out += ["**Verification:** " + ("access removal confirmed by reading the account back." if not x.verified
                                            else "; ".join(x.verified)), ""]
        if p.manual:
            out += ["**For a person to do**", ""] + [f"- [ ] {m}" for m in p.manual] + [""]
    return "\n".join(out).rstrip() + "\n"


_CSS = """
:root{--bg:#f6f7f8;--card:#fff;--ink:#15171a;--muted:#5c6168;--line:#e2e4e7;--ok:#1f7a3a;--bad:#b42318;--warn:#8a5a00;--accent:#2457c5}
@media (prefers-color-scheme:dark){:root{--bg:#101215;--card:#181b1f;--ink:#eef0f2;--muted:#a3a8b0;--line:#2b2f35;--ok:#7bd88f;--bad:#ff8b80;--warn:#f2b54a;--accent:#8fb3ff}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0}h2{font-size:18px;margin:0 0 4px}
.muted{color:var(--muted);font-size:13px}.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;margin:20px 0}
table{width:100%;border-collapse:collapse;min-width:640px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500;font-size:13px}tr:last-child td{border-bottom:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-top:16px}
.kind{display:inline-block;font-size:12px;font-weight:600;border:1px solid currentColor;border-radius:999px;padding:0 8px;margin-right:6px;color:var(--accent)}
ul{list-style:none;padding-left:2px;margin:6px 0 12px}li{margin:2px 0}.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 0 4px}
"""


def html_report(entries: list[Entry], applied: bool, provider: str) -> str:
    e_ = html.escape
    title = "Access changes applied" if applied else "Access change plan (dry run)"
    rows = "".join(f"<tr><td><span class=kind>{x.plan.event.kind}</span></td><td>{e_(x.plan.event.display_name)}<br>"
                   f"<span class=muted>{e_(x.plan.event.email)}</span></td><td>{e_(str(x.plan.event.effective_date or 'now'))}</td>"
                   f"<td>{e_(_status(x, applied))}</td></tr>" for x in entries)
    cards = []
    for x in entries:
        p, e = x.plan, x.plan.event
        meta = " · ".join(m for m in [e.employee_id, e.department, e.title, f"manager {e.manager_email}" if e.manager_email else ""] if m)
        parts = [f"<section class=card><h2><span class=kind>{e.kind}</span>{e_(e.display_name)}</h2><p class=muted>{e_(meta or e.email)}</p>"]
        if p.blocked:
            parts.append(f"<p class=warn><strong>Skipped:</strong> {e_(p.blocked)}</p>")
        parts += [f"<p class=warn>⚠ {e_(w)}</p>" for w in p.warnings]
        done = {id(o.action): o for o in x.outcomes}
        if p.actions:
            items = []
            for a in p.actions:
                o = done.get(id(a))
                cls, mark = ("", "") if o is None else (("ok", "✓ ") if o.ok else ("bad", "✗ "))
                tail = f' <span class=muted>{e_(o.at)}{(" · " + e_(o.detail)) if o.detail and o.detail != "done" else ""}</span>' if o else ""
                items.append(f'<li class="{cls}">{mark}{e_(a.summary)}{tail}</li>')
            parts.append(f"<h3>{'Steps' if applied else 'Planned steps'}</h3><ul>{''.join(items)}</ul>")
        if x.verified is not None:
            parts.append(f"<p class={'ok' if not x.verified else 'bad'}><strong>Verification:</strong> "
                         f"{'access removal confirmed by reading the account back.' if not x.verified else e_('; '.join(x.verified))}</p>")
        if p.manual:
            parts.append("<h3>For a person to do</h3><ul>" + "".join(f"<li>☐ {e_(m)}</li>" for m in p.manual) + "</ul>")
        parts.append("</section>")
        cards.append("".join(parts))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e_(title)}</title><style>{_CSS}</style></head><body><main><h1>{e_(title)}</h1>"
            f'<p class=muted>Directory: {e_(provider)}</p><div class=wrap><table><thead><tr><th>Event</th><th>Person</th>'
            f"<th>Effective</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>{''.join(cards)}</main></body></html>\n")
