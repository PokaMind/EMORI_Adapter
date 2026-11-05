"""``GET /api/robot/whoami``: one client, a strict contract, content-free failures.

Pure: respx stands in for Pokamind. What is pinned is the contract this robot and
Pokamind agreed on -- the URL convention, the EdDSA assertion it authenticates with,
the exact response shape -- and that nothing the server sends can
leak into a result except the fields the contract names.
"""

from __future__ import annotations

import copy

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from emori_adapter import robot_token
from emori_adapter.hostapp.pokamind_whoami import (
    CONTRACT_HEADER,
    CONTRACT_VERSION,
    RESULT_CODES,
    WhoamiError,
    fetch_whoami,
    parse_whoami,
    whoami_url,
)

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
BASE = "https://api.pokamind.com"
URL = BASE + "/api/robot/whoami"
TOKEN = robot_token.parse_token(GOLDEN_TOKEN)


def body(**overrides) -> dict:
    """A whoami answer for the golden robot."""

    value = {
        "robot_id": "test-robot-01",
        "display_name": "Demo reception",
        "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": "Vkdap1RjR0wChd9d",
                       "key_version": 1},
        "org": "example-org",
        "partner": "emori-robot",
        "course_id": "example-course",
        "languages": ["ca"],
        "pathways": ["Example pathway"],
        "identity_domains": [],
        "modules": [
            {"module_id": "example-ca-module-1", "title": "Atenció", "category":
             "Example pathway", "language": "ca", "available": True, "problem": None},
            {"module_id": "example-ca-module-2", "title": None, "category": None,
             "language": None, "available": False, "problem": "not_found"},
        ],
        "features": {"feature_a": False, "feature_b": False, "feature_c": True},
    }
    value.update(overrides)
    return value


# --- the URL convention -----------------------------------------------------------------


@pytest.mark.parametrize("base, url", [
    ("https://api.pokamind.com", URL),
    ("https://api.pokamind.com/", URL),
    (" https://api.pokamind.com ", URL),
    ("http://localhost:4000", "http://localhost:4000/api/robot/whoami"),
    ("http://127.0.0.1:4000/", "http://127.0.0.1:4000/api/robot/whoami"),
])
def test_whoami_lives_under_api_on_the_backend_origin(base, url):
    assert whoami_url(base) == url


@pytest.mark.parametrize("base", [
    "", None, "api.pokamind.com", "http://api.pokamind.com", "http://192.0.2.20:4000",
    "https://user:pw@api.pokamind.com", "https://api.pokamind.com?x=1",
    "https://api.pokamind.com#f", "ftp://api.pokamind.com",
])
def test_a_base_that_would_put_the_bearer_on_the_open_network_is_refused(base):
    with pytest.raises(WhoamiError) as caught:
        whoami_url(base)
    assert caught.value.code == "whoami_base_url_invalid"
    assert fetch_whoami(base, TOKEN).code == "whoami_base_url_invalid"


# --- the call ----------------------------------------------------------------------------


@respx.mock
def test_a_good_answer_is_parsed_and_the_request_is_an_eddsa_control_assertion():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=body()))

    result = fetch_whoami(BASE, TOKEN, now=1_785_000_000.0)

    assert (result.ok, result.code, result.http_status) == (True, "ok", 200)
    assert result.checked_at == 1_785_000_000.0
    who = result.whoami
    assert (who.robot_id, who.org, who.course_id, who.status) == (
        "test-robot-01", "example-org", "example-course", "active")
    assert who.languages == ("ca",)
    assert who.module("example-ca-module-1").available is True
    assert [m.module_id for m in who.problems()] == ["example-ca-module-2"]
    # A v1 body: contract 1, and to_dict is that body plus the contract-2 keys as null.
    assert (who.contract, who.ignored_keys) == (1, ())
    assert result.to_dict()["whoami"] == _as_dict(body())

    request = route.calls.last.request
    scheme, _, assertion = request.headers["authorization"].partition(" ")
    assert scheme == "Bearer"
    assert jwt.get_unverified_header(assertion) == {
        "alg": "EdDSA", "typ": "JWT", "kid": "test-robot-01"}
    claims = jwt.decode(assertion, Ed25519PublicKey.from_public_bytes(TOKEN.public_key),
                        algorithms=["EdDSA"], audience="pokamind-robot-control")
    assert claims["sub"] == "test-robot-01" and claims["exp"] - claims["iat"] == 45


