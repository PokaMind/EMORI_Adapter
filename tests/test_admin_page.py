"""The page-to-route contract: fields the phone reads must be fields the API sends.

``admin/page.py`` is one Python string of HTML + JS and there is no browser in
this suite, so a field the UI reads under the wrong name is invisible to every
other test in the repo.  That is exactly how the last-Wi-Fi-error banner came to
read ``.error`` off a route that only ever sends ``error_text``: blank after
every real daemon error (a wrong passphrase reverts the robot to its hotspot and
three separate UI strings then point the operator at "the last error above"),
and a fabricated ``daemon_down`` "Wi-Fi error" in the one case where the daemon
had said nothing at all.

So these tests pair the two sides against each other rather than restating one
of them: the field names the page reads are extracted from the JS and checked
against the keys the real route actually produces.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from emori_adapter.admin import page, server as admin_server
from test_admin_service_fakes import PIN, base_env, http_ok, install


@pytest.fixture()
def env(monkeypatch, tmp_path):
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    return install(monkeypatch)


SAMPLE_ROSTER = """course_id: emori-demo
staff:
  "EMORI:anna-001":
    student_id: anna-001
    email: anna@example.org
    name: "Anna Example"
    pathway: "Example pathway"
  "EMORI:marc-002":
    student_id: marc-002
    email: marc@example.org
    name: "Marc Example"
