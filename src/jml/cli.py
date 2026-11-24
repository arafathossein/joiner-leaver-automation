"""Command line.

    jml plan  events.csv --roles roles.yaml --provider graph     # dry run: what would change
    jml apply events.csv --roles roles.yaml --provider graph     # make the changes
    jml demo                                                      # sample events against a demo directory

Nothing changes without `apply`. Plans marked for review need --approved-by.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

from .config import RoleMap, load_events
from .executor import AuditLog, apply, verify_leaver
from .planner import build
from .providers.memory import MemoryDirectory
from .report import Entry, html_report, markdown


def make_provider(kind: str, rm: RoleMap, directory_file: str | None):
    if kind == "graph":
        from .providers.graph import GraphDirectory

        missing = [v for v in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET") if not os.getenv(v)]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} (an Entra ID app registration with the permissions in the README)")
        return GraphDirectory(os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"], os.environ["GRAPH_CLIENT_SECRET"],
                              credential=rm.credential)
    if kind == "google":
        from .providers.google import GoogleDirectory, service_account_token

        missing = [v for v in ("GOOGLE_SA_KEY_FILE", "GOOGLE_ADMIN_EMAIL") if not os.getenv(v)]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} (a service account with domain-wide delegation)")
        return GoogleDirectory(service_account_token(os.environ["GOOGLE_SA_KEY_FILE"], os.environ["GOOGLE_ADMIN_EMAIL"]),
                               rm.domain, org_unit=os.getenv("GOOGLE_ORG_UNIT", "/"))
    if kind == "file":
        if not directory_file:
            raise RuntimeError("--directory-file is required with --provider file")
        return MemoryDirectory.load(directory_file)
    raise RuntimeError(f"unknown provider {kind}")


def run(events, rm: RoleMap, provider, do_apply: bool, approved_by: str | None, out: Path, today: date | None = None,
        actor: str = "jml") -> tuple[list[Entry], int]:
    entries: list[Entry] = []
    audit = AuditLog(out / "audit.jsonl")
    failures = 0
    for e in events:
        plan = build(e, provider.lookup(e.email), rm, today, canon=provider.canon, supports=provider.supports)
        x = Entry(plan)
        if do_apply and plan.actionable:
            if plan.needs_review and not approved_by:
                plan.warnings.append("Not applied: this change needs review. Re-run with --approved-by NAME after a second person checks it.")
            else:
                if plan.needs_review:
                    plan.warnings.append(f"Approved by {approved_by}.")
                x.outcomes = apply(plan, provider, audit, out / "secrets", actor=approved_by or actor)
                failures += sum(not o.ok for o in x.outcomes)
                if e.kind == "leaver" and all(o.ok for o in x.outcomes):
                    x.verified = verify_leaver(plan, provider)
                    failures += len(x.verified)
        entries.append(x)
    return entries, failures


def _write(entries, applied: bool, provider_name: str, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    name = "applied" if applied else "plan"
    (out / f"{name}.md").write_text(markdown(entries, applied, provider_name), encoding="utf-8")
    (out / f"{name}.html").write_text(html_report(entries, applied, provider_name), encoding="utf-8")
    return out / f"{name}.html"


def _print(entries, applied: bool) -> None:
    from .report import _status

    for x in entries:
        e = x.plan.event
        print(f"  {e.kind:<7} {e.email:<34} {len(x.plan.actions):>2} steps   {_status(x, applied)}")


def cmd(a: argparse.Namespace, do_apply: bool) -> int:
    try:
        rm = RoleMap.load(a.roles)
        events = load_events(a.events)
        provider = make_provider(a.provider, rm, a.directory_file)
        entries, failures = run(events, rm, provider, do_apply, a.approved_by, Path(a.out))
    except Exception as exc:  # noqa: BLE001 - report input and connection problems plainly
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if do_apply and hasattr(provider, "save"):
        provider.save()
    _print(entries, do_apply)
    print(f"\nReport: {_write(entries, do_apply, provider.name, Path(a.out))}")
    if do_apply:
        print(f"Audit log: {Path(a.out) / 'audit.jsonl'}")
    return 2 if failures else 0


def cmd_demo(a: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[2] / "samples"
    if not root.exists():
        print("samples/ not found; run the demo from a clone of the repository.", file=sys.stderr)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="jml-demo-"))
    shutil.copy(root / "directory.json", tmp / "directory.json")
    rm = RoleMap.load(root / "roles.yaml")
    events = load_events(root / "events.csv")
    out = Path(a.out)
    today = date(2026, 11, 30)  # fixed so the sample dates read the same every time
    d = MemoryDirectory.load(tmp / "directory.json")
    plan_entries, _ = run(events, rm, d, False, None, out, today)
    print("Dry run:")
    _print(plan_entries, False)
    _write(plan_entries, False, d.name, out)
    applied, _ = run(events, rm, d, True, "demo reviewer", out, today, actor="demo")
    print("\nApplied (demo directory, with review approval):")
    _print(applied, True)
    again, _ = run(events, rm, d, False, None, tmp, today)
    print("\nPlanned again after applying (should be nothing to change):")
    _print(again, False)
    print(f"\nReports: {out / 'plan.html'} and {_write(applied, True, d.name, out)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jml", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("plan", "Show what would change (dry run)"), ("apply", "Make the changes")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("events", help="HR events CSV or JSON")
        s.add_argument("--roles", required=True, help="Role map YAML")
        s.add_argument("--provider", choices=["graph", "google", "file"], required=True)
        s.add_argument("--directory-file", help="JSON directory for --provider file (testing)")
        s.add_argument("--approved-by", help="Name of the person who reviewed changes marked for review")
        s.add_argument("--out", default="reports")
        s.set_defaults(func=lambda a, n=name: cmd(a, n == "apply"))
    d = sub.add_parser("demo", help="Run the sample events against a demo directory")
    d.add_argument("--out", default="reports")
    d.set_defaults(func=cmd_demo)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