@pytest.mark.parametrize("status, payload, code", [
    (401, {"error_code": "robot_revoked"}, "robot_revoked"),
    (401, {"error_code": "robot_unknown"}, "robot_unknown"),
    (401, {"error_code": "authentication_failed"}, "whoami_auth_failed"),
    (401, {"error_code": "something new"}, "whoami_auth_failed"),
    (403, {"error_code": "robot_revoked"}, "robot_revoked"),
    (401, {"error_code": "authentication_unavailable"}, "whoami_unavailable"),
    # 401/403 WITHOUT Pokamind's JSON is not Pokamind's answer (a captive portal, a site
    # proxy, a firewall): nothing is known about the token, so it is not a refusal.
    (403, None, "whoami_unreachable"),
    (401, None, "whoami_unreachable"),
    (403, {"error": "forbidden by policy"}, "whoami_unreachable"),
    (401, {"error_code": 7}, "whoami_unreachable"),
    (404, None, "whoami_not_supported"),
    (408, None, "whoami_timeout"),
    (429, None, "whoami_rate_limited"),
    (400, {"error_code": "robot_revoked"}, "whoami_rejected"),
    (503, {"error_code": "authentication_unavailable"}, "whoami_unavailable"),
    (500, None, "whoami_server_error"),
    (504, None, "whoami_timeout"),
])
@respx.mock
def test_a_refusal_maps_to_a_fixed_code_and_never_carries_the_body(status, payload, code):
    response = (httpx.Response(status, json=payload) if payload is not None
                else httpx.Response(status, text="<html>secret proxy page</html>"))
    respx.get(URL).mock(return_value=response)

    result = fetch_whoami(BASE, TOKEN)

    assert (result.ok, result.code, result.http_status, result.whoami) == (
        False, code, status, None)
    assert "secret" not in repr(result.to_dict())


@respx.mock
def test_transport_failures_are_unreachable_or_timeout():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom " + URL))
    assert fetch_whoami(BASE, TOKEN).code == "whoami_unreachable"
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    assert fetch_whoami(BASE, TOKEN).code == "whoami_timeout"


@respx.mock
def test_a_non_json_200_is_an_invalid_response():
    respx.get(URL).mock(return_value=httpx.Response(200, text="not json"))

    assert fetch_whoami(BASE, TOKEN).code == "invalid_whoami_response"


def test_no_token_means_nothing_is_asked():
    assert fetch_whoami(BASE, None).code == "no_robot_token"


def test_a_token_that_cannot_sign_is_its_own_code():
    class Broken:
        robot_id = "test-robot-01"

        def control_assertion(self):
            raise ImportError("cryptography")

    assert fetch_whoami(BASE, Broken()).code == "whoami_signing_unavailable"


# --- strict parsing ----------------------------------------------------------------------


def _parse(value):
    return parse_whoami(value, robot_id="test-robot-01", fingerprint="Vkdap1RjR0wChd9d")


def _with(path, value):
    raw = copy.deepcopy(body())
    target = raw
    for key in path[:-1]:
        target = target[key]
    if value is _DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return raw


_DROP = object()


@pytest.mark.parametrize("path, value", [
    (("surprise",), "a new server field"),
    (("org",), _DROP),
    (("status",), "suspended"),
    (("status",), None),
    (("robot_id",), 7),
    (("languages",), "ca"),
    (("languages",), ["ca", 3]),
    (("credential", "kind"), "partner"),
    (("credential", "key_version"), "1"),
    (("credential", "key_version"), True),
    (("credential", "extra"), 1),
    (("features", "feature_a"), "false"),
    (("features", "Not A Name"), True),
    (("modules",), {"example-ca-module-1": {}}),
    (("modules", 0, "available"), "yes"),
    (("modules", 0, "problem"), "deleted"),
    (("modules", 0, "module_id"), "modules/../users"),
    (("modules", 0, "notes"), "x"),
])
def test_anything_off_contract_is_an_invalid_response(path, value):
    with pytest.raises(WhoamiError) as caught:
        _parse(_with(path, value))
    assert caught.value.code == "invalid_whoami_response"


