import json
import stat
from datetime import date
from pathlib import Path

import httpx
import pytest

from jml.cli import main, run
from jml.config import RoleMap, load_events
from jml.executor import AuditLog, apply
from jml.models import Action, Event, UserState
from jml.planner import build
from jml.providers import graph as graph_mod
from jml.providers.google import GoogleDirectory
from jml.providers.graph import GraphDirectory
from jml.providers.memory import MemoryDirectory

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
TODAY = date(2026, 11, 30)


@pytest.fixture
def rm():
    return RoleMap.load(SAMPLES / "roles.yaml")


@pytest.fixture
def directory(tmp_path):
    p = tmp_path / "dir.json"
    p.write_text((SAMPLES / "directory.json").read_text())
    return MemoryDirectory.load(p)


def ev(kind="joiner", email="new.person@example.com", **kw):
    base = dict(employee_id="E1", first_name="New", last_name="Person", department="Finance", title="Analyst",
                manager_email="omar.ali@example.com", effective_date=TODAY)
    base.update(kw)
    return Event(kind, base.pop("employee_id"), email, **base)


def kinds(plan):
    return [a.kind for a in plan.actions]


# ---------- input validation ----------

def test_events_load_and_validate(tmp_path):
    assert len(load_events(SAMPLES / "events.csv")) == 6
    bad = tmp_path / "e.csv"
    bad.write_text("kind,email,first_name,last_name,effective_date\nhire,a@example.com,A,B,2026-01-01\n"
                   "joiner,not-an-email,A,B,\njoiner,c@example.com,,,\nleaver,d@example.com,D,E,30/11/2026\n")
    with pytest.raises(ValueError) as e:
        load_events(bad)
    msg = str(e.value)
    assert "kind 'hire'" in msg and "not an email" in msg and "needs first_name" in msg and "YYYY-MM-DD" in msg


def test_events_json(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps([{"kind": "leaver", "email": "X@Example.com", "effective_date": "2026-11-30"}]))
    (e,) = load_events(p)
    assert e.email == "x@example.com" and e.effective_date == TODAY


def test_role_map_adds_up_rules(rm):
    groups, lic, review = rm.target("Finance", "Accounts Payable Clerk")
    assert groups == ["All Staff", "VPN Users", "Finance", "Finance Share", "AP Approvers"] and lic == ["SPB"] and not review
    assert rm.target("IT", "Engineer")[2] is True
    assert "Office 3rd Floor" not in rm.managed_groups