"""


def _client():
    from starlette.testclient import TestClient

    from emori_adapter.admin import server

    client = TestClient(server.create_app(background=False))
    client.post("/api/login", json={"pin": PIN})
    return client


def _js_function(name: str) -> str:
    """The body of one top-level JS function in ``APP_HTML`` (brace-matched)."""

    source = page.APP_HTML
    start = source.index("function %s(" % name)
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace:index + 1]
    raise AssertionError("unbalanced braces in %s()" % name)


@pytest.mark.parametrize("constant", ["APP_HTML", "LOGIN_HTML"])
def test_the_inline_javascript_parses(tmp_path, constant):
    """One stray quote in this Python string is a BLANK recovery tool.

    Nothing else in the suite ever parses it, and the page is the only way an
    operator with a phone has of reaching a robot whose Wi-Fi is gone.
    """

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node available to parse the page's inline JS")
    html = admin_server.app_html() if constant == "APP_HTML" else getattr(page, constant)
    source = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    if not source.strip():
        pytest.skip("%s carries no inline script" % constant)
    script = tmp_path / "page.js"
    script.write_text(source, encoding="utf-8")

    result = subprocess.run([node, "--check", str(script)],
                            capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_wifi_error_banner_reads_only_fields_the_route_sends(env):
    """`error_text` (the daemon's message), never the envelope's `error`."""

    _, http = env
    http.add("GET", "/wifi/error", http_ok({"error": "Secrets were required"}))

    body = _client().get("/api/network/error").json()
    read = set(re.findall(r"\be\.([A-Za-z_]\w*)", _js_function("renderNetError")))

    assert body == {"ok": True, "error_text": "Secrets were required"}
    assert "error_text" in read                      # the banner can be filled
    assert read <= set(body)                         # ...and reads nothing else


def test_a_silent_daemon_does_not_become_a_fabricated_wifi_error(env):
    """No `/wifi/error` rule = the daemon is unreachable.

    The route then answers with the service's own envelope, and the page must
    render an empty banner rather than "The robot could not complete its last
    Wi-Fi change: daemon_down" -- a Wi-Fi error the daemon never reported.
    """

    response = _client().get("/api/network/error")

    assert response.status_code == 502
    assert response.json()["error"] == "daemon_down"
    # The guard that keeps that body out of the banner.
    assert "e.ok===false" in _js_function("renderNetError").replace(" ", "")


def test_the_pokamind_tab_reads_only_fields_the_hostapp_status_sends():
    """renderPokamind/renderScope/tokenState against the REAL producers' keys.

    The status is built by the hostapp (``admin_api.PokamindAdmin.status``) and passed
    through by emori-admin with ``ok`` added; the scope is ``Whoami.to_dict``; the Home
    dot is the overview's reduced ``pokamind.token``. A field read under any other
    name is a blank row on a phone, which no other test would ever notice.
    """

    from emori_adapter import robot_token
    from emori_adapter.hostapp.admin_api import PokamindAdmin
    from emori_adapter.hostapp.pokamind_whoami import Whoami, WhoamiResult

    token = robot_token.generate("bench-01")
    load = robot_token.TokenLoad(token=token, source="file")
    status = PokamindAdmin(load=load, read_file=lambda: load).status()[1]
    who = Whoami(robot_id="bench-01", display_name="", status="active",
                 credential_kind="fleet", key_fingerprint=None, key_version=1, org="o",
                 partner="emori-robot", course_id="c", languages=(), pathways=(),
                 identity_domains=(), modules=(), features={})
    result = WhoamiResult(ok=True, code="ok", whoami=who).to_dict()

    status_reads = set(re.findall(r"\bp\.([A-Za-z_]\w*)", _js_function("renderPokamind")))
    whoami_reads = set(re.findall(r"\bw\.([A-Za-z_]\w*)", _js_function("renderPokamind")))
    scope_reads = set(re.findall(r"\bwho\.([A-Za-z_]\w*)", _js_function("renderScope")))
    module_reads = set(re.findall(r"\bm\.([A-Za-z_]\w*)", _js_function("renderScope")))
    dot_reads = set(re.findall(r"\bt\.([A-Za-z_]\w*)", _js_function("tokenState")))

    assert status_reads and status_reads <= set(status) | {"ok"}
    assert whoami_reads <= set(result)
    assert scope_reads <= set(who.to_dict())
    assert module_reads <= {"module_id", "title", "category", "language", "available",
                            "problem"}
    assert dot_reads <= {"credential", "robot_id", "mismatch", "restart_pending", "verified"}


# --------------------------------------------------------------------------
# Wi-Fi / volume failures: a sentence and the way back in, never "HTTP 404"
# --------------------------------------------------------------------------
# A Network tab that says "HTTP 404" and nothing else is no help to anyone. The
# words a Wi-Fi/volume call can fail with are read from netman/mixer/host_backend
# (test_admin_service_fakes.backend_error_words), so a new one cannot ship without a
# sentence here.
# "HTTP 404", "http 404", "http_404", a bare 404 -- but not the http:// of a URL.
_STATUS_CODE = re.compile(r"(?i:\bhttp[ _]?[0-9]{3}\b)|\bHTTP [0-9]|\b404\b")
_AP = "EMORI-Setup"
_NMCLI = 'sudo nmcli device wifi connect "<SSID>" password "<password>"'
_RESTART = "sudo systemctl restart emori-admin"


def _js_object_keys(name: str) -> set[str]:
    source = page.APP_HTML
    start = source.index("var %s={" % name)
    block = source[start:source.index("};", start)]
    keys = set(re.findall(r"^\s*([a-z_0-9]+):", block, re.M))
    return keys | set(re.findall(r"\b%s\.([a-z_0-9]+)=" % name, source))


def _every_failure_word() -> set[str]:
    from test_admin_service_fakes import backend_error_words

    # + the daemon's own answers that arrive as http_<code>, and forget's "not saved".
    return backend_error_words() | {"http_404", "http_409", "http_500", "not_saved",
                                    "timeout", "unreachable"}


def test_every_wifi_and_volume_failure_word_has_a_sentence():
    said = _js_object_keys("BACKEND_WHY") | _js_object_keys("BACKEND_SAID")

    missing = {w for w in _every_failure_word() if not w.startswith("http_5")} - said
    assert not missing, "no sentence on the phone for %s" % sorted(missing)


def test_both_tabs_have_the_recovery_banner_in_their_banner_slot():
    body = page.APP_HTML
    network = body[body.index("id=v-network"):body.index("id=v-audio")]
    audio = body[body.index("id=v-audio"):body.index("id=v-lang")]

    # After the header, before the first card -- where the last-Wi-Fi-error banner is.
    assert network.index("id=netErrBan") < network.index("id=netFailBan") \
        < network.index("class=card")
    assert audio.index("id=audFailBan") < audio.index("class=card")
    # Whichever control hits a broken backend, api() draws the banner.
    assert "renderBackendBan(area" in _js_function("api")


def test_the_audio_tab_reads_only_fields_the_route_sends(env):
    body = _client().get("/api/audio").json()
    read = set(re.findall(r"\ba\.([A-Za-z_]\w*)", _js_function("renderAudio")))

    assert {"speaker_error", "mic_error"} <= read <= set(body)


def test_a_failed_network_status_still_names_the_hotspot(env):
    """loadNetwork takes ap_ssid from the failure envelope for the banner's first step."""

    response = _client().get("/api/network/status")

    assert response.status_code == 502
    assert response.json()["error"] == "daemon_down" and response.json()["ap_ssid"]
    assert "'ap_ssid' in e.data" in _js_function("loadNetwork")


def _run_js(tmp_path, script: str) -> dict:
    import json

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node available to run the page's inline JS")
    source = page.APP_HTML
    mapping = source[source.index("var ERRMSG={"):source.index("async function api(")]
    prelude = (
        "var S={net:null}; var AP_URL='http://10.42.0.1:8070'; var nodes={};\n"
        "function $(id){return nodes[id]||(nodes[id]={innerHTML:''});}\n"
        "function esc(s)" + _js_function("esc") + "\n")
    path = tmp_path / "mapping.js"
    path.write_text(prelude + mapping + "\n" + script, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _text(markup: str) -> str:
    import html

    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def test_a_missing_route_is_a_sentence_and_the_way_back_in(tmp_path):
    out = _run_js(tmp_path, """
S.net={ap_ssid:'%s'};
var r={};
r.msg=apiMessage({ok:false,error:'route_missing',status:404},502);
r.legacy=apiMessage({ok:false,error:'http_404',status:404},502);
r.bare=apiMessage(null,404);
r.detail=apiMessage({detail:'Not Found'},404);
renderBackendBan('network','route_missing'); r.network=$('netFailBan').innerHTML;
renderBackendBan('audio','route_missing'); r.audio=$('audFailBan').innerHTML;
S.net=null; renderBackendBan('network','daemon_down'); r.noap=$('netFailBan').innerHTML;
renderBackendBan('network',''); r.cleared=$('netFailBan').innerHTML;
renderBackendBan('network','not_saved'); r.notbroken=$('netFailBan').innerHTML;
console.log(JSON.stringify(r));
""" % _AP)

    for key in ("msg", "legacy", "bare", "detail", "network", "audio", "noap"):
        assert not _STATUS_CODE.search(_text(out[key])), (key, out[key])
    assert "does not have" in out["msg"] and out["legacy"] == out["msg"]
    network = _text(out["network"])
    assert out["msg"] in network
    assert _AP in network and "http://10.42.0.1:8070" in network
    assert _NMCLI in network and _RESTART in network
    audio = _text(out["audio"])
    assert _RESTART in audio and "nmcli" not in audio
    assert "(its hotspot)" in _text(out["noap"]) and _NMCLI in _text(out["noap"])
    assert out["cleared"] == "" and out["notbroken"] == ""


def test_every_failure_word_reads_as_a_sentence_not_a_code(tmp_path):
    import json

    words = sorted(_every_failure_word())
    out = _run_js(tmp_path, """
var r={};
%s.forEach(function(w){
 var b=backendWords(w);
 r[w]={msg:apiMessage({ok:false,error:w},502),broken:!!(b&&b.broken)};
});
console.log(JSON.stringify(r));
""" % json.dumps(words))

    for word in words:
        message = out[word]["msg"]
        assert message.endswith(".") and "_" not in message, (word, message)
        assert not _STATUS_CODE.search(message), (word, message)
    # Breakage gets the recovery steps; an answer does not.
    assert out["route_missing"]["broken"] and out["nmcli_missing"]["broken"]
    assert out["alsa_missing"]["broken"] and out["daemon_down"]["broken"]
    assert out["http_500"]["broken"]
    assert not out["not_saved"]["broken"] and not out["http_409"]["broken"]


def test_the_hotspot_name_is_escaped_in_the_banner(tmp_path):
    out = _run_js(tmp_path, """
S.net={ap_ssid:'<img src=x onerror=alert(1)>'};
renderBackendBan('network','route_missing');
console.log(JSON.stringify({ban:$('netFailBan').innerHTML}));
""")

    assert "<img" not in out["ban"] and "&lt;img src=x" in out["ban"]


def test_a_missing_route_banner_names_the_backend_setting_to_fix(tmp_path):
    """EMORI_ADMIN_HOST_BACKEND=daemon on a v2 robot is the one
    setting that produces route_missing; the banner must say how to undo it."""

    out = _run_js(tmp_path, """
var r={};
renderBackendBan('network','route_missing'); r.network=$('netFailBan').innerHTML;
renderBackendBan('audio','route_missing'); r.audio=$('audFailBan').innerHTML;
renderBackendBan('network','daemon_down'); r.down=$('netFailBan').innerHTML;
console.log(JSON.stringify(r));
""")

    for key in ("network", "audio"):
        text = _text(out[key])
        assert "set EMORI_ADMIN_HOST_BACKEND=auto in /opt/emori/.env" in text, text
        assert _RESTART in text
        assert text.index("EMORI_ADMIN_HOST_BACKEND=auto") < text.index(_RESTART)
    # Only where it can be the cause: a daemon that is down is not a backend setting.
    assert "EMORI_ADMIN_HOST_BACKEND" not in _text(out["down"])
    assert _RESTART in _text(out["down"])


def test_an_unexplained_failure_is_a_sentence_and_a_log_id_never_a_status_code(tmp_path):
    out = _run_js(tmp_path, """
var r={};
r.bare=apiMessage(null,500,'1a2b3c4d');
r.validation=apiMessage({detail:[{loc:['body','volume'],msg:'bad'}]},422,'00ff00ff');
r.noid=apiMessage(null,500,null);
r.junk=apiMessage(null,500,'<b>x</b>');
console.log(JSON.stringify(r));
""")

    for key, message in out.items():
        assert not _STATUS_CODE.search(message), (key, message)
        assert not re.search(r"\b(?:500|422)\b", message), (key, message)
        assert message.startswith("The robot could not do that."), (key, message)
    assert "1a2b3c4d" in out["bare"] and "00ff00ff" in out["validation"]
    assert out["noid"] == "The robot could not do that."
    assert "<b>" not in out["junk"] and out["junk"] == out["noid"]
    # api() hands the header to it.
    assert "r.headers.get('X-EMORI-Log-Id')" in _js_function("api")


def test_a_failed_api_answer_carries_the_log_id_the_journal_line_has(env, caplog):
    from starlette.testclient import TestClient

    from emori_adapter.admin import server

    client = _client()
    with caplog.at_level("WARNING", logger="emori.admin"):
        failed = client.get("/api/network/status")       # nothing answers Wi-Fi here
        ok = client.get("/api/overview")
        signed_out = TestClient(server.create_app(background=False)).get("/api/overview")

    assert failed.status_code >= 500
    log_id = failed.headers.get(server.LOG_ID_HEADER)
    assert log_id and re.fullmatch(r"[0-9a-f]{8}", log_id)
    lines = [r.getMessage() for r in caplog.records if log_id in r.getMessage()]
    assert lines == ["admin: GET /api/network/status answered %d (log id %s)"
                     % (failed.status_code, log_id)]
    assert ok.status_code == 200 and server.LOG_ID_HEADER not in ok.headers
    assert signed_out.status_code == 401 and server.LOG_ID_HEADER not in signed_out.headers


# --------------------------------------------------------------------------
# People tab: the badge roster
# --------------------------------------------------------------------------
def test_the_people_tab_reads_only_fields_the_hostapp_listing_sends(tmp_path, monkeypatch):
    """renderPeople/ppAdd against the REAL producer (hostapp roster_admin.RosterAdmin)."""

    from emori_adapter.hostapp.identity import Roster
    from emori_adapter.hostapp.roster_admin import RosterAdmin

    monkeypatch.setenv("EMORI_ROBOT_ID", "bench-01")
    path = tmp_path / "roster.yaml"
    # The shipped example lists nobody: a two-person sample, with a pathway on one.
    path.write_text(SAMPLE_ROSTER, encoding="utf-8")
    admin = RosterAdmin(roster=Roster.from_yaml(path), path=path, scope=None)
    listing = admin.list()[1]
    added = admin.add({"name": "Pau", "email": "pau@example.org"})[1]
    person = listing["people"][0]

    def reads(var: str, *functions: str) -> set[str]:
        found: set[str] = set()
        for name in functions:
            found |= set(re.findall(r"\b%s\.([A-Za-z_]\w*)" % var, _js_function(name)))
        return found

    body_reads = reads("p", "renderPeople")
    person_reads = reads("x", "renderPeople", "openEdit", "ppRemove", "openBadge")
    scope_reads = reads("sc", "renderPeople", "ppDomainList")
    write_reads = reads("j", "ppAdd")
    who_reads = reads("who", "ppAdd")

    assert {"people", "scope", "qr_available", "live", "pathways",
            "course_id"} <= body_reads <= set(listing) | {"ok"}
    assert {"ref", "name", "email", "valid", "in_scope", "pathway"} <= person_reads <= set(person)
    assert {"checked", "domains", "code"} <= scope_reads <= set(listing["scope"])
    assert write_reads <= set(added) | {"ok"} and who_reads <= set(person)


def test_every_people_refusal_reads_as_a_sentence():
    """Each code the hostapp's People routes answer has words on the phone."""

    import inspect

    from emori_adapter.hostapp import roster_admin

    source = inspect.getsource(roster_admin)
    # Every place a code is born: {"error": "..."}, FieldError(field, "..."), RosterError("...").
    codes = set(re.findall(
        r"(?:\"error\":\s*|FieldError\(\"\w+\",\s*|RosterError\()\"([a-z_]+)\"", source))
    said = _js_object_keys("ERRMSG")

    assert {"email_not_in_scope", "duplicate_email", "roster_unreadable", "qr_unavailable",
            "person_not_found", "name_invalid", "email_reserved"} <= codes
    missing = codes - said - {"bad_format", "bad_refs"}  # never sent by this page
    assert not missing, "no sentence on the phone for %s" % sorted(missing)


def test_the_people_tab_draws_names_only_through_esc():
    render = _js_function("renderPeople")

    assert "esc(txt(x.name))" in render and "esc(txt(x.email))" in render
    assert not re.search(r"\+\s*x\.(?:name|email)\b", render)
    assert not re.search(r"innerHTML\s*=\s*[^;]*\bwho\.name", _js_function("ppAdd"))


# --------------------------------------------------------------------------
# Site settings and menu: the tabs token-driven provisioning added
# --------------------------------------------------------------------------
def _reads(var: str, *functions: str) -> set[str]:
    found: set[str] = set()
    for name in functions:
        found |= set(re.findall(r"\b%s\.([A-Za-z_]\w*)" % var, _js_function(name)))
    return found


@pytest.mark.parametrize("tab", ["menu", "settings"])
def test_the_new_tabs_render_with_a_section_a_button_and_a_loader(tab):
    body = page.APP_HTML

    assert 'data-tab=%s aria-controls=v-%s' % (tab, tab) in body
    assert "id=v-%s role=tabpanel" % tab in body
    views = re.search(r"var VIEWS=\[(.*?)\];", body, re.S).group(1)
    assert "'%s'" % tab in views
    assert "LOADERS.%s=" % tab in body


def test_with_no_add_on_the_page_has_no_add_on_tab_and_its_script_runs(env, tmp_path):
    """No add-on installed: three empty slots, every tab button has a view and a loader,
    and the page script runs against a stub DOM (the run test below feeds it real answers)."""

    import json

    client = _client()
    served = client.get("/").text
    assert served == admin_server.app_html(None)
    assert "__ADDON" not in served
    buttons = re.findall(r"data-tab=(\w+) aria-controls=v-(\w+)", served)
    views = re.search(r"var VIEWS=\[(.*?)\];", served, re.S).group(1)
    assert buttons and all(tab == ctl for tab, ctl in buttons)
    assert [t for t, _ in buttons] == re.findall(r"'(\w+)'", views)
    for tab, _ in buttons:
        assert "id=v-%s role=tabpanel" % tab in served and "LOADERS.%s=" % tab in served
    assert client.get("/api/setup").json().get("addon") is None

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node available to run the page's inline JS")
    app_js = "\n".join(re.findall(r"<script>(.*?)</script>", served, re.S))
    probe = "console.log(JSON.stringify({addon: ADDON, views: VIEWS.length}));"
    path = tmp_path / "plain.js"
    path.write_text(_DOM_STUB + "var ROUTES={};\n" + app_js + "\n" + probe, encoding="utf-8")

    result = subprocess.run([node, str(path)], capture_output=True, encoding="utf-8",
                            timeout=60)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {"addon": {"setup": [], "link": []}, "views": len(buttons)}


def test_every_element_the_new_tabs_script_looks_up_exists():
    """``$('id')`` of an id the HTML does not have is a TypeError on a phone."""

    body = page.APP_HTML
    script = "\n".join(re.findall(r"<script>(.*?)</script>", body, re.S))
    ids = set(re.findall(r"\$\('([A-Za-z][\w-]*)'\)", script))
    dynamic = {i for i in ids if i.startswith(("fm_", "mnName", "mnKw", "st_"))}
    markup = set(re.findall(r"\bid=\"?([A-Za-z][\w-]*)", body.split("<script>")[0]))
    missing = ids - dynamic - markup
    assert not missing, sorted(missing)


def test_no_secret_value_ever_appears_in_the_page(monkeypatch, tmp_path):
    secrets_in_env = {
        "EMORI_ADMIN_TOKEN": "admin-token-9f8e7d6c5b4a",
        "EMORI_AP_SSID": "Hotspot-Name-4471",
        "EMORI_AP_PASSWORD": "hotspot-pass-8812",
        "EMORI_DASH_PASSWORD": "dash-pass-3390-qq",
        "EMORI_SMTP_PASS": "smtp-pass-1177-zz",
        "EMORI_ADMIN_SALT": "admin-salt-5521-yy",
    }
    base_env(monkeypatch, state_file=str(tmp_path / "state.json"))
    install(monkeypatch)
    for key, value in secrets_in_env.items():
        monkeypatch.setenv(key, value)
    from starlette.testclient import TestClient

    from emori_adapter.admin import server

    client = TestClient(server.create_app(background=False))
    login = client.get("/").text
    client.post("/api/login", json={"pin": PIN})
    app = client.get("/").text

    for text in (login, app, page.APP_HTML, page.LOGIN_HTML):
        for value in list(secrets_in_env.values()) + [PIN]:
            assert value not in text
    # A revealed credential is drawn for its seconds and wiped; never stored on the phone.
    stores = set(re.findall(r"\bstore\('([a-z_]+)'", app))
    assert stores <= {"tab", "log_unit"}, stores
    assert "localStorage" not in _js_function("showSecret") + _js_function("credAction")


def test_the_setup_checklist_is_live_data_never_the_phone_s_memory(monkeypatch, tmp_path):
    """renderSetup/setupSteps against the REAL /api/setup, which emori-admin assembles."""

    from starlette.testclient import TestClient

    from emori_adapter.admin import server

    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "state.json"))
    _, http = install(monkeypatch)
    http.add("GET", "/api/admin/site", http_ok({
        "keys": [], "restart_pending": True, "restart_reasons": ["scope_changed"],
        "invalid_running": {}, "scope": {"status": "ok", "auto": True, "fetched_at": 1.0,
                                         "stale": False, "last_sync_code": "ok"}}))
    http.add("GET", "/api/admin/menu", http_ok({"lang": "ca", "source": "auto",
                                                "authored": "title_draft", "errors": [],
                                                "saved": False}))
    http.add("GET", "/api/admin/roster", http_ok({"people": [{"ref": "x"}]}))
    http.add("GET", "/api/admin/summary", http_ok({"lang": "ca", "allowed_langs": ["ca"],
                                                   "session_active": False,
                                                   "addon_mode": None}))
    http.add("GET", "/api/admin/pokamind", http_ok({"credential": "robot_token",
                                                    "robot_id": "bench-01",
                                                    "last_whoami": {"ok": True}}))
    client = TestClient(server.create_app(background=False))
    client.post("/api/login", json={"pin": PIN})

    reply = client.get("/api/setup")
    setup = reply.json()

    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    assert setup["menus"]["ca"] == {"source": "auto", "authored": "title_draft", "errors": 0,
                                    "saved": False}
    assert setup["people"] == {"count": 1} and setup["langs"] == ["ca"]
    assert setup["scope"]["restart_pending"] is True
    assert setup["token"]["verified"] is True
    assert set(setup) == {"wifi", "token", "scope", "langs", "menus", "people", "addon",
                          "readycheck", "auto_apply"}
    assert _reads("s", "setupSteps", "renderSetup") <= set(setup)
    assert _reads("w", "setupSteps") <= set(setup["wifi"])
    assert _reads("tk", "setupSteps") <= set(setup["token"])
    assert _reads("sc", "setupSteps", "renderSetup") <= set(setup["scope"])
    assert _reads("pp", "setupSteps") <= set(setup["people"])
    assert _reads("rc", "setupSteps") <= set(setup["readycheck"])
    assert _reads("m", "menuLabel", "menuState") <= set(setup["menus"]["ca"])
    assert _reads("auto", "applyBanner") <= set(setup["auto_apply"])
    for name in ("setupSteps", "renderSetup", "loadSetup"):
        assert "store(" not in _js_function(name) and "localStorage" not in _js_function(name)
    assert "api('/api/setup')" in _js_function("loadSetup")