def test_an_answer_about_another_robot_or_another_key_is_not_believed():
    with pytest.raises(WhoamiError) as caught:
        _parse(body(robot_id="test-robot-02"))
    assert caught.value.code == "whoami_robot_id_mismatch"

    rotated = body(credential={"kind": "fleet", "key_fingerprint": "AAAAAAAAAAAAAAAA",
                               "key_version": 2})
    with pytest.raises(WhoamiError) as caught:
        _parse(rotated)
    assert caught.value.code == "whoami_key_mismatch"


def test_a_registry_robot_needs_no_fingerprint_or_version():
    registry = body(credential={"kind": "registry", "key_fingerprint": None,
                                "key_version": None}, modules=[], pathways=[])

    who = _parse(registry)

    assert (who.credential_kind, who.key_fingerprint, who.modules) == ("registry", None, ())


def test_every_result_code_is_safe_to_put_in_a_readycheck_line():
    from emori_adapter.hostapp.readycheck import SAFE_CODE_RE

    for code in RESULT_CODES:
        assert SAFE_CODE_RE.fullmatch(code), code


# --- contract 2: the header, the named-optional keys ---------------------------------------


def _as_dict(raw: dict) -> dict:
    """What ``Whoami.to_dict()`` gives for ``raw``: every contract-2 key, null when absent."""

    out = copy.deepcopy(raw)
    out.setdefault("default_language", None)
    out.setdefault("app_origin", None)
    out.setdefault("ignored_keys", [])
    for module in out["modules"]:
        module.setdefault("menu_name", None)
        module.setdefault("menu_keywords", None)
    return out


def body_v2(**overrides) -> dict:
    """A contract-2 answer carrying every optional key, all well formed."""

    value = body(languages=["ca", "en"], default_language="ca",
                 app_origin="https://app.example.test")
    value["modules"][0].update(menu_name="Atenció al client",
                               menu_keywords=["atenció", "client", "atenció al client"])
    value["modules"][1].update(menu_name=None, menu_keywords=None)
    value.update(overrides)
    return value


def test_every_request_opts_into_contract_2():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body())

    result = fetch_whoami(BASE, TOKEN, transport=httpx.MockTransport(handler))

    assert result.ok and len(seen) == 1
    assert (CONTRACT_HEADER, CONTRACT_VERSION) == ("X-EMORI-Contract", "2")
    assert seen[0].headers["x-emori-contract"] == "2"
    assert seen[0].headers["accept"] == "application/json"
    assert seen[0].url == httpx.URL(URL)


def test_a_v1_answer_parses_exactly_as_before():
    who = _parse(body())

    assert who.contract == 1 and who.ignored_keys == ()
    assert (who.default_language, who.app_origin) == (None, None)
    assert all(m.menu_name is None and m.menu_keywords is None for m in who.modules)
    assert who.to_dict() == _as_dict(body())


def test_a_v2_answer_carries_every_optional_key():
    who = _parse(body_v2())

    assert who.contract == 2 and who.ignored_keys == ()
    assert (who.default_language, who.app_origin) == ("ca", "https://app.example.test")
    first, second = who.modules
    assert first.menu_name == "Atenció al client"
    assert first.menu_keywords == ("atenció", "client", "atenció al client")
    assert (second.menu_name, second.menu_keywords) == (None, None)
    assert who.to_dict() == _as_dict(body_v2())


@pytest.mark.parametrize("raw", [
    body(default_language=None),                       # null is "absent", but the key is v2
    body(app_origin=None),
    body(modules=[dict(body()["modules"][0], menu_name=None)]),
])
def test_any_contract_2_key_marks_the_answer_contract_2(raw):
    who = _parse(raw)

    assert who.contract == 2 and who.ignored_keys == ()


def test_to_dict_round_trips_through_the_parser():
    for raw in (body(), body_v2()):
        who = _parse(raw)
        again = who.to_dict()
        again.pop("ignored_keys")

        reparsed = _parse(again)

        assert reparsed.to_dict() == who.to_dict()
        assert (reparsed.modules, reparsed.default_language, reparsed.app_origin) == (
            who.modules, who.default_language, who.app_origin)