def test_role_map_rejects_unknown_keys(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("roles:\n  - match: {dept: Finance}\n")
    with pytest.raises(ValueError, match="unknown match keys"):
        RoleMap.load(p)


# ---------- planning ----------

def test_new_joiner_plan(rm):
    plan = build(ev(title="Accounts Payable Clerk"), None, rm, TODAY)
    assert kinds(plan) == ["create_user", "set_manager", "add_licence"] + ["add_group"] * 5 + ["issue_credential"]
    assert plan.actions[0].params["usage_location"] == "GB"


def test_existing_joiner_only_gets_what_is_missing(rm):
    cur = UserState("u1", "new.person@example.com", True, {"All Staff": "g1"}, {"SPB"}, manager_email="omar.ali@example.com")
    plan = build(ev(), cur, rm, TODAY)
    assert kinds(plan) == ["add_group"] * 3 and "already exists" in plan.warnings[0]


def test_disabled_existing_account_is_not_reenabled(rm):
    plan = build(ev(), UserState("u1", "new.person@example.com", False), rm, TODAY)
    assert plan.blocked and plan.actions == []


def test_privileged_role_needs_review(rm):
    assert build(ev(department="IT"), None, rm, TODAY).needs_review


def test_mover_swaps_managed_groups_and_leaves_others(rm, directory):
    cur = directory.lookup("lena.berg@example.com")
    plan = build(ev("mover", "lena.berg@example.com", title="Financial Analyst"), cur, rm, TODAY)
    removed = sorted(a.params["group"] for a in plan.actions if a.kind == "remove_group")
    added = sorted(a.params["group"] for a in plan.actions if a.kind == "add_group")
    assert removed == ["CRM Users", "Sales"] and added == ["Finance", "Finance Share"]
    assert any("Office 3rd Floor" in m for m in plan.manual)


def test_mover_with_admin_role_needs_review(rm):
    cur = UserState("u1", "x@example.com", True, admin_roles=["Global Administrator"], department="Sales")
    assert build(ev("mover", "x@example.com"), cur, rm, TODAY).needs_review


def test_leaver_plan_and_admin_warning(rm, directory):
    plan = build(ev("leaver", "daniel.cho@example.com"), directory.lookup("daniel.cho@example.com"), rm, TODAY)
    assert kinds(plan)[:2] == ["block_sign_in", "revoke_sessions"] and kinds(plan)[-1] == "set_auto_reply"
    assert "Exchange Administrator" in plan.warnings[0]
    assert any("never deletes" in m for m in plan.manual)


def test_protected_and_outside_domain_are_blocked(rm, directory):
    assert "protected" in build(ev("leaver", "admin@example.com"), directory.lookup("admin@example.com"), rm, TODAY).blocked
    assert "outside" in build(ev("joiner", "x@other.org"), None, rm, TODAY).blocked


def test_future_leaver_waits_but_future_joiner_is_prepared(rm, directory):
    later = date(2026, 12, 15)
    leaver = build(ev("leaver", "priya.nair@example.com", effective_date=later), directory.lookup("priya.nair@example.com"), rm, TODAY)
    joiner = build(ev(effective_date=later), None, rm, TODAY)
    assert leaver.scheduled and not leaver.actionable
    assert not joiner.scheduled and joiner.actionable


def test_unsupported_actions_become_manual_steps(rm):
    plan = build(ev(), None, rm, TODAY, supports={"create_user", "add_group", "set_manager", "issue_credential"})
    assert "add_licence" not in kinds(plan)
    assert plan.manual[0].startswith("Do by hand") and "SPB" in plan.manual[0]


# ---------- applying ----------

def test_apply_whole_sample_then_nothing_left_to_do(rm, directory, tmp_path):
    events = load_events(SAMPLES / "events.csv")
    applied, failures = run(events, rm, directory, True, "reviewer", tmp_path, TODAY)
    assert failures == 0
    status = {x.plan.event.email: x for x in applied}
    assert status["daniel.cho@example.com"].verified == []
    assert directory.users["daniel.cho@example.com"]["enabled"] is False
    assert directory.users["sara.khan@example.com"]["groups"][-1] == "AP Approvers"
    again, _ = run(events, rm, directory, False, None, tmp_path, TODAY)
    assert all(not x.plan.actions for x in again if not x.plan.scheduled)


def test_review_gate_blocks_apply_without_approval(rm, directory, tmp_path):
    (x,), _ = run([ev(department="IT", email="it.new@example.com")], rm, directory, True, None, tmp_path, TODAY)
    assert x.outcomes == [] and "it.new@example.com" not in directory.users
    assert any("needs review" in w for w in x.plan.warnings)


def test_secret_goes_to_private_file_never_to_logs_or_reports(rm, directory, tmp_path):
    (x,), _ = run([ev()], rm, directory, True, None, tmp_path, TODAY)
    secret_file = tmp_path / "secrets" / "new.person@example.com.txt"
    secret = secret_file.read_text().splitlines()[1].split(": ", 1)[1]
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    assert secret not in (tmp_path / "audit.jsonl").read_text()
    from jml.report import html_report, markdown
    assert secret not in markdown([x], True, "d") and secret not in html_report([x], True, "d")


def test_failed_create_stops_the_rest(rm, tmp_path):
    class Broken(MemoryDirectory):
        def run(self, a, uid, email):
            if a.kind == "create_user":
                raise RuntimeError("quota exceeded")
            return super().run(a, uid, email)

    d = Broken({})
    plan = build(ev(), None, rm, TODAY)
    out = apply(plan, d, AuditLog(tmp_path / "a.jsonl"), tmp_path / "s")
    assert len(out) == 1 and not out[0].ok
    log = [json.loads(line) for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert log[0]["ok"] is False and "quota" in log[0]["detail"]


# ---------- Microsoft Graph (mocked) ----------

class FakeGraph:
    def __init__(self):
        self.calls = []
        self.throttled = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        self.calls.append((m, path, req.content.decode() if req.content else ""))
        if "oauth2" in path:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if path == "/v1.0/users/sam@example.com" and m == "GET":
            if not self.throttled:
                self.throttled = True
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"id": "U1", "accountEnabled": True, "department": "Sales", "jobTitle": "Rep"})
        if path == "/v1.0/users/U1/memberOf":
            if "skiptoken" in str(req.url):
                return httpx.Response(200, json={"value": [{"@odata.type": "#microsoft.graph.directoryRole", "id": "R", "displayName": "Helpdesk Administrator"}]})
            return httpx.Response(200, json={"value": [{"@odata.type": "#microsoft.graph.group", "id": "G1", "displayName": "Sales"}],
                                             "@odata.nextLink": "https://graph.microsoft.com/v1.0/users/U1/memberOf?$skiptoken=x"})
        if path == "/v1.0/users/U1/licenseDetails":
            return httpx.Response(200, json={"value": [{"skuPartNumber": "SPB"}]})
        if path == "/v1.0/users/U1/manager":
            return httpx.Response(200, json={"userPrincipalName": "Boss@example.com"})
        if path.startswith("/v1.0/users/") and m == "GET":
            return httpx.Response(404, json={"error": {"message": "not found"}})
        if path == "/v1.0/users" and m == "POST":
            return httpx.Response(201, json={"id": "U2"})
        if path == "/v1.0/groups" and m == "GET":
            return httpx.Response(200, json={"value": [{"id": "G9", "displayName": "Finance"}]})
        if path == "/v1.0/groups/G9/members/$ref":
            return httpx.Response(400, json={"error": {"message": "One or more added object references already exist"}})
        if path == "/v1.0/subscribedSkus":
            return httpx.Response(200, json={"value": [{"skuPartNumber": "SPB", "skuId": "sku-spb"}]})
        if path.endswith("/temporaryAccessPassMethods"):
            return httpx.Response(201, json={"temporaryAccessPass": "TAP-SECRET"})
        return httpx.Response(204)