def _site_body(monkeypatch, tmp_path):
    """GET /api/settings as emori-admin answers it, built from the REAL producers."""

    from emori_adapter import site_settings
    from emori_adapter.admin import server, site_apply
    from emori_adapter.hostapp import admin_api

    monkeypatch.setattr(site_settings, "base_dir", lambda root=None: tmp_path)
    monkeypatch.setattr(admin_api, "_dotenv_file", lambda: None)
    body = admin_api.site_status(admin_api.SiteRuntime(token=None, root=tmp_path))
    body["keys"] = server.merge_admin_rows(body["keys"])
    watcher = site_apply.SiteApplyWatcher(fetch=lambda m, p: None, restart=lambda: {})
    body["auto_apply"] = watcher.status()
    return body


def test_the_settings_tabs_read_only_fields_the_routes_send(monkeypatch, tmp_path):
    body = _site_body(monkeypatch, tmp_path)
    row_keys = set(body["keys"][0])

    assert _reads("st", "renderApplied", "renderSettings") <= set(body) | {"ok"}
    assert _reads("sc", "renderApplied") <= set(body["scope"])
    assert {"source", "running_differs"} <= _reads("r", "renderApplied") <= row_keys
    assert _reads("r", "stField", "renderSettings", "stSave") <= row_keys
    assert _reads("a", "renderAuto") <= set(body["auto_apply"])
    assert all(set(row) == row_keys for row in body["keys"])


