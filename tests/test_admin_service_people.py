"""emori-admin's People tab: a PIN-gated proxy that never keeps a name.

The hostapp holds the roster (hostapp/roster_admin.py); every People route here is a
proxy to its loopback-only ``/api/admin/roster*`` with the shared admin token. Pinned:

* nothing answers without the PIN cookie, a cross-origin request is refused, and a
  robot with no admin token says so;
* only the expected string fields are forwarded, bounded, and a malformed request
  never leaves this process;
* the hostapp's refusals (out-of-scope e-mail + its domains, duplicate, not found)
  reach the phone as they are, and badges/sheets come back as files with a CSP.
"""

from __future__ import annotations

import pytest

from emori_adapter.admin import server
from test_admin_service_fakes import PIN, base_env, http_ok, http_status, install

REF = "0123456789abcdef"
LISTING = {"people": [{"ref": REF, "name": "Anna", "email": "anna@example.org",
                       "in_scope": True, "valid": True}],
           "count": 1, "max": 1000, "live": True,
           "scope": {"checked": True, "code": "ok", "domains": ["example.org"]},
           "robot_name": "EMORI", "site_name": "Example Site", "qr_available": True}
_ROUTES = [("get", "/api/people", None), ("post", "/api/people/add", {"name": "A"}),
           ("post", "/api/people/update", {"ref": REF}),
           ("post", "/api/people/remove", {"ref": REF}),
           ("get", "/api/people/badge?ref=%s" % REF, None),
           ("get", "/api/people/sheet", None)]


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "admin_state.json"))
    return install(monkeypatch)


def _client(login: bool = True):
    from starlette.testclient import TestClient

    client = TestClient(server.create_app(background=False))
    if login:
        assert client.post("/api/login", json={"pin": PIN}).status_code == 204
    return client


def _call(client, method, path, body, **kw):
    if body is not None:
        kw["json"] = body
    return getattr(client, method)(path, **kw)


def test_nothing_answers_without_the_pin(env):
    _, http = env
    client = _client(login=False)

    for method, path, body in _ROUTES:
        assert _call(client, method, path, body).status_code == 401, path
    assert http.calls == []


def test_a_cross_origin_request_is_refused_before_anything_else(env):
    _, http = env
    client = _client()

    for method, path, body in _ROUTES:
        reply = _call(client, method, path, body,
                      headers={"Origin": "http://reachy-mini.local:8000"})
        assert (reply.status_code, reply.json()["error"]) == (403, "bad_origin"), path
    assert http.calls == []


def test_a_robot_without_the_admin_token_says_so(env, monkeypatch):
    client = _client()
    monkeypatch.delenv("EMORI_ADMIN_TOKEN")

    for method, path, body in _ROUTES:
        reply = _call(client, method, path, body)
        assert (reply.status_code, reply.json()["error"]) == (503, "admin_token_unset"), path


def test_the_list_is_the_hostapp_s_with_the_shared_secret(env):
    _, http = env
    http.add("GET", "/api/admin/roster", http_ok(LISTING))

    reply = _client().get("/api/people")

    assert reply.status_code == 200 and reply.json() == {"ok": True, **LISTING}
    assert reply.headers["cache-control"] == "no-store"
    call = http.called("GET", "/api/admin/roster")[0]
    assert call["headers"] == {"X-EMORI-Admin-Token": "tok-123"}
    assert call["timeout_s"] == server.POKAMIND_PROXY_TIMEOUT_S


def test_only_the_expected_fields_are_forwarded(env):
    _, http = env
    http.add("POST", "/api/admin/roster/add", http_ok({"saved": True, **LISTING}))
    http.add("POST", "/api/admin/roster/update", http_ok({"saved": True, **LISTING}))
    http.add("POST", "/api/admin/roster/remove", http_ok({"saved": True, **LISTING}))
    client = _client()

    client.post("/api/people/add", json={"name": "Pau", "email": "p@example.org",
                                          "student_id": "admin", "staff": {"x": 1}})
    client.post("/api/people/update", json={"ref": REF, "name": "Pau", "email": "p@example.org",
                                             "pathway": "x", "course_id": "c"})
    client.post("/api/people/remove", json={"ref": REF, "name": "ignored", "pathway": "x"})

    assert http.called("POST", "/api/admin/roster/add")[0]["json"] == {
        "name": "Pau", "email": "p@example.org"}
    assert http.called("POST", "/api/admin/roster/update")[0]["json"] == {
        "ref": REF, "name": "Pau", "email": "p@example.org", "pathway": "x"}
    assert http.called("POST", "/api/admin/roster/remove")[0]["json"] == {"ref": REF}


def test_a_pathway_is_forwarded_only_when_the_page_sends_one(env):
    """Absent keeps the person's pathway; "" or null removes it (hostapp roster_admin)."""

    _, http = env
    http.add("POST", "/api/admin/roster/add", http_ok({"saved": True, **LISTING}))
    http.add("POST", "/api/admin/roster/update", http_ok({"saved": True, **LISTING}))
    client = _client()

    client.post("/api/people/add", json={"name": "Pau", "email": "p@example.org",
                                          "pathway": "Example pathway"})
    client.post("/api/people/update", json={"ref": REF, "name": "Pau",
                                             "email": "p@example.org", "pathway": ""})
    client.post("/api/people/update", json={"ref": REF, "name": "Pau",
                                             "email": "p@example.org", "pathway": None})

    assert http.called("POST", "/api/admin/roster/add")[0]["json"]["pathway"] == (
        "Example pathway")
    updates = [c["json"] for c in http.called("POST", "/api/admin/roster/update")]
    assert [u["pathway"] for u in updates] == ["", None]


