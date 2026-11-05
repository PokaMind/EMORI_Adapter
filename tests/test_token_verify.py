"""``python -m emori_adapter.hostapp.token_verify``: the robot's own check of a candidate
token, run by ``deploy/robot/install-token.sh`` before the candidate replaces anything.

The admin page's whoami, with three answers instead of two: Pokamind said yes
(verified), Pokamind said no (refused: the robot keeps its old token), or Pokamind could
not be asked (unverified: a first install on a robot with no internet must still work).
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
import respx

from emori_adapter import robot_token
from emori_adapter.hostapp import token_verify
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult
from tests.test_readycheck import GOLDEN_TOKEN, whoami

SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]
TOKEN = robot_token.parse_token(GOLDEN_TOKEN)


def _asks(answer):
    calls = []

    def ask(base_url, token):
        calls.append((base_url, token))
        return answer

    ask.calls = calls
    return ask


def test_an_active_robot_holding_this_key_is_verified():
    ask = _asks(whoami())

    verdict = token_verify.judge(TOKEN, base_url="https://api.example.test", whoami=ask)

    assert (verdict.verdict, verdict.code, verdict.org) == ("verified", "ok", "example-org")
    assert ask.calls == [("https://api.example.test", TOKEN)]  # signed with the CANDIDATE


@pytest.mark.parametrize("code", ["robot_unknown", "robot_revoked", "whoami_auth_failed",
                                  "whoami_key_mismatch", "whoami_robot_id_mismatch"])
def test_pokamind_saying_no_is_a_refusal(code):
    verdict = token_verify.judge(TOKEN, base_url="https://x.test",
                                 whoami=_asks(WhoamiResult(code=code, http_status=401)))

    assert (verdict.verdict, verdict.code) == ("refused", code)
    assert verdict.reason == token_verify.REFUSAL_REASONS[code]


def test_a_robot_listed_but_not_active_is_refused():
    answer = whoami()
    revoked = dataclasses.replace(
        answer, whoami=dataclasses.replace(answer.whoami, status="revoked"))

    verdict = token_verify.judge(TOKEN, base_url="https://x.test", whoami=_asks(revoked))

    assert (verdict.verdict, verdict.code, verdict.status) == (
        "refused", "robot_not_active", "revoked")


@pytest.mark.parametrize("code", ["whoami_unreachable", "whoami_timeout", "whoami_unavailable",
                                  "whoami_server_error", "whoami_rate_limited",
                                  "whoami_not_supported", "whoami_base_url_invalid",
                                  "whoami_signing_unavailable", "invalid_whoami_response",
                                  "whoami_rejected"])
def test_pokamind_not_answering_is_unverified_not_refused(code):
    verdict = token_verify.judge(TOKEN, base_url="https://x.test",
                                 whoami=_asks(WhoamiResult(code=code)))

    assert (verdict.verdict, verdict.code) == ("unverified", code)
    assert "UNVERIFIED" in verdict.reason


def test_a_whoami_that_raises_is_unverified():
    def boom(base_url, token):
        raise RuntimeError("network stack on fire")

    assert token_verify.judge(TOKEN, base_url="https://x.test", whoami=boom).code == (
        "whoami_unreachable")


def test_an_env_robot_id_naming_another_robot_is_refused_without_asking():
    ask = _asks(whoami())

    verdict = token_verify.judge(TOKEN, base_url="https://x.test", env_robot_id="ward-3",
                                 whoami=ask)

    assert (verdict.verdict, verdict.code) == ("refused", "robot_id_mismatch")
    assert ask.calls == []
    # The same id written another way is the same robot ...
    assert token_verify.judge(TOKEN, base_url="https://x.test", env_robot_id="Test Robot 01",
                              whoami=_asks(whoami())).verdict == "verified"
    # ... and a human's "install it anyway" at the deploy keyboard lets it through.
    assert token_verify.judge(TOKEN, base_url="https://x.test", env_robot_id="ward-3",
                              allow_robot_id_change=True,
                              whoami=_asks(whoami())).verdict == "verified"


def _main(tmp_path, monkeypatch, capsys, content, answer, *args):
    path = tmp_path / ".pokamind_robot_token.candidate"
    path.write_text(content, encoding="ascii")
    seen = []

    def fake_fetch(base_url, token, **_):
        seen.append(base_url)
        return answer

    monkeypatch.setattr(token_verify, "fetch_whoami", fake_fetch)
    monkeypatch.delenv("EMORI_ROBOT_ID", raising=False)
    rc = token_verify.main(["--token-file", str(path), *args])
    out = capsys.readouterr()
    assert SEED not in out.out and SEED not in out.err
    lines = dict(line.split("=", 1) for line in out.out.splitlines())
    return rc, lines, seen


def test_main_prints_key_values_and_exits_by_verdict(tmp_path, monkeypatch, capsys):
    rc, lines, seen = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN + "\r\n", whoami(),
                            "--base", "https://api.example.test")

    assert rc == 0
    assert lines["verdict"] == "verified" and lines["robot_id"] == "test-robot-01"
    assert lines["org"] == "example-org" and lines["status"] == "active"
    assert seen == ["https://api.example.test"]

    rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN,
                         WhoamiResult(code="robot_unknown"), "--base", "https://x.test")
    assert (rc, lines["verdict"], lines["code"]) == (2, "refused", "robot_unknown")

    rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN,
                         WhoamiResult(code="whoami_unreachable"), "--base", "https://x.test")
    assert (rc, lines["verdict"]) == (3, "unverified")


def test_main_reads_pokamind_base_and_the_robot_id_from_the_environment(tmp_path, monkeypatch,
                                                                        capsys):
    monkeypatch.setenv("POKAMIND_BASE", "https://api.robot-env.test")
    rc, _, seen = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami())

    assert rc == 0 and seen == ["https://api.robot-env.test"]
    rc, lines, seen = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami(),
                            "--env-robot-id", "ward-3")
    assert (rc, lines["code"], seen) == (2, "robot_id_mismatch", [])


@pytest.mark.parametrize("content", ["", "not a token\n", SEED + "\n"])
def test_main_refuses_a_file_that_is_not_a_token(tmp_path, monkeypatch, capsys, content):
    rc, lines, seen = _main(tmp_path, monkeypatch, capsys, content, whoami(),
                            "--base", "https://x.test")

    assert rc == token_verify.EXIT_NOT_A_TOKEN
    assert lines["verdict"] == "refused" and seen == []


@respx.mock
def test_a_proxys_403_is_not_pokamind_refusing_the_token():
    """A captive portal or a site proxy answering 401/403 with its own page says nothing
    about the token: refusing it would keep a valid token off the robot."""

    route = respx.get("https://api.example.test/api/robot/whoami")
    route.mock(return_value=httpx.Response(403, text="<html>Sign in to Guest Wi-Fi</html>"))

    verdict = token_verify.judge(TOKEN, base_url="https://api.example.test")

    assert (verdict.verdict, verdict.code) == ("unverified", "whoami_unreachable")
    # Pokamind's own JSON refusal is still a refusal.
    route.mock(return_value=httpx.Response(401, json={"error_code": "authentication_failed"}))
    assert token_verify.judge(TOKEN, base_url="https://api.example.test").verdict == "refused"


# --- the default origin and --write-scope ----------------------------------------------


def test_the_default_base_is_the_api_origin(tmp_path, monkeypatch, capsys):
    """app.pokamind.com cannot serve the robot routes; the API origin is the default."""

    from emori_adapter import site_settings

    assert token_verify._DEFAULT_BASE == site_settings.DEFAULT_POKAMIND_BASE
    assert token_verify._DEFAULT_BASE == "https://api.pokamind.com"
    monkeypatch.delenv("POKAMIND_BASE", raising=False)
    rc, _, seen = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami())

    assert rc == 0 and seen == ["https://api.pokamind.com"]


def _scope_file(root):
    from emori_adapter import site_settings

    return root / site_settings.SCOPE_FILE


def test_write_scope_records_a_verified_tokens_scope(tmp_path, monkeypatch, capsys):
    root = tmp_path / "install"
    rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami(),
                         "--base", "https://api.example.test", "--write-scope",
                         "--root", str(root))

    assert (rc, lines["verdict"], lines["scope"]) == (0, "verified", "written")
    assert "scope_code" not in lines
    import json

    snapshot = json.loads(_scope_file(root).read_text(encoding="utf-8"))
    assert snapshot["robot_id"] == "test-robot-01"
    assert snapshot["key_fingerprint"] == TOKEN.fingerprint
    assert snapshot["whoami"]["org"] == "example-org"


@pytest.mark.parametrize("answer, verdict", [
    (WhoamiResult(code="robot_unknown"), "refused"),
    (WhoamiResult(code="whoami_unreachable"), "unverified"),
    ("revoked", "refused"),
], ids=["refused", "unverified", "not-active"])
def test_write_scope_writes_nothing_unless_the_token_is_verified(tmp_path, monkeypatch,
                                                                  capsys, answer, verdict):
    if answer == "revoked":
        base = whoami()
        answer = dataclasses.replace(
            base, whoami=dataclasses.replace(base.whoami, status="revoked"))
    root = tmp_path / "install"

    _rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, answer,
                          "--base", "https://x.test", "--write-scope", "--root", str(root))

    assert lines["verdict"] == verdict and "scope" not in lines
    assert not _scope_file(root).exists()


def test_without_write_scope_nothing_is_written(tmp_path, monkeypatch, capsys):
    root = tmp_path / "install"
    rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami(),
                         "--base", "https://x.test", "--root", str(root))

    assert rc == 0 and "scope" not in lines
    assert not _scope_file(root).exists()


def test_a_scope_that_cannot_be_written_never_changes_the_verdict(tmp_path, monkeypatch,
                                                                  capsys):
    from emori_adapter import site_settings

    def refuse(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(site_settings, "write_scope_snapshot", refuse)
    rc, lines, _ = _main(tmp_path, monkeypatch, capsys, GOLDEN_TOKEN, whoami(),
                         "--base", "https://x.test", "--write-scope",
                         "--root", str(tmp_path / "install"))

    assert (rc, lines["verdict"]) == (0, "verified")
    assert (lines["scope"], lines["scope_code"]) == ("failed", "scope_write_failed")


def test_a_verified_verdict_carries_its_answer_but_never_prints_it():
    verdict = token_verify.judge(TOKEN, base_url="https://x.test", whoami=_asks(whoami()))

    assert verdict.whoami == whoami().whoami
    assert "whoami" not in repr(verdict).lower()
    assert all(not line.startswith("whoami") for line in verdict.lines())
    assert token_verify.write_scope(TOKEN, token_verify.Verdict(
        "refused", "robot_unknown", "test-robot-01"), base_url="https://x.test") == (
        "not_verified")