@pytest.fixture
def graph(monkeypatch):
    monkeypatch.setattr(graph_mod.time, "sleep", lambda s: None)
    fake = FakeGraph()
    g = GraphDirectory("tenant", "cid", "secret", client=httpx.Client(transport=httpx.MockTransport(fake)))
    return g, fake


def test_graph_lookup_pages_roles_licences_and_retries_throttling(graph):
    g, fake = graph
    u = g.lookup("sam@example.com")
    assert u.groups == {"Sales": "G1"} and u.admin_roles == ["Helpdesk Administrator"] and u.licences == {"SPB"}
    assert u.manager_email == "boss@example.com" and u.department == "Sales"
    assert sum(1 for c in fake.calls if c[1] == "/v1.0/users/sam@example.com") == 2  # one 429, one success
    assert g.lookup("nobody@example.com") is None


def test_graph_create_licence_group_and_tap(graph):
    g, fake = graph
    uid, _, _ = g.run(Action("create_user", "", {"email": "new@example.com", "first_name": "N", "last_name": "P",
                                                 "display_name": "N P", "department": "Finance", "title": "",
                                                 "employee_id": "E9", "usage_location": "GB"}), None, "new@example.com")
    body = json.loads(next(c[2] for c in fake.calls if c[:2] == ("POST", "/v1.0/users")))
    assert uid == "U2" and body["usageLocation"] == "GB" and body["passwordProfile"]["forceChangePasswordNextSignIn"]
    assert "jobTitle" not in body  # empty values are left out
    g.run(Action("add_licence", "", {"licence": "SPB"}), "U2", "new@example.com")
    lic = json.loads(next(c[2] for c in fake.calls if c[1].endswith("/assignLicense")))
    assert lic["addLicenses"][0]["skuId"] == "sku-spb"
    g.run(Action("add_group", "", {"group": "Finance"}), "U2", "new@example.com")  # "already exists" is fine
    _, detail, secret = g.run(Action("issue_credential", "", {"method": "tap"}), "U2", "new@example.com")
    assert secret == "TAP-SECRET" and "8 hours" in detail


def test_graph_leaver_calls(graph):
    g, fake = graph
    for k in ("block_sign_in", "revoke_sessions"):
        g.run(Action(k, "", {}), "U1", "sam@example.com")
    g.run(Action("remove_group", "", {"group": "Sales", "group_id": "G1"}), "U1", "sam@example.com")
    sent = [(m, p) for m, p, _ in fake.calls if "oauth2" not in p]
    assert ("PATCH", "/v1.0/users/U1") in sent and ("POST", "/v1.0/users/U1/revokeSignInSessions") in sent
    assert ("DELETE", "/v1.0/groups/G1/members/U1/$ref") in sent