def test_localhost_app_origins_are_accepted_for_local_testing():
    for origin in ("http://localhost:3000", "http://127.0.0.1:8080", "https://app.example.test:8443"):
        assert _parse(body_v2(app_origin=origin)).app_origin == origin


@pytest.mark.parametrize("key, value", [
    ("default_language", "CA"),                       # not a lowercase base code
    ("default_language", "ca-ES"),
    ("default_language", "es"),                       # not one of languages
    ("default_language", 7),
    ("default_language", ""),
    ("app_origin", "https://app.example.test/path"),
    ("app_origin", "https://app.example.test/"),
    ("app_origin", "http://app.example.test"),        # http only for loopback
    ("app_origin", "https://user:pw@app.example.test"),
    ("app_origin", "https://app.example.test?x=1"),
    ("app_origin", "https://app.example.test#frag"),
    ("app_origin", "https://app.example.test:99999"),
    ("app_origin", "app.example.test"),
    ("app_origin", "https://" + "a" * 200 + ".test"),
    ("app_origin", ["https://app.example.test"]),
])
def test_a_malformed_top_level_optional_value_is_ignored_by_name(key, value):
    who = _parse(body_v2(**{key: value}))

    assert getattr(who, key) is None
    assert who.ignored_keys == (key,)
    assert who.contract == 2
    assert repr(value) not in repr(who.ignored_keys)   # names only, never the value


def test_default_language_must_be_listed_only_when_languages_is_not_empty():
    assert _parse(body_v2(languages=[], default_language="ca")).default_language == "ca"
    # A base-code match counts as listed.
    assert _parse(body_v2(languages=["ca-ES"], default_language="ca")).default_language == "ca"


@pytest.mark.parametrize("key, value", [
    ("menu_name", "Escolta, activa"),                  # a comma breaks the grammar
    ("menu_name", "Escolta\nactiva"),
    ("menu_name", "Escolta\ractiva"),
    ("menu_name", ""),
    ("menu_name", "   "),
    ("menu_name", "x" * 61),
    ("menu_name", 7),
    ("menu_keywords", ["ok"] * 13),
    ("menu_keywords", ["ok", "a,b"]),
    ("menu_keywords", ["ok", "line\nbreak"]),
    ("menu_keywords", ["ok", ""]),
    ("menu_keywords", ["ok", "x" * 41]),
    ("menu_keywords", ["ok", 3]),
    ("menu_keywords", "escolta"),
])
def test_a_malformed_module_optional_value_is_ignored_by_name(key, value):
    raw = body_v2()
    raw["modules"][1][key] = value

    who = _parse(raw)

    assert getattr(who.modules[1], key) is None
    assert who.ignored_keys == (f"modules[1].{key}",)
    # The rest of the answer, and the other module's copy, are untouched.
    assert who.modules[0].menu_name == "Atenció al client"
    assert who.default_language == "ca"


def test_several_malformed_values_are_all_listed_and_the_body_still_parses():
    raw = body_v2(app_origin="ftp://x.test", default_language="xx")
    raw["modules"][0]["menu_keywords"] = ["a,b"]

    who = _parse(raw)

    assert who.ignored_keys == ("modules[0].menu_keywords", "default_language", "app_origin")
    assert who.modules[0].menu_name == "Atenció al client"
    assert who.to_dict()["ignored_keys"] == list(who.ignored_keys)


@pytest.mark.parametrize("path, value", [
    (("menu_order",), ["x"]),                          # not a contract-2 key at all
    (("capabilities",), []),
    (("credential", "default_language"), "ca"),        # optional keys live where named
    (("features", "app_origin"), "https://x.test"),
    (("modules", 0, "default_language"), "ca"),
    (("modules", 0, "menu"), "x"),
])
def test_an_unknown_key_is_still_an_invalid_response(path, value):
    raw = body_v2()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(WhoamiError) as caught:
        _parse(raw)
    assert caught.value.code == "invalid_whoami_response"


@respx.mock
def test_a_v2_answer_through_the_client_reports_contract_2():
    respx.get(URL).mock(return_value=httpx.Response(200, json=body_v2()))

    result = fetch_whoami(BASE, TOKEN)

    assert result.ok and result.whoami.contract == 2
    assert result.to_dict()["whoami"] == _as_dict(body_v2())