def test_secret_settings_reach_the_phone_as_set_and_source_only(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_SMTP_PASS", "smtp-pass-1177-zz")
    monkeypatch.setenv("EMORI_DASH_PASSWORD", "dash-pass-3390-qq")
    body = _site_body(monkeypatch, tmp_path)
    rows = {row["key"]: row for row in body["keys"]}

    assert rows["EMORI_SMTP_PASS"]["value"] is None and rows["EMORI_SMTP_PASS"]["set"]
    assert rows["EMORI_DASH_PASSWORD"]["value"] is None
    assert rows["EMORI_DASH_PASSWORD"]["owner"] == "admin"
    import json

    assert "smtp-pass-1177-zz" not in json.dumps(body)
    assert "dash-pass-3390-qq" not in json.dumps(body)
    # stField never draws a secret's value: the box is write-only.
    assert "r.value" not in _js_function("stField")


def test_the_menu_tab_reads_only_fields_the_route_sends(monkeypatch, tmp_path):
    from emori_adapter import site_settings
    from emori_adapter.hostapp import admin_api, menu_builder

    monkeypatch.setattr(site_settings, "base_dir", lambda root=None: tmp_path)

    def no_menu(lang):
        raise RuntimeError("no menu in this test")

    menus = admin_api.MenuAccess(source=lambda lang: "builtin", path=lambda lang: None,
                                 site_path=lambda lang: str(tmp_path / "m.yaml"),
                                 load=no_menu, clear=lambda: None)
    body = admin_api.menu_status("ca", menus=menus,
                                 site=admin_api.SiteRuntime(token=None, root=tmp_path))
    rows = menu_builder.module_rows({"modules": [{
        "module_id": "AAAA", "title": "T", "category": None, "language": "ca",
        "available": True, "problem": None}]}, "ca")

    assert _reads("d", "renderMenu", "mnFromData", "menuProblems") <= set(body) | {"ok"}
    assert _reads("m", "renderMenu", "renderMenuOpts", "mnModule") <= set(rows[0])
    assert _reads("x", "mnFromData") <= {"module_id", "name", "keywords"}
    assert _reads("p", "menuProblems") <= {"code", "option", "keyword"}


def _said(name: str) -> set[str]:
    return _js_object_keys(name)


def test_every_settings_code_reads_as_a_sentence():
    import inspect

    from emori_adapter import site_settings
    from emori_adapter.hostapp import admin_api, results_email

    validator = set(re.findall(r'"(value_[a-z_]+)"', inspect.getsource(site_settings)))
    assert {"value_not_https", "value_not_url"} <= validator
    assert validator | {"key_unknown", "key_wrong_owner"} <= _said("CODEMSG")

    hostapp_errors = set(re.findall(r'"error": "([a-z_]+)"', inspect.getsource(admin_api)))
    # Answered only to a caller that is not emori-admin (its own guard maps them).
    never_shown = {"local_only", "unauthorized", "admin_not_configured"}
    mail = set(re.findall(r'"((?:smtp|recipient|test)_[a-z_]+)"',
                          inspect.getsource(results_email) + inspect.getsource(admin_api)))
    mail.discard("smtp_code")
    missing = (hostapp_errors - never_shown | mail) - _said("ERRMSG")
    assert not missing, "no sentence on the phone for %s" % sorted(missing)


def test_every_menu_problem_and_module_reason_reads_as_a_sentence():
    from emori_adapter.hostapp import menu_builder, voice_topics

    assert set(voice_topics.MENU_PROBLEMS) | {"menu_unreadable"} <= _said("MENUMSG")
    reasons = {value for name, value in vars(menu_builder).items()
               if name.startswith("SKIP_") and isinstance(value, str)}
    assert reasons | {"not_found", "not_visible_to_robot", "language_mismatch"} \
        <= _said("MODREASON")


def test_every_new_admin_route_refusal_reads_as_a_sentence():
    import inspect

    from emori_adapter.admin import auth, network, server

    source = inspect.getsource(server.create_app)
    start = source.index("# ---------------------------------------------------------------- PIN again")
    end = source.index("# ---------------------------------------------------------------- services")
    codes = set(re.findall(r'"error": "([a-z_]+)"', source[start:end]))
    codes |= set(auth.PIN_CHANGE_CODES)
    codes |= set(re.findall(r'return False, "([a-z_]+)"',
                            inspect.getsource(network.set_ap_credentials)))
    assert {"bad_pin", "pin_invalid", "credential_unset", "bad_ssid", "restart_failed"} <= codes
    missing = codes - _said("ERRMSG")
    assert not missing, "no sentence on the phone for %s" % sorted(missing)


def test_the_language_tab_offers_only_the_scope_s_languages():
    render, load = _js_function("renderLang"), _js_function("loadLang")

    assert "S.langAllowed" in render and "disabled" in render
    assert "allowed_langs" in load


def test_the_people_tab_sends_a_pathway_only_when_the_picker_is_shown():
    add, save = _js_function("ppAdd"), _js_function("ppSave")

    assert "$('ppPathBox').hidden" in add and "body.pathway" in add
    # An edit that leaves the pathway alone leaves the key out (the hostapp keeps it).
    assert "!==ppEditPath" in save and "body.pathway" in save
    assert "esc(x.pathway)" in _js_function("renderPeople")


# --------------------------------------------------------------------------
# The new tabs, RUN: node with a stub DOM, fed the real producers' answers
# --------------------------------------------------------------------------
_DOM_STUB = r"""
function El(id){ this.id=id; this.innerHTML=''; this.textContent=''; this.value='';
 this.hidden=false; this.disabled=false; this.className=''; this.checked=false;
 this.placeholder=''; this.attrs={}; this.style={};
 this.classList={add:function(){},remove:function(){},toggle:function(){},
  contains:function(){return false;}}; }
El.prototype.setAttribute=function(k,v){ this.attrs[k]=String(v); };
El.prototype.getAttribute=function(k){ return k in this.attrs?this.attrs[k]:null; };
El.prototype.hasAttribute=function(k){ return k in this.attrs; };
El.prototype.removeAttribute=function(k){ delete this.attrs[k]; };
El.prototype.addEventListener=function(){};
El.prototype.querySelectorAll=function(){ return []; };
El.prototype.querySelector=function(){ return null; };
El.prototype.closest=function(){ return null; };
El.prototype.scrollIntoView=function(){}; El.prototype.focus=function(){};
El.prototype.showModal=function(){}; El.prototype.close=function(){};
var ELS={};
globalThis.document={getElementById:function(id){ return ELS[id]||(ELS[id]=new El(id)); },
 querySelector:function(){ return new El('q'); }, querySelectorAll:function(){ return []; },
 addEventListener:function(){}, visibilityState:'visible'};
globalThis.window={addEventListener:function(){}, scrollTo:function(){}};
globalThis.location={hash:'', href:'/'};
globalThis.history={replaceState:function(){}};
globalThis.localStorage={getItem:function(){ return null; }, setItem:function(){}};
globalThis.setInterval=function(){ return 0; };
var CALLS=[];
globalThis.fetch=async function(path,o){
 CALLS.push(path);
 var key=String(path).split('?')[0], body=ROUTES[key];
 var ok=body!==undefined;
 return {ok:ok, status:ok?200:404, headers:{get:function(){ return null; }},
  json:async function(){ return ok?body:{error:'not_found'}; }};
};
"""


def _site_answer(monkeypatch, tmp_path) -> dict:
    body = _site_body(monkeypatch, tmp_path)
    body["restart_pending"] = True
    body["restart_reasons"] = ["scope_changed", "settings_saved"]
    body["scope"].update({"status": "ok", "fetched_at": 1.0, "contract": 2,
                          "derived": {"EMORI_COURSE_ID": "course-1", "EMORI_LANG": "ca"},
                          "last_sync_code": "ok", "ignored_keys": ["app_origin"]})
    for row in body["keys"]:
        if row["key"] == "EMORI_COURSE_ID":
            row.update(value="course-1", set=True, source="pokamind")
        if row["key"] == "EMORI_SMTP_HOST":
            row.update(value="smtp.example.org", set=True, source="admin",
                       running_differs=True)
        if row["key"] == "EMORI_SESSION_START_S":
            row.update(value="130", set=True, source="hardware")
    return {"ok": True, **body}


def test_the_new_tabs_run_on_the_real_answers_without_a_script_error(monkeypatch, tmp_path):
    import json

    from emori_adapter.hostapp import menu_builder

    node = shutil.which("node")
    if node is None:
        pytest.skip("no node available to run the page's inline JS")
    site = _site_answer(monkeypatch, tmp_path)
    whoami = {"modules": [
        {"module_id": "AAAA", "title": "Listening, speaking and writing", "category": None,
         "language": "ca", "available": True, "problem": None, "menu_name": None,
         "menu_keywords": None},
        {"module_id": "BBBB", "title": "Front desk", "category": None, "language": "ca",
         "available": False, "problem": "not_visible_to_robot"}]}
    menu = {"ok": True, "lang": "ca", "source": "auto", "authored": "title_draft",
            "menu": {"prompt": "Hola.", "options": {"1": {"module_id": "AAAA",
                                                          "name": "Escolta",
                                                          "keywords": ["escolta"]}},
                     "default": "1"},
            "modules": menu_builder.module_rows(whoami, "ca"), "errors": [], "problems": [],
            "saved": True, "auto_available": True, "auto": True, "scope_status": "ok"}
    setup = {"wifi": {"mode": "wlan", "ssid": "Site", "internet": True},
             "token": {"credential": "robot_token", "robot_id": "bench-01", "mismatch": False,
                       "restart_pending": False, "verified": True},
             "scope": {"status": "ok", "stale": False, "auto": True, "fetched_at": 1.0,
                       "last_sync_code": "ok", "restart_pending": True,
                       "restart_reasons": ["scope_changed"]},
             "langs": ["ca", "en"],
             "menus": {"ca": {"source": "auto", "authored": "title_draft", "errors": 0,
                              "saved": False},
                       "en": {"source": "dev", "authored": None, "errors": 0, "saved": False}},
             "people": {"count": 0},
             "readycheck": {"running": False, "ran": False, "fail": 0, "warn": 0},
             "auto_apply": site["auto_apply"]}
    routes = {"/api/setup": setup, "/api/settings": site, "/api/menu": menu,
              "/api/lang": {"ok": True, "lang": "ca", "allowed_langs": ["ca"]}}
    source = admin_server.app_html()
    app_js = "\n".join(re.findall(r"<script>(.*?)</script>", source, re.S))
    probe = r"""
(async function(){
 await loadSetup();
 await loadSettings();
 MN.lang='ca'; await loadMenu();
 await loadLang();
 // The stub DOM does not parse innerHTML: type into the option boxes it drew.
 document.getElementById('mnName0').value='Escolta';
 document.getElementById('mnKw0').value='escolta, escolta activa';
 mnPromptFromNames();
 var out={};
 ['steps','homeApply','setupChip','pkApplied','stApply','stGroup-mail','stGroup-tuning',
  'stCreds','stAuto',
  'mnChip','mnOpts','mnMods','mnWhere','langScope']
  .forEach(function(id){ var e=ELS[id]||{}; out[id]=(e.innerHTML||'')+(e.textContent||''); });
 out.prompt=ELS.mnPrompt.value; out.langCa=ELS.langCa.disabled; out.langEn=ELS.langEn.disabled;
 out.draft=mnDraft();
 // The host app down: the credentials (this service's own) still render.
 delete ROUTES['/api/settings']; S.st=null; ELS.stCreds.innerHTML='';
 await loadSettings();
 out.credsDown=ELS.stCreds.innerHTML; out.banDown=ELS.stBan.innerHTML;
 console.log(JSON.stringify(out));
})().catch(function(e){ console.error(e&&e.stack||e); process.exit(3); });
"""
    script = (_DOM_STUB + "var ROUTES=" + json.dumps(routes) + ";\n" + app_js + "\n" + probe)
    path = tmp_path / "run.js"
    path.write_text(script, encoding="utf-8")

    result = subprocess.run([node, str(path)], capture_output=True, encoding="utf-8",
                            timeout=60)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert "Check the spoken menu" in out["steps"] and "title draft" in out["steps"]
    assert "Apply now" in out["homeApply"] and "Apply now" in out["stApply"]
    assert "course-1" in out["pkApplied"] and "Ignored from the record" in out["pkApplied"]
    assert "SMTP server" in out["stGroup-mail"] and "restart pending" in out["stGroup-mail"]
    # The hardware profile's tuning is on the Settings tab, with where it comes from.
    assert "Session start deadline, robot (s)" in out["stGroup-tuning"]
    assert 'class="src hardware">hardware' in out["stGroup-tuning"]
    assert 'inputmode="decimal"' in out["stGroup-tuning"]
    assert "Dashboard password" in out["stCreds"]
    assert "Listening, speaking and writing" in out["mnOpts"]
    assert "not visible to this robot" in out["mnMods"]
    assert out["langEn"] is True and out["langCa"] is False and "Catal" in out["langScope"]
    assert "Admin PIN" in out["credsDown"] and "Cannot read the settings" in out["banDown"]
    assert out["prompt"] == "Hola. Per triar la teva pràctica, digues u per Escolta."
    assert out["draft"] == {"prompt": out["prompt"], "default": "1", "options": {
        "1": {"module_id": "AAAA", "name": "Escolta",
              "keywords": ["escolta", "escolta activa"]}}}


def test_an_add_on_script_runs_in_its_own_try_above_the_pages_wiring():
    class _Addon:
        tab_id = "extra"
        tab_label = "Extra"
        html = "<section class=view id=v-extra></section>"
        js = "throw new Error('broken add-on');"

    source = admin_server.app_html(_Addon())

    opened = source.index("try{\nthrow new Error('broken add-on');\n}catch(e){")
    assert opened < source.index("LOADERS.home=")
    assert "__ADDON_JS__" not in source and "try{\n\n}" not in admin_server.app_html(None)
