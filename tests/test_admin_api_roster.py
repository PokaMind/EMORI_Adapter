"""The hostapp half of the phone's People tab: ``/api/admin/roster*``.

The roster is personal data and the badge ids in it log people in, so these routes
are guarded like the robot's own token: the admin token AND this robot only
(loopback; emori-admin, which holds the PIN, is the one caller). And the change is
live: a person added here badges in on the kiosk at once, without a restart, because
the routes edit the ONE roster object ``/api/identify`` resolves against.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from emori_adapter import robot_token
from emori_adapter.hostapp import server
from emori_adapter.hostapp.admin_api import PokamindAdmin
from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult
from emori_adapter.hostapp.roster_admin import badge_ref
from tests.test_admin_api import HEADER, TOKEN, _FakeMicProfile, _IdleRuntime, _RecordingPlayer

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
# A populated roster (the shipped example lists nobody): two people, one course.
SAMPLE_ROSTER = """course_id: emori-demo
staff:
  "EMORI:anna-001":
    student_id: anna-001
    email: anna@example.org
    name: "Anna Example"
  "EMORI:marc-002":
    student_id: marc-002
    email: marc@example.org
    name: "Marc Example"
"""
_ON_THE_ROBOT = ("127.0.0.1", 50000)
_ROUTES = [("get", "/api/admin/roster", None),
           ("post", "/api/admin/roster/add", {"name": "Pau", "email": "pau@example.org"}),
           ("post", "/api/admin/roster/update", {"ref": "0" * 16, "name": "P",
                                                 "email": "p@example.org"}),
           ("post", "/api/admin/roster/remove", {"ref": "0" * 16}),
           ("post", "/api/admin/roster/badge", {"ref": "0" * 16}),
           ("post", "/api/admin/roster/sheet", {})]


class _Whoami:
    def __init__(self, domains=("example.org",), pathways=()):
        self.domains = domains
        self.pathways = pathways
        self.asked = 0

    def __call__(self, base_url, token):
        self.asked += 1
        who = Whoami(robot_id=token.robot_id, display_name="Example Site", status="active",
                     credential_kind="registry", key_fingerprint=None, key_version=1,
                     org="example-org", partner="emori-robot", course_id="",
                     languages=("ca",), pathways=tuple(self.pathways),
                     identity_domains=tuple(self.domains),
                     modules=(), features={"feature_c": True, "feature_b": False,
                                           "feature_a": False})
        return WhoamiResult(ok=True, code="ok", http_status=200, whoami=who, checked_at=1.0)


def _rig(monkeypatch, tmp_path, *, pathways=()):
    from emori_adapter.robot.client import DryRunRobotClient

    roster = tmp_path / "roster.yaml"
    roster.write_text(SAMPLE_ROSTER, encoding="utf-8")
    monkeypatch.setenv("EMORI_ROSTER", str(roster))
    monkeypatch.delenv("EMORI_PATHWAYS", raising=False)
    monkeypatch.setenv("EMORI_ROBOT_ID", "test-robot-01")
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("EMORI_LANG_STATE_FILE", str(tmp_path / "kiosk_lang.json"))
    monkeypatch.setattr(server, "DEMO", False)
    whoami = _Whoami(pathways=pathways)
    load = robot_token.TokenLoad(token=robot_token.parse_token(GOLDEN_TOKEN), source="file")
    admin = PokamindAdmin(load=load, base_url="https://api.pokamind.com", whoami=whoami,
                          read_file=lambda: load)
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "extension": _IdleRuntime(), "mic_profile": _FakeMicProfile(),
        "tts": None, "stt": None, "speak": lambda text, code: None,
        "pokamind_admin": admin,
    })

    class Rig:
        pass

    rig = Rig()
    rig.app, rig.roster, rig.whoami = app, roster, whoami
    rig.client = TestClient(app, client=_ON_THE_ROBOT)
    rig.kiosk = TestClient(app, client=("192.168.1.40", 50000))
    return rig


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    return _rig(monkeypatch, tmp_path)


def _add(rig, name="Pau Example", email="pau@example.org"):
    return rig.client.post("/api/admin/roster/add", headers=HEADER,
                           json={"name": name, "email": email})


def _key_of(rig, email):
    staff = yaml.safe_load(rig.roster.read_text(encoding="utf-8"))["staff"]
    return next(k for k, r in staff.items() if r["email"] == email)


def test_every_roster_route_needs_the_admin_token(rig, monkeypatch):
    for method, path, body in _ROUTES:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(rig.client, method)(path, **kwargs).status_code == 401, path
        wrong = {"X-EMORI-Admin-Token": "nope"}
        assert getattr(rig.client, method)(path, headers=wrong, **kwargs).status_code == 401
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")
    for method, path, body in _ROUTES:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(rig.client, method)(path, headers=HEADER, **kwargs).status_code == 503


@pytest.mark.parametrize("peer", ["192.168.1.40", "10.42.0.17", "testclient"])
def test_the_roster_answers_nobody_off_the_robot_even_with_the_token(rig, peer):
    lan = TestClient(rig.app, client=(peer, 50000))
    before = rig.roster.read_bytes()

    for method, path, body in _ROUTES:
        kwargs = {"json": body} if body is not None else {}
        reply = getattr(lan, method)(path, headers=HEADER, **kwargs)
        assert (reply.status_code, reply.json()) == (403, {"error": "local_only"}), path
        assert "anna" not in reply.text.lower()
    assert rig.roster.read_bytes() == before


def test_the_list_carries_names_emails_refs_and_the_scope_never_a_badge_id(rig):
    reply = rig.client.get("/api/admin/roster", headers=HEADER)

    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    body = reply.json()
    assert body["scope"] == {"checked": True, "code": "ok", "domains": ["example.org"]}
    assert {p["email"] for p in body["people"]} == {"anna@example.org", "marc@example.org"}
    assert set(body["people"][0]) == {"ref", "name", "email", "pathway", "in_scope", "valid"}
    # The People tab's pathway picker and the course every badge is claimed in.
    assert body["pathways"] == [] and body["course_id"] == "emori-demo"
    assert "EMORI:anna-001" not in reply.text and "anna-001" not in reply.text


def test_a_person_added_on_the_phone_badges_in_without_a_restart(rig):
    key_check = rig.kiosk.post("/api/identify",
                               json={"payload": "EMORI:NOBODYYET", "lookup_only": True})
    assert key_check.status_code == 409  # an unknown badge is refused

    reply = _add(rig)
    assert reply.status_code == 200 and reply.json()["saved"] is True
    key = _key_of(rig, "pau@example.org")

    # The kiosk's own scanner path, on the SAME running app.
    seen = rig.kiosk.post("/api/identify", json={"payload": key + "\n", "lookup_only": True})
    assert (seen.status_code, seen.json()) == (200, {"anonymous": False, "name": "Pau Example"})
    assert rig.kiosk.get("/healthz").json()["roster_staff"] == 3

    gone = rig.client.post("/api/admin/roster/remove", headers=HEADER,
                           json={"ref": badge_ref(key)})
    assert gone.status_code == 200 and gone.json()["count"] == 2
    refused = rig.kiosk.post("/api/badge/claim", json={"payload": key})
    assert refused.status_code == 409


def test_a_misshapen_hand_edit_never_reaches_the_badge_scanner(rig):
    """Opening the tab after `staff: [..]` must not swap it in: every scan would 500."""

    rig.roster.write_text("course_id: emori-demo\nstaff:\n  - EMORI:anna-001\n",
                          encoding="utf-8")

    tab = rig.client.get("/api/admin/roster", headers=HEADER)
    scan = rig.kiosk.post("/api/identify",
                          json={"payload": "EMORI:anna-001", "lookup_only": True})

    assert (tab.status_code, tab.json()) == (409, {"error": "roster_unreadable"})
    assert (scan.status_code, scan.json()) == (200, {"anonymous": False,
                                                     "name": "Anna Example"})


def test_an_email_outside_identity_domains_is_refused_by_the_route(rig):
    reply = _add(rig, email="someone@example.net")

    assert reply.status_code == 422
    assert reply.json() == {"error": "email_not_in_scope", "field": "email",
                            "domains": ["example.org"]}


def test_whoami_is_asked_once_for_a_burst_of_changes(rig):
    rig.client.get("/api/admin/roster", headers=HEADER)
    _add(rig, "A One", "a1@example.org")
    _add(rig, "A Two", "a2@example.org")

    assert rig.whoami.asked == 1


def test_duplicate_and_bad_fields_through_the_route(rig):
    dup = _add(rig, "Anna Again", "Anna@example.org")
    bad = _add(rig, "Pau\x00", "pau@example.org")
    robot = _add(rig, "Robot", "robot-test-robot-01@emori.local")

    assert (dup.status_code, dup.json()["error"]) == (409, "duplicate_email")
    assert (bad.status_code, bad.json()) == (400, {"error": "name_invalid", "field": "name"})
    assert (robot.status_code, robot.json()["error"]) == (400, "email_reserved")


def test_badge_and_sheet_come_back_as_files(rig):
    _add(rig)
    ref = badge_ref(_key_of(rig, "pau@example.org"))

    svg = rig.client.post("/api/admin/roster/badge", headers=HEADER, json={"ref": ref})
    png = rig.client.post("/api/admin/roster/badge", headers=HEADER,
                          json={"ref": ref, "format": "png"})
    sheet = rig.client.post("/api/admin/roster/sheet", headers=HEADER, json={})
    bad = rig.client.post("/api/admin/roster/badge", headers=HEADER,
                          json={"ref": ref, "format": "pdf"})

    assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"
    assert b"Pau Example" in svg.content and svg.headers["cache-control"] == "no-store"
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert sheet.status_code == 200 and sheet.headers["content-type"].startswith("text/html")
    assert sheet.text.count('role="img"') == 3
    assert (bad.status_code, bad.json()) == (400, {"error": "bad_format"})


def test_a_pathway_outside_this_robots_scope_is_refused_by_the_route(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path, pathways=("Example pathway",))
    before = rig.roster.read_bytes()

    refused = rig.client.post("/api/admin/roster/add", headers=HEADER,
                              json={"name": "Pau Example", "email": "pau@example.org",
                                    "pathway": "Another pathway"})
    listing = rig.client.get("/api/admin/roster", headers=HEADER).json()

    assert refused.status_code == 422
    assert refused.json() == {"error": "pathway_not_in_scope", "field": "pathway",
                              "pathways": ["Example pathway"]}
    assert rig.roster.read_bytes() == before
    assert listing["pathways"] == ["Example pathway"]

    accepted = rig.client.post("/api/admin/roster/add", headers=HEADER,
                               json={"name": "Pau Example", "email": "pau@example.org",
                                     "pathway": "Example pathway"})
    assert accepted.status_code == 200
    person = next(p for p in rig.client.get("/api/admin/roster", headers=HEADER)
                  .json()["people"] if p["email"] == "pau@example.org")
    assert person["pathway"] == "Example pathway"