@pytest.mark.parametrize("pathway", [7, ["a"], "x" * 600])
def test_a_malformed_pathway_never_leaves_this_process(env, pathway):
    _, http = env

    reply = _client().post("/api/people/add", json={"name": "P", "email": "p@example.org",
                                                    "pathway": pathway})

    assert (reply.status_code, reply.json()["error"]) == (400, "bad_request")
    assert http.calls == []


def test_an_out_of_scope_pathway_reaches_the_phone_with_the_choices(env):
    _, http = env
    http.add("POST", "/api/admin/roster/add", http_status(
        422, {"error": "pathway_not_in_scope", "field": "pathway",
              "pathways": ["Example pathway"]}))

    reply = _client().post("/api/people/add", json={"name": "P", "email": "p@example.org",
                                                    "pathway": "Other"})

    assert reply.status_code == 422
    assert reply.json()["error"] == "pathway_not_in_scope"
    assert reply.json()["pathways"] == ["Example pathway"]


@pytest.mark.parametrize("payload", [{"name": 7, "email": "p@example.org"},
                                     {"name": "P", "email": ["p@example.org"]},
                                     {"name": "x" * 600, "email": "p@example.org"}])
def test_a_malformed_field_never_leaves_this_process(env, payload):
    _, http = env

    reply = _client().post("/api/people/add", json=payload)

    assert (reply.status_code, reply.json()["error"]) == (400, "bad_request")
    assert http.calls == []


@pytest.mark.parametrize("answer, status, error", [
    (http_status(422, {"error": "email_not_in_scope", "field": "email",
                       "domains": ["example.org"]}), 422, "email_not_in_scope"),
    (http_status(409, {"error": "duplicate_email", "field": "email"}), 409, "duplicate_email"),
    (http_status(404, {"error": "person_not_found"}), 404, "person_not_found"),
    (http_status(500, {"error": "roster_write_failed"}), 502, "roster_write_failed"),
])
def test_the_hostapp_s_refusal_reaches_the_phone(env, answer, status, error):
    _, http = env
    http.add("POST", "/api/admin/roster/add", answer)

    reply = _client().post("/api/people/add", json={"name": "Pau", "email": "someone@example.net"})

    assert (reply.status_code, reply.json()["error"]) == (status, error)
    if error == "email_not_in_scope":
        assert reply.json()["domains"] == ["example.org"]


def test_a_dead_hostapp_is_named(env):
    reply = _client().get("/api/people")

    assert (reply.status_code, reply.json()["error"]) == (502, "hostapp_down")


def test_a_badge_comes_back_as_a_locked_down_file(env):
    _, http = env
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    http.add("POST", "/api/admin/roster/badge",
             http_ok(content=svg, content_type="image/svg+xml"))
    client = _client()

    shown = client.get("/api/people/badge?ref=%s&format=svg" % REF)
    saved = client.get("/api/people/badge?ref=%s&format=svg&download=1" % REF)

    assert shown.status_code == 200 and shown.content == svg
    assert shown.headers["content-type"].startswith("image/svg+xml")
    assert "default-src 'none'" in shown.headers["content-security-policy"]
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert shown.headers["cache-control"] == "no-store"
    assert "content-disposition" not in shown.headers
    assert saved.headers["content-disposition"] == (
        'attachment; filename="emori-badge-%s.svg"' % REF)
    assert http.called("POST", "/api/admin/roster/badge")[0]["json"] == {
        "ref": REF, "format": "svg"}


@pytest.mark.parametrize("query", ["ref=EMORI:anna-001", "ref=%s&format=pdf" % REF, "ref=",
                                   "ref=0123"])
def test_a_bad_badge_request_is_refused_here(env, query):
    _, http = env

    reply = _client().get("/api/people/badge?" + query)

    assert (reply.status_code, reply.json()["error"]) == (400, "bad_request")
    assert http.calls == []


def test_a_robot_without_segno_says_so_instead_of_a_broken_image(env):
    _, http = env
    http.add("POST", "/api/admin/roster/badge", http_status(409, {"error": "qr_unavailable"}))

    reply = _client().get("/api/people/badge?ref=%s" % REF)

    assert (reply.status_code, reply.json()["error"]) == (409, "qr_unavailable")


def test_the_sheet_forwards_the_chosen_refs(env):
    _, http = env
    html = b"<!doctype html><title>Badges</title>"
    http.add("POST", "/api/admin/roster/sheet",
             http_ok(content=html, content_type="text/html; charset=utf-8"))
    client = _client()

    every = client.get("/api/people/sheet")
    two = client.get("/api/people/sheet?ref=%s&ref=%s" % (REF, "f" * 16))

    assert every.status_code == 200 and every.content == html
    assert every.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in every.headers["content-security-policy"]
    calls = http.called("POST", "/api/admin/roster/sheet")
    assert calls[0]["json"] == {} and calls[1]["json"] == {"refs": [REF, "f" * 16]}
    assert two.status_code == 200
