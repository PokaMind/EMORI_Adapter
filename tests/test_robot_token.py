"""THIS robot's own Pokamind token: the string, the key, the two JWTs, the file.

The golden vector is shared with Pokamind, which asserts the very same three values:
if either side ever derives a different public key or fingerprint from the same token,
no robot can log in, so both sides pin it.
"""

from __future__ import annotations

import base64
import os
import stat
import sys
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from emori_adapter import robot_token
from emori_adapter.hostapp.jwt_sso import build_sso_url, mint_robot_sso_jwt
from emori_adapter.robot_token import TokenError, parse_token

GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
GOLDEN_PUBLIC_KEY = "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg"
GOLDEN_FINGERPRINT = "Vkdap1RjR0wChd9d"
GOLDEN_SEED = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _public_key(token) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(token.public_key)


# --- the golden vector ----------------------------------------------------------------


def test_the_golden_vector_derives_the_agreed_key_and_fingerprint():
    token = parse_token(GOLDEN_TOKEN)

    assert token.robot_id == "test-robot-01"
    assert token.public_key == bytes.fromhex(
        "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
    assert token.public_key_b64u == GOLDEN_PUBLIC_KEY
    assert token.fingerprint == GOLDEN_FINGERPRINT
    # The seed IS bytes 0..31 -- the vector is what it says it is.
    assert base64.urlsafe_b64decode(GOLDEN_SEED + "=") == bytes(range(32))
    assert token.reveal() == GOLDEN_TOKEN


def test_the_masked_form_is_the_only_form_a_repr_or_a_log_line_can_carry():
    token = parse_token(GOLDEN_TOKEN)

    assert token.masked == "pkr1.test-robot-01.…dHh8"
    for shown in (repr(token), str(token), f"{token}", "%s" % (token,), "%r" % (token,)):
        assert GOLDEN_SEED not in shown
        assert token.masked in shown
    described = robot_token.TokenLoad(token=token, source="file").describe()
    assert GOLDEN_SEED not in repr(described)
    assert described["masked"] == token.masked


def test_mask_never_echoes_a_value_that_is_not_a_token():
    assert robot_token.mask(GOLDEN_TOKEN) == "pkr1.test-robot-01.…dHh8"
    pasted = "my-password-123"
    assert pasted not in robot_token.mask(pasted)


# --- parsing --------------------------------------------------------------------------


def test_surrounding_whitespace_is_forgiven_but_nothing_inside():
    assert parse_token(f"  {GOLDEN_TOKEN}\n").robot_id == "test-robot-01"
    with pytest.raises(TokenError) as caught:
        parse_token(GOLDEN_TOKEN[:30] + " " + GOLDEN_TOKEN[30:])
    assert caught.value.code == "token_malformed"


@pytest.mark.parametrize("value, code", [
    ("", "token_empty"),
    ("   \n", "token_empty"),
    (None, "token_malformed"),
    ("pkr1.test-robot-01", "token_malformed"),
    ("pkr1.a.b.c", "token_malformed"),
    ("pkr2.test-robot-01." + GOLDEN_SEED, "token_version_unsupported"),
    ("tok1.test-robot-01." + GOLDEN_SEED, "token_malformed"),
    ("pkr1.Test-Robot-01." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1.ab." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1.-robot." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1.robot-." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1.robot_01." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1." + "a" * 64 + "." + GOLDEN_SEED, "robot_id_invalid"),
    ("pkr1.test-robot-01." + GOLDEN_SEED[:-1], "seed_invalid"),
    ("pkr1.test-robot-01." + GOLDEN_SEED + "A", "seed_invalid"),
    ("pkr1.test-robot-01." + GOLDEN_SEED + "=", "seed_invalid"),
    ("pkr1.test-robot-01." + GOLDEN_SEED[:-1] + "+", "seed_invalid"),
    # The last character carries two spare bits: "h9" decodes to the same 32 bytes as
    # "h8", and accepting both would let two strings mean one key.
    ("pkr1.test-robot-01." + GOLDEN_SEED[:-1] + "9", "seed_invalid"),
    ("pkr1.test-robot-01." + "é" * 43, "token_malformed"),
])
def test_a_bad_token_is_refused_with_a_code_that_never_quotes_it(value, code):
    with pytest.raises(TokenError) as caught:
        parse_token(value)

    assert caught.value.code == code
    assert str(caught.value) == code


def test_the_longest_and_shortest_robot_ids_are_accepted():
    for robot_id in ("abc", "a" * 63, "reception-01", "7-x-9"):
        assert parse_token(f"pkr1.{robot_id}.{GOLDEN_SEED}").robot_id == robot_id


def test_tokens_compare_by_value_and_two_generated_ones_differ():
    assert parse_token(GOLDEN_TOKEN) == parse_token(GOLDEN_TOKEN + "\n")
    first, second = robot_token.generate("bench-01"), robot_token.generate("bench-01")
    assert first != second
    assert parse_token(first.reveal()) == first


# --- the control-plane assertion -------------------------------------------------------


def test_the_control_assertion_is_eddsa_with_kid_and_todays_exact_claim_set():
    token = parse_token(GOLDEN_TOKEN)

    encoded = token.control_assertion(now=1_785_000_000)
    header = jwt.get_unverified_header(encoded)
    claims = jwt.decode(encoded, _public_key(token), algorithms=["EdDSA"],
                        audience="pokamind-robot-control", options={"verify_exp": False})

    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": "test-robot-01"}
    assert set(claims) == {"sub", "aud", "iat", "exp", "jti"}
    assert claims["sub"] == "test-robot-01"
    assert claims["exp"] - claims["iat"] == 45
    assert token.control_assertion(now=1_785_000_000) != encoded  # a fresh jti each time


def test_a_signature_from_another_robot_does_not_verify():
    mine, other = parse_token(GOLDEN_TOKEN), robot_token.generate("test-robot-01")

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(other.control_assertion(), _public_key(mine), algorithms=["EdDSA"],
                   audience="pokamind-robot-control")


@pytest.mark.parametrize("now", [-1, 1.5, "0", True])
def test_the_assertion_refuses_a_malformed_clock(now):
    with pytest.raises(ValueError):
        parse_token(GOLDEN_TOKEN).control_assertion(now=now)


# --- the training SSO token ------------------------------------------------------------


def test_the_robot_sso_token_carries_the_partner_claims_plus_robot_id_aud_and_jti():
    token = parse_token(GOLDEN_TOKEN)

    encoded = mint_robot_sso_jwt(
        token, student_id="robot-test-robot-01", email="robot-test-robot-01@emori.local",
        course_id="example-course", now_ts=1_785_000_000,
        module_id="example-ca-module-1", name="EMORI",
        extra={"emori_lang": "ca", "emori_kiosk": True})
    header = jwt.get_unverified_header(encoded)
    claims = jwt.decode(encoded, _public_key(token), algorithms=["EdDSA"],
                        audience="pokamind-partner-sso", options={"verify_exp": False})

    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": "test-robot-01"}
    assert claims["robot_id"] == header["kid"]
    assert claims["aud"] == "pokamind-partner-sso"
    assert claims["exp"] - claims["iat"] == 600  # the robot-side ttl
    assert claims["jti"]
    for key, value in (("student_id", "robot-test-robot-01"),
                       ("email", "robot-test-robot-01@emori.local"),
                       ("course_id", "example-course"),
                       ("module_id", "example-ca-module-1"), ("name", "EMORI"),
                       ("emori_lang", "ca"), ("emori_kiosk", True)):
        assert claims[key] == value
    assert "pathway" not in claims  # optional claims stay absent when empty, as before


def test_a_robot_sso_token_the_backend_would_refuse_is_refused_here():
    token = parse_token(GOLDEN_TOKEN)
    base = dict(student_id="s", email="s@emori.local", course_id="c")

    for ttl in (0, -5, 901, 3600):
        with pytest.raises(ValueError):
            mint_robot_sso_jwt(token, ttl_s=ttl, **base)
    # A caller can never make the body disagree with the header, or reuse a jti.
    for claim in ("robot_id", "aud", "jti", "iat", "exp"):
        with pytest.raises(ValueError):
            mint_robot_sso_jwt(token, extra={claim: "x"}, **base)
    assert mint_robot_sso_jwt(token, ttl_s=900, **base)


def test_the_sso_url_is_unchanged_for_the_robot_token():
    url = build_sso_url("https://api.pokamind.com/", "emori-robot", "a.b.c")

    assert url == "https://api.pokamind.com/partner/sso/login?token=a.b.c&partner=emori-robot"


# --- where the token lives --------------------------------------------------------------


def test_the_token_file_path_defaults_under_the_working_directory(tmp_path):
    assert robot_token.token_file_path({}, cwd=tmp_path) == (
        tmp_path / "secrets" / "pokamind_robot_token")
    assert robot_token.token_file_path(
        {"EMORI_POKAMIND_TOKEN_FILE": "keys/t"}, cwd=tmp_path) == tmp_path / "keys" / "t"
    absolute = tmp_path / "elsewhere" / "token"
    assert robot_token.token_file_path(
        {"EMORI_POKAMIND_TOKEN_FILE": str(absolute)}, cwd="/nowhere") == absolute


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii", newline="\n")
    return path


def test_the_file_wins_over_the_env_and_an_empty_file_falls_through(tmp_path):
    other = robot_token.generate("dev-bench-01").reveal()
    env = {"POKAMIND_ROBOT_TOKEN": other}

    assert robot_token.load_token(env, cwd=tmp_path).source == "env"
    _write(tmp_path / "secrets" / "pokamind_robot_token", "\n")
    assert robot_token.load_token(env, cwd=tmp_path).token.robot_id == "dev-bench-01"
    _write(tmp_path / "secrets" / "pokamind_robot_token", GOLDEN_TOKEN + "\n")
    load = robot_token.load_token(env, cwd=tmp_path)
    assert (load.source, load.state, load.token.robot_id) == ("file", "configured",
                                                              "test-robot-01")


def test_nothing_configured_is_the_legacy_state():
    load = robot_token.load_token({}, cwd=os.devnull + "-not-a-dir")

    assert (load.token, load.source, load.error, load.state) == (None, "none", "", "none")


def test_a_broken_file_is_an_error_and_never_falls_back_to_the_env(tmp_path):
    _write(tmp_path / "secrets" / "pokamind_robot_token", GOLDEN_TOKEN[:-1] + "\n")

    load = robot_token.load_token({"POKAMIND_ROBOT_TOKEN": GOLDEN_TOKEN}, cwd=tmp_path)

    assert (load.token, load.source, load.error, load.state) == (
        None, "file", "seed_invalid", "invalid")


def test_a_huge_or_binary_token_file_is_refused_without_being_read_whole(tmp_path):
    path = _write(tmp_path / "secrets" / "pokamind_robot_token", "x" * 10_000)
    assert robot_token.load_token({}, cwd=tmp_path).error == "token_file_too_large"
    path.write_bytes(b"\xff\xfe" + GOLDEN_TOKEN.encode())
    assert robot_token.load_token({}, cwd=tmp_path).error == "token_malformed"


def test_a_bad_env_token_is_reported_under_the_env_source():
    load = robot_token.load_token({"POKAMIND_ROBOT_TOKEN": "nope"}, cwd=os.devnull + "-x")

    assert (load.source, load.error) == ("env", "token_malformed")


def test_save_writes_atomically_and_round_trips(tmp_path):
    token = parse_token(GOLDEN_TOKEN)
    target = tmp_path / "secrets" / "pokamind_robot_token"

    assert robot_token.save_token(token, target) == target
    assert target.read_text(encoding="ascii") == GOLDEN_TOKEN + "\n"
    replacement = robot_token.generate("test-robot-01")
    robot_token.save_token(replacement, target)
    assert robot_token.load_token({"EMORI_POKAMIND_TOKEN_FILE": str(target)}).token == replacement
    # No temp file is ever left beside it; the token it replaced is, for a roll-back.
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "pokamind_robot_token", "pokamind_robot_token.prev"]
    assert (target.parent / "pokamind_robot_token.prev").read_text(
        encoding="ascii") == GOLDEN_TOKEN + "\n"


def test_saving_the_same_token_again_keeps_the_real_previous_one(tmp_path):
    target = tmp_path / "secrets" / "pokamind_robot_token"
    robot_token.save_token(parse_token(GOLDEN_TOKEN), target)
    assert not (target.parent / "pokamind_robot_token.prev").exists()  # nothing replaced
    newer = robot_token.generate("test-robot-01")

    robot_token.save_token(newer, target)
    robot_token.save_token(newer, target)

    assert (target.parent / "pokamind_robot_token.prev").read_text(
        encoding="ascii") == GOLDEN_TOKEN + "\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_save_makes_the_directory_and_the_file_private(tmp_path):
    target = tmp_path / "secrets" / "pokamind_robot_token"

    robot_token.save_token(parse_token(GOLDEN_TOKEN), target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert robot_token.load_token({"EMORI_POKAMIND_TOKEN_FILE": str(target)}) \
        .insecure_permissions is False
    target.chmod(0o644)
    assert robot_token.load_token({"EMORI_POKAMIND_TOKEN_FILE": str(target)}) \
        .insecure_permissions is True
    # The replaced token kept for a roll-back is as private as the token.
    robot_token.save_token(robot_token.generate("test-robot-01"), target)
    previous = target.parent / "pokamind_robot_token.prev"
    assert stat.S_IMODE(previous.stat().st_mode) == 0o600


def test_save_refuses_anything_but_a_parsed_token(tmp_path):
    with pytest.raises(TypeError):
        robot_token.save_token(GOLDEN_TOKEN, tmp_path / "t")  # type: ignore[arg-type]


# --- the process binding and the robot_id cross-check ----------------------------------


def test_the_process_binds_its_token_once(monkeypatch, tmp_path):
    target = _write(tmp_path / "t", GOLDEN_TOKEN + "\n")
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(target))
    robot_token.reset_configured()

    first = robot_token.configured()
    target.write_text(robot_token.generate("someone-else").reveal(), encoding="ascii")

    assert robot_token.configured() is first  # a new file takes effect at a restart
    robot_token.reset_configured()
    assert robot_token.configured().token.robot_id == "someone-else"


@pytest.mark.parametrize("env_value, conflict", [
    ("", False),
    ("   ", False),
    ("test-robot-01", False),
    ("Test-Robot-01", False),       # normalised exactly like identity.py's account slug
    (" test robot 01 ", False),
    ("ward-3", True),
])
def test_a_robot_id_conflict_needs_a_set_and_different_emori_robot_id(env_value, conflict):
    assert robot_token.robot_id_conflict(parse_token(GOLDEN_TOKEN), env_value) is conflict
    assert robot_token.robot_id_conflict(None, env_value) is False


# --- where the token file is, whoever asks and from wherever ----------------------------
# The default `secrets/pokamind_robot_token` must not be resolved against the CURRENT
# DIRECTORY: any tool run outside /opt/emori would silently find no token and fall back
# to the legacy fleet secret, which Pokamind refuses for a robot that has a token.


@pytest.fixture()
def install(monkeypatch, tmp_path):
    """An install root (/opt/emori) and a shell somewhere else entirely."""

    root, elsewhere = tmp_path / "opt-emori", tmp_path / "home-pollen"
    root.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(robot_token, "install_root", lambda: root)
    monkeypatch.delenv("EMORI_POKAMIND_TOKEN_FILE", raising=False)
    monkeypatch.delenv("POKAMIND_ROBOT_TOKEN", raising=False)
    robot_token.reset_configured()
    yield root, elsewhere
    robot_token.reset_configured()


def test_a_relative_token_file_is_relative_to_the_install_not_the_cwd(install):
    root, _ = install

    assert robot_token.token_file_path({}) == root / "secrets" / "pokamind_robot_token"
    assert robot_token.token_file_path(
        {"EMORI_POKAMIND_TOKEN_FILE": "keys/t"}) == root / "keys" / "t"


def test_the_working_directory_is_only_the_last_resort(install, monkeypatch):
    _, elsewhere = install
    monkeypatch.setattr(robot_token, "install_root", lambda: None)

    assert robot_token.token_file_path({}) == elsewhere / "secrets" / "pokamind_robot_token"


def test_a_tool_run_from_another_directory_still_finds_the_token(install):
    root, _ = install
    saved = robot_token.save_token(parse_token(GOLDEN_TOKEN))

    load = robot_token.load_token({})

    assert saved == root / "secrets" / "pokamind_robot_token"
    assert load.source == "file" and load.token == parse_token(GOLDEN_TOKEN)


def test_readycheck_the_hostapp_and_the_admin_tab_agree_on_the_token_file(install):
    """Three readers, one file: the next start, this process, and the phone's Pokamind tab."""

    from emori_adapter.hostapp import admin_api, readycheck

    root, _ = install
    expected = root / "secrets" / "pokamind_robot_token"
    robot_token.save_token(parse_token(GOLDEN_TOKEN))

    by_readycheck = readycheck.SystemProbe().robot_token()
    by_hostapp = robot_token.configured()
    tab = admin_api.PokamindAdmin(load=by_hostapp)

    assert by_readycheck.path == by_hostapp.path == str(expected)
    assert by_readycheck.token == by_hostapp.token == parse_token(GOLDEN_TOKEN)
    # The tab reads the same file to tell "saved but not in use yet" ...
    assert tab._read_file().path == str(expected)
    assert tab.status()[1]["restart_pending"] is False
    # ... and a replacement lands where the next hostapp start will look.
    replacement = robot_token.generate("bench-02")
    assert tab._save(replacement) == expected
    assert readycheck.SystemProbe().robot_token().token == replacement
    assert tab.status()[1]["restart_pending"] is True


def test_the_install_root_is_the_env_above_where_config_lives(monkeypatch, tmp_path):
    import types

    install = tmp_path / "opt" / "emori"
    (install / "src" / "emori_adapter").mkdir(parents=True)
    (install / ".env").write_text("X=1\n", encoding="utf-8")
    config_file = install / "src" / "emori_adapter" / "config.py"
    # DOTENV_FILE is deliberately elsewhere: install_root walks, it does not trust a
    # value some other search produced.
    monkeypatch.setitem(sys.modules, "emori_adapter.config", types.SimpleNamespace(
        __file__=str(config_file), DOTENV_FILE=str(tmp_path / "elsewhere" / ".env")))

    assert robot_token.install_root() == Path(os.path.abspath(install))


def test_without_a_loaded_env_the_install_root_is_found_from_the_package(monkeypatch):
    import types

    monkeypatch.setitem(sys.modules, "emori_adapter.config",
                        types.SimpleNamespace(DOTENV_FILE=""))

    root = robot_token.install_root()

    assert root is not None
    assert (root / ".env").is_file() or (root / "pyproject.toml").is_file()
    assert Path(robot_token.__file__).resolve().is_relative_to(root)


def test_config_records_the_env_file_it_loaded():
    import emori_adapter.config as config

    assert isinstance(config.DOTENV_FILE, str)
    if config.DOTENV_FILE:
        assert Path(config.DOTENV_FILE).name == ".env"
        assert Path(config.DOTENV_FILE).is_file()


def test_a_token_file_somebody_configured_that_is_missing_warns_once(caplog, tmp_path):
    missing = tmp_path / "keys" / "pokamind_robot_token"
    env = {"EMORI_POKAMIND_TOKEN_FILE": str(missing), "POKAMIND_ROBOT_TOKEN": GOLDEN_TOKEN}

    with caplog.at_level("WARNING", logger="emori.robot_token"):
        first = robot_token.load_token(env)
        robot_token.load_token(env)

    warnings = [r for r in caplog.records if r.name == "emori.robot_token"]
    assert len(warnings) == 1
    assert str(missing) in warnings[0].getMessage()
    assert GOLDEN_SEED not in caplog.text
    assert first.source == "env"  # the warning changes nothing else


def test_the_default_token_file_being_absent_is_not_a_warning(caplog, tmp_path):
    with caplog.at_level("WARNING", logger="emori.robot_token"):
        load = robot_token.load_token({}, cwd=tmp_path)

    assert load.state == "none"
    assert not [r for r in caplog.records if r.name == "emori.robot_token"]


def test_a_tool_started_outside_the_install_resolves_the_install_token_file(tmp_path):
    """The real thing, in a fresh interpreter started from another directory."""

    import subprocess

    src = Path(robot_token.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items()
           if k not in ("EMORI_POKAMIND_TOKEN_FILE", "POKAMIND_ROBOT_TOKEN")}
    env["PYTHONPATH"] = str(src)
    env["EMORI_POKAMIND_TOKEN_FILE"] = "secrets/pokamind_robot_token"
    result = subprocess.run(
        [sys.executable, "-c",
         "import emori_adapter.config\n"
         "from emori_adapter import robot_token\n"
         "print(robot_token.token_file_path())\n"
         "print(robot_token.install_root())"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    path, root = (Path(line) for line in result.stdout.strip().splitlines())
    assert path == root / "secrets" / "pokamind_robot_token"
    assert not path.is_relative_to(tmp_path)
    assert (root / ".env").is_file() or (root / "pyproject.toml").is_file()


@pytest.mark.parametrize("how", ["python -c", "under a debugger"])
def test_a_tool_started_next_to_another_env_still_uses_the_install(tmp_path, how):
    """python-dotenv's find_dotenv() walks from the WORKING DIRECTORY under `python -c`,
    a REPL or a debugger: started in a directory with its own .env, config loaded that
    one and the token was looked for next to it."""

    import subprocess

    (tmp_path / ".env").write_text("EMORI_DECOY_ENV=loaded\n", encoding="utf-8")
    src = Path(robot_token.__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items()
           if k not in ("EMORI_POKAMIND_TOKEN_FILE", "POKAMIND_ROBOT_TOKEN",
                        "EMORI_DECOY_ENV")}
    env["PYTHONPATH"] = str(src)
    body = ("import os, sys\n"
            "import emori_adapter.config as config\n"
            "from emori_adapter import robot_token\n"
            "print(robot_token.install_root())\n"
            "print(config.DOTENV_FILE)\n"
            "print(os.environ.get('EMORI_DECOY_ENV', '-'))\n")
    if how == "python -c":
        argv = [sys.executable, "-c", body]
    else:
        script = tmp_path / "tool.py"
        script.write_text("import sys\nsys.settrace(lambda *a: None)\n" + body,
                          encoding="utf-8")
        argv = [sys.executable, str(script)]
    result = subprocess.run(argv, cwd=str(tmp_path), env=env, capture_output=True,
                            text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    root, dotenv, decoy = result.stdout.strip().splitlines()
    assert not Path(root).is_relative_to(tmp_path)
    assert not dotenv or not Path(dotenv).is_relative_to(tmp_path)
    assert decoy == "-"