def test_graph_unknown_licence_is_a_clear_error(graph):
    g, _ = graph
    with pytest.raises(graph_mod.GraphError, match="not in this tenant"):
        g.run(Action("add_licence", "", {"licence": "ENTERPRISEPACK"}), "U1", "sam@example.com")


def test_graph_bad_login_is_explained():
    c = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(graph_mod.GraphError, match="check tenant"):
        GraphDirectory("t", "c", "s", client=c).lookup("x@example.com")


# ---------- Google Workspace (mocked) ----------

def test_google_lookup_create_and_leaver(monkeypatch):
    monkeypatch.setattr("jml.providers.google.time.sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, req.content.decode() if req.content else ""))
        p = req.url.path
        if p.endswith("/users/kim@example.com") and req.method == "GET":
            return httpx.Response(200, json={"id": "G-U1", "suspended": False, "isAdmin": True,
                                             "organizations": [{"department": "Sales", "title": "Rep", "primary": True}],
                                             "relations": [{"type": "manager", "value": "Boss@example.com"}]})
        if p.endswith("/groups") and req.method == "GET":
            return httpx.Response(200, json={"groups": [{"email": "sales@example.com", "id": "g1"}]})
        if p.endswith("/members") and req.method == "POST":
            return httpx.Response(409, json={"error": {"message": "Member already exists."}})
        if p.endswith("/users") and req.method == "POST":
            return httpx.Response(200, json={"id": "G-U2"})
        return httpx.Response(204 if req.method == "DELETE" or p.endswith("signOut") else 200, json={})

    g = GoogleDirectory(lambda: "tok", "example.com", client=httpx.Client(transport=httpx.MockTransport(handler)))
    u = g.lookup("kim@example.com")
    assert u.groups == {"sales@example.com": "g1"} and u.admin_roles == ["Super Admin"] and u.manager_email == "boss@example.com"
    assert g.canon("Sales") == "sales@example.com" and g.canon("Finance Share") == "finance-share@example.com"
    _, detail, _ = g.run(Action("add_group", "", {"group": "Sales"}), "G-U1", "kim@example.com")
    assert detail == "already a member"
    g.run(Action("block_sign_in", "", {}), "G-U1", "kim@example.com")
    g.run(Action("revoke_sessions", "", {}), "G-U1", "kim@example.com")
    assert ("PATCH", "/admin/directory/v1/users/kim@example.com", '{"suspended":true}') in calls
    assert ("POST", "/admin/directory/v1/users/kim@example.com/signOut", "") in calls


def test_google_plan_moves_licence_and_auto_reply_to_manual(rm):
    g = GoogleDirectory(lambda: "t", "example.com")
    plan = build(ev(), None, rm, TODAY, canon=g.canon, supports=g.supports)
    assert "add_licence" not in kinds(plan)
    cur = UserState("u", "x@example.com", True, {"all-staff@example.com": "1", "vpn-users@example.com": "2"})
    plan2 = build(ev("mover", "x@example.com", department="Finance"), cur, rm, TODAY, canon=g.canon, supports=g.supports)
    assert sorted(a.params["group"] for a in plan2.actions if a.kind == "add_group") == ["Finance", "Finance Share"]


# ---------- CLI ----------

def test_cli_plan_then_apply_with_file_provider(tmp_path):
    d = tmp_path / "dir.json"
    d.write_text((SAMPLES / "directory.json").read_text())
    args = [str(SAMPLES / "events.csv"), "--roles", str(SAMPLES / "roles.yaml"), "--provider", "file",
            "--directory-file", str(d), "--out", str(tmp_path / "r")]
    before = d.read_text()
    assert main(["plan", *args]) == 0 and d.read_text() == before  # dry run changes nothing
    assert (tmp_path / "r" / "plan.html").exists()
    assert main(["apply", *args]) == 0
    assert "sara.khan@example.com" in json.loads(d.read_text())


def test_cli_missing_graph_settings(tmp_path, monkeypatch):
    for v in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    assert main(["plan", str(SAMPLES / "events.csv"), "--roles", str(SAMPLES / "roles.yaml"), "--provider", "graph",
                 "--out", str(tmp_path)]) == 1


def test_cli_demo(tmp_path):
    assert main(["demo", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "applied.html").exists() and (tmp_path / "plan.html").exists()
