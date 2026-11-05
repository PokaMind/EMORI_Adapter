"""Token in, robot configured: the provisioning promise, end to end.

A robot deployed from the public tree is given Wi-Fi and its Pokamind token, and
configures itself from its Pokamind robot record. This file proves that promise on the
real code path, with only Pokamind itself stood in:

* the robot is a temporary install tree laid out like ``/opt/emori`` -- a copy of the
  package, the shipped configs, and ``.env`` copied from the shipped ``.env.example``
  exactly as the installer leaves it (course, language, name, pathways and menus EMPTY);
* Pokamind is a loopback HTTP stand-in for ``GET /api/robot/whoami``. It checks the
  robot's EdDSA assertion against the token's public key, and follows the server rule of
  the wire contract: the contract-2 keys go only to a caller that sent
  ``X-EMORI-Contract: 2``, and an older server (``contract=1``) never sends them;
* the token goes in through the admin page's own route (the host app's
  ``POST /api/admin/pokamind/token``, which emori-admin proxies) or through the
  deploy's verifier (``token_verify --write-scope``, what install-token.sh runs);
* "the next start" is a REAL start: a fresh Python process, systemd's environment for
  the unit (``.env``, then the three robot-local settings files), ``emori_adapter.config``
  importing and applying the settings layer, then the host app module itself. It reports
  what that process runs with: settings, their sources, the boot language, the course,
  the robot's name and the spoken menu each language serves.

Asserted: the scope snapshot is written; the course, pathways, name, boot language,
menus (per language, numbered stably across a re-sync) are
derived and their provenance is ``pokamind``; a contract-2 record's ``menu_name`` and
``menu_keywords`` are the menu, a v1 record's titles are drafted into one; explicit
``.env`` values win; ``EMORI_SITE_AUTO=0`` derives nothing; a revoked or mismatched
whoami changes nothing.
"""

from __future__ import annotations

import contextlib
import copy
import http.server
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import admin_api, readycheck, server, token_verify
from emori_adapter.hostapp.voice_topics import VoiceMenu
from tests.test_admin_api import (
    HEADER,
    TOKEN,
    _FakeMicProfile,
    _RecordingPlayer,
)

_REPO = Path(__file__).resolve().parents[1]

# The golden test vector (the one full token the repository may hold).
GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
SEED = GOLDEN_TOKEN.rsplit(".", 1)[1]
ROBOT_ID = "test-robot-01"
FINGERPRINT = "Vkdap1RjR0wChd9d"

# Module ids shaped like real ones (no example-/dev- placeholder prefix).
LISTENING = "AAAAAAAAAAAAAAAAAAAA"
TEAMWORK = "BBBBBBBBBBBBBBBBBBBB"
FEEDBACK = "CCCCCCCCCCCCCCCCCCCC"
TIMEKEEPING = "DDDDDDDDDDDDDDDDDDDD"
GONE = "EEEEEEEEEEEEEEEEEEEE"
NEWCOMER = "FFFFFFFFFFFFFFFFFFFF"

DERIVED = ("EMORI_COURSE_ID", "EMORI_PATHWAYS", "EMORI_ROBOT_NAME", "EMORI_LANG")
_V2_TOP = ("default_language", "app_origin")
_V2_MODULE = ("menu_name", "menu_keywords")


def _module(module_id, title, language, *, menu_name=None, menu_keywords=None,
            available=True, problem=None):
    return {"module_id": module_id, "title": title, "category": "Example pathway",
            "language": language, "available": available, "problem": problem,
            "menu_name": menu_name, "menu_keywords": menu_keywords}


def record(**overrides) -> dict:
    """This robot's Pokamind record, in the contract-2 shape (the stand-in strips the
    contract-2 keys for a caller or a server that does not speak contract 2)."""

    value = {
        "robot_id": ROBOT_ID,
        "display_name": "Reception",
        "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": FINGERPRINT, "key_version": 1},
        "org": "example-org",
        "partner": "emori-robot",
        "course_id": "example-course",
        # Preference order: English first, so a v1 record boots in English while a
        # contract-2 record's default_language (Catalan) wins.
        "languages": ["en", "ca"],
        "pathways": ["Example pathway", "Second pathway"],
        "identity_domains": ["example.org"],
        "modules": [
            _module(LISTENING, "Active listening, part one", "ca",
                    menu_name="Escolta activa",
                    menu_keywords=["Escolta", "activa", "escolta activa"]),
            _module(FEEDBACK, "Giving feedback (level 1)", "en",
                    menu_name="Giving feedback", menu_keywords=["feedback"]),
            _module(TEAMWORK, "Teamwork: the basics", "ca",
                    menu_name="Treball en equip", menu_keywords=["equip"]),
            _module(TIMEKEEPING, "Time management, part one", "en",
                    menu_name="Time management", menu_keywords=["time"]),
            # Never offered: not available to this robot.
            {"module_id": GONE, "title": None, "category": None, "language": None,
             "available": False, "problem": "not_found", "menu_name": None,
             "menu_keywords": None},
        ],
        "features": {"feature_a": False, "feature_b": True, "feature_c": True},
        "default_language": "ca",
        "app_origin": "https://app.example.test",
    }
    value.update(overrides)
    return value


def v1_body(body: dict) -> dict:
    """Today's (v1) wire body of a record: the twelve v1 keys, v1 module rows."""

    out = {key: value for key, value in body.items() if key not in _V2_TOP}
    out["modules"] = [{key: value for key, value in row.items() if key not in _V2_MODULE}
                      for row in body["modules"]]
    return out


class FakePokamind:
    """A loopback stand-in for Pokamind's ``GET /api/robot/whoami``."""

    def __init__(self, robot: robot_token.RobotToken, *, contract: int) -> None:
        self.robot = robot             # the robot Pokamind knows (its public key)
        self.contract = contract       # 2 = honours the header; 1 = predates it
        self.record = record()
        self.reply: tuple[int, dict] | None = None   # a fixed error answer
        self.seen: list[tuple[str, str | None, bool]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's name
                status, payload = fake.answer(self.path, self.headers)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def _signed_by_the_robot(self, authorization: str) -> bool:
        import jwt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        scheme, _, assertion = authorization.partition(" ")
        if scheme != "Bearer" or not assertion:
            return False
        try:
            header = jwt.get_unverified_header(assertion)
            claims = jwt.decode(
                assertion, Ed25519PublicKey.from_public_bytes(self.robot.public_key),
                algorithms=["EdDSA"], audience=robot_token.CONTROL_AUDIENCE)
        except jwt.PyJWTError:
            return False
        return header.get("kid") == self.robot.robot_id == claims.get("sub")

    def answer(self, path: str, headers) -> tuple[int, dict]:
        contract = headers.get("X-EMORI-Contract")
        signed = self._signed_by_the_robot(headers.get("Authorization", ""))
        self.seen.append((path, contract, signed))
        if path != "/api/robot/whoami":
            return 404, {"error_code": "not_found"}
        if not signed:
            return 401, {"error_code": "invalid_assertion"}
        if self.reply is not None:
            return self.reply
        body = copy.deepcopy(self.record)
        # The server rule: the contract-2 keys only for a caller that asked for them,
        # and never from a server that predates the contract.
        if self.contract < 2 or contract != "2":
            body = v1_body(body)
        return 200, body


# --- the robot ------------------------------------------------------------------------

# The next start, in a fresh process: config (settings layer) then the host app module.
_NEXT_START = r'''
import json, os, sys
import yaml
from emori_adapter.hostapp import identity, lang_state, server
from emori_adapter import config, robot_token, site_settings
import emori_adapter


def menu(lang):
    path = server._menu_source(lang)
    loaded = server._load_menu(lang)
    authored = None
    if path:
        data = yaml.safe_load(open(path, encoding="utf-8").read())
        if isinstance(data, dict):
            authored = data.get("authored")
    return {"source": server.menu_source(lang), "path": path, "authored": authored,
            "prompt": loaded.prompt,
            "options": {key: [o.module_id, o.name, list(o.keywords)]
                        for key, o in loaded.options.items()}}


state = lang_state.LangState(default=server.EMORI_LANG,
                             allowed=lang_state.allowed_languages())
print("NEXT_START=" + json.dumps({
    "package": emori_adapter.__file__,
    "dotenv": config.DOTENV_FILE,
    "env": {key: os.environ.get(key) for key in site_settings.REGISTRY},
    "sources": site_settings.effective_sources(),
    "scope": site_settings.last_scope_status(),
    "boot_lang": server.EMORI_LANG,
    "kiosk_lang": state.current(),
    "allowed": list(state.allowed),
    "course": server.COURSE_ID,
    "effective_course": identity.effective_course_id(),
    "robot_name": identity.robot_name(),
    "pathways": list(identity.pathway_allowlist()),
    "menus": {lang: menu(lang) for lang in ("ca", "en")},
}))
'''


# What emori-readycheck's site_config gate sees on that robot (the same start path).
_READYCHECK = r'''
import dataclasses, json
import emori_adapter
from emori_adapter import robot_token
from emori_adapter.hostapp import readycheck

token = robot_token.configured().token
site = readycheck.SystemProbe().site_config(token.robot_id if token else "",
                                            token.fingerprint if token else "")
print("READYCHECK=" + json.dumps({"package": emori_adapter.__file__,
                                  "site": dataclasses.asdict(site)}))
'''


def site_config_gate(robot: "Robot", pokamind_base: str):
    """readycheck's site_config verdict for ``robot``: its own process's settings-layer
    probe, judged on an otherwise green robot."""

    from tests.test_readycheck import _by_name, _healthy

    raw = robot.run_probe(_READYCHECK, "READYCHECK")["site"]
    site = readycheck.SiteProbe(**{
        key: tuple(tuple(item) if isinstance(item, list) else item for item in value)
        if isinstance(value, list) else value for key, value in raw.items()})
    return _by_name(readycheck.evaluate(_healthy(site=site, pokamind_base=pokamind_base)))[
        "site_config"]


def _robot_owned(key: str) -> bool:
    """A variable systemd's unit environment would not carry from this test process."""

    if key in ("EMORI_LANG_STATE_FILE",):  # conftest's per-test (absent) state file
        return False
    return key.startswith(("EMORI_", "POKAMIND_", "ORION_", "NGSILD_", "QL_")) \
        or key == "PYTHONPATH"


@dataclass
class Robot:
    root: Path

    @property
    def token_file(self) -> Path:
        return self.root / "secrets" / "pokamind_robot_token"

    @property
    def scope_file(self) -> Path:
        return self.root / site_settings.SCOPE_FILE

    @property
    def auto_dir(self) -> Path:
        return self.root / site_settings.AUTO_MENU_DIR

    def auto_menu(self, lang: str) -> dict:
        return yaml.safe_load((self.auto_dir / f"voice_topics.{lang}.yaml").read_text(
            encoding="utf-8"))

    def env_set(self, **values: str) -> None:
        """install.sh's env_set: replace a ``KEY=`` line in .env, or append one."""

        path = self.root / ".env"
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            line = f"{key}={value}"
            pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
            if pattern.search(text):
                text = pattern.sub(lambda _m, line=line: line, text, count=1)
            else:
                text = text.rstrip("\n") + "\n" + line + "\n"
        path.write_text(text, encoding="utf-8")

    def unit_environment(self) -> dict[str, str]:
        """emori-hostapp.service's environment: .env, then the robot-local files."""

        from dotenv import dotenv_values

        env = {key: value for key, value in os.environ.items() if not _robot_owned(key)}
        # Whether an add-on is discovered is the test's, not the robot's: the child follows it.
        if "EMORI_ADDONS" in os.environ:
            env["EMORI_ADDONS"] = os.environ["EMORI_ADDONS"]
        for relative in (".env", site_settings.SETTINGS_FILE,
                         site_settings.SITE_SECRETS_FILE, site_settings.ADMIN_SECRETS_FILE):
            path = self.root / relative
            if path.exists():
                env.update({key: value for key, value in dotenv_values(path).items()
                            if value is not None})
        env["PYTHONPATH"] = str(self.root / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run_probe(self, code: str, marker: str, *args: str) -> dict:
        """Run ``code`` as the robot's host app process would start: its own tree, its
        working directory, systemd's environment. Returns the ``<marker>=`` JSON line."""

        result = subprocess.run([sys.executable, "-c", code, *args], cwd=self.root,
                                env=self.unit_environment(), capture_output=True,
                                timeout=180, check=False)
        out = result.stdout.decode("utf-8", "replace")
        err = result.stderr.decode("utf-8", "replace")
        assert result.returncode == 0, err[-4000:]
        # The token never reaches a log line or an output of the start.
        assert SEED not in out and SEED not in err
        line = next(item for item in out.splitlines() if item.startswith(marker + "="))
        report = json.loads(line.split("=", 1)[1])
        # It really was the robot's own tree that started.
        assert Path(report["package"]).resolve().is_relative_to(self.root.resolve())
        return report

    def next_start(self) -> dict:
        """Start the robot's host app process (to the point it has configured itself)."""

        report = self.run_probe(_NEXT_START, "NEXT_START")
        # ...configured from its own .env.
        assert Path(report["dotenv"]).resolve() == (self.root / ".env").resolve()
        return report

    def configured_environment(self) -> tuple[dict, dict]:
        """The same settings layer, in this process, on a copy of the unit environment."""

        env = self.unit_environment()
        sources = site_settings.apply(env, root=self.root,
                                      dotenv_file=str(self.root / ".env"))
        return env, sources


@pytest.fixture
def robot(tmp_path, monkeypatch) -> Robot:
    """``/opt/emori`` as the installer leaves a fresh robot: no token, no scope."""

    return build_robot(tmp_path, monkeypatch)


def build_robot(tmp_path, monkeypatch) -> Robot:
    """A fresh install tree (see :func:`robot`), and this process playing its host app."""

    root = tmp_path / "opt-emori"
    shutil.copytree(_REPO / "src" / "emori_adapter", root / "src" / "emori_adapter",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "configs").mkdir()
    for name in ("voice_topics.yaml", "voice_topics.ca.yaml", "voice_topics.example.yaml",
                 "roster.example.yaml", "behavior.yaml", "mapping.yaml"):
        shutil.copy2(_REPO / "configs" / name, root / "configs" / name)
    shutil.copy2(root / "configs" / "roster.example.yaml", root / "configs" / "roster.yaml")
    (root / "configs" / "site" / "auto").mkdir(parents=True)
    (root / "secrets").mkdir()
    shutil.copy2(_REPO / ".env.example", root / ".env")
    rig = Robot(root)
    # What install.sh step 8 adds: the hardware profile and the admin token.
    rig.env_set(EMORI_HARDWARE_PROFILE="reachy-mini-wireless", EMORI_ADMIN_TOKEN=TOKEN)

    # This process plays the host app of THAT robot: its install root, its token file,
    # and none of this machine's own settings.
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root_=None: Path(root_) if root_ is not None else root)
    monkeypatch.setattr(admin_api, "_dotenv_file", lambda: str(root / ".env"))
    for key in (*site_settings.REGISTRY, site_settings.SOURCES_ENV,
                site_settings.PROFILE_ENV, site_settings.AUTO_ENV, "EMORI_VOICE_TOPICS",
                "EMORI_VOICE_TOPICS_EN", "EMORI_VOICE_TOPICS_CA",
                "EMORI_ROBOT_ID", "POKAMIND_ROBOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(rig.token_file))
    monkeypatch.setenv("EMORI_ADMIN_TOKEN", TOKEN)
    robot_token.reset_configured()
    return rig


@pytest.fixture(autouse=True)
def _the_checkout_is_never_written():
    paths = [_REPO / site_settings.SCOPE_FILE, _REPO / site_settings.SETTINGS_FILE,
             _REPO / site_settings.AUTO_MENU_DIR / "voice_topics.ca.yaml",
             _REPO / site_settings.AUTO_MENU_DIR / "voice_topics.en.yaml",
             _REPO / "secrets" / "pokamind_robot_token"]
    before = [p.read_bytes() if p.exists() else None for p in paths]
    yield
    assert [p.read_bytes() if p.exists() else None for p in paths] == before


def _golden() -> robot_token.RobotToken:
    return robot_token.parse_token(GOLDEN_TOKEN)


@pytest.fixture
def pokamind_v2():
    fake = FakePokamind(_golden(), contract=2)
    yield fake
    fake.close()


@pytest.fixture
def pokamind_v1():
    fake = FakePokamind(_golden(), contract=1)
    yield fake
    fake.close()


def hostapp(monkeypatch, robot: Robot, pokamind: FakePokamind, *, token=None):
    """The robot's host app in this process: as it runs before the token is installed
    (``token=None``), or after the restart that puts one in force."""

    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "POKAMIND_BASE", pokamind.base)
    monkeypatch.setattr(server, "DEMO", False)
    load = (robot_token.TokenLoad(token=token, source="file", path=str(robot.token_file))
            if token is not None else robot_token.TokenLoad())
    monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", load)
    monkeypatch.setattr(server, "ROBOT_TOKEN", token)
    app = server._build_app(overrides={
        "robot_client": DryRunRobotClient(), "player": _RecordingPlayer(),
        "fiware_factory": None, "idle": False, "watchdog": False, "gesture": False,
        "mic_profile": _FakeMicProfile(),
        "tts": None, "stt": None, "speak": lambda text, code: None,
        "scope_background": False,
    })
    # emori-admin reaches the host app on loopback, with the admin token.
    return TestClient(app, client=("127.0.0.1", 50000))


def install_token(monkeypatch, robot: Robot, pokamind: FakePokamind, *, via: str = "admin"):
    """Give the robot its token the way an operator does. Returns the HTTP reply or the
    verifier's output lines."""

    if via == "admin":
        client = hostapp(monkeypatch, robot, pokamind)
        reply = client.post("/api/admin/pokamind/token", headers=HEADER,
                            json={"token": GOLDEN_TOKEN})
        assert SEED not in reply.text
        return reply
    # install-token.sh: verify the candidate (and record its scope), then move it in.
    candidate = robot.root / "secrets" / "pokamind_robot_token.new"
    candidate.write_text(GOLDEN_TOKEN + "\n", encoding="ascii")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = token_verify.main(["--token-file", str(candidate), "--base", pokamind.base,
                                  "--env-robot-id", "", "--write-scope",
                                  "--root", str(robot.root)])
    lines = out.getvalue().splitlines()
    assert SEED not in out.getvalue()
    if code == 0:
        os.replace(candidate, robot.token_file)
    return code, lines


# --- token in, robot configured --------------------------------------------------------


@pytest.mark.parametrize("via", ["admin", "install_token"])
def test_a_contract_2_record_configures_a_fresh_robot(monkeypatch, robot, pokamind_v2, via):
    # A fresh robot: nothing robot-specific in .env.
    fresh = robot.next_start()
    assert fresh["scope"] == "no_token"
    assert not any(fresh["env"].get(key) for key in DERIVED)
    assert fresh["boot_lang"] == "en" and fresh["robot_name"] == "EMORI"
    assert fresh["course"] == "emori"
    assert fresh["menus"]["ca"]["source"] == "dev"

    # Token in.
    if via == "admin":
        reply = install_token(monkeypatch, robot, pokamind_v2, via="admin")
        assert reply.status_code == 200, reply.text
        assert reply.json()["saved"] is True and reply.json()["scope_recorded"] is True
    else:
        code, lines = install_token(monkeypatch, robot, pokamind_v2, via="install_token")
        assert code == 0, lines
        assert "verdict=verified" in lines and "scope=written" in lines
    assert robot.token_file.read_text(encoding="ascii").strip() == GOLDEN_TOKEN
    # Every whoami was signed by THIS robot and opted into contract 2.
    assert pokamind_v2.seen and all(
        (path, contract, signed) == ("/api/robot/whoami", "2", True)
        for path, contract, signed in pokamind_v2.seen)

    # The scope snapshot is written, private, and is this robot's.
    snapshot = json.loads(robot.scope_file.read_text(encoding="utf-8"))
    assert (snapshot["robot_id"], snapshot["key_fingerprint"], snapshot["contract"]) == (
        ROBOT_ID, FINGERPRINT, 2)
    assert snapshot["whoami"]["default_language"] == "ca"
    assert snapshot["menu_order"] == {"ca": [LISTENING, TEAMWORK], "en": [FEEDBACK, TIMEKEEPING]}
    if os.name == "posix":
        assert stat.S_IMODE(robot.scope_file.stat().st_mode) == 0o600

    # The menus the record authored, per language, written before any restart.
    ca, en = robot.auto_menu("ca"), robot.auto_menu("en")
    assert (ca["authored"], en["authored"]) == ("pokamind", "pokamind")
    assert ca["options"]["1"] == {"module_id": LISTENING, "name": "Escolta activa",
                                  "keywords": ["escolta", "activa", "escolta activa"]}
    assert ca["options"]["2"]["module_id"] == TEAMWORK
    assert ca["options"]["2"]["name"] == "Treball en equip"
    assert [en["options"][k]["name"] for k in ("1", "2")] == ["Giving feedback",
                                                             "Time management"]

    # The restart: the robot is configured, from Pokamind.
    started = robot.next_start()
    assert started["scope"] == "ok"
    assert started["env"]["EMORI_COURSE_ID"] == "example-course"
    assert started["env"]["EMORI_PATHWAYS"] == "Example pathway,Second pathway"
    assert started["env"]["EMORI_ROBOT_NAME"] == "Reception"
    assert started["env"]["EMORI_LANG"] == "ca"          # default_language beats order
    assert {key: started["sources"].get(key) for key in DERIVED} == {
        key: "pokamind" for key in DERIVED}
    assert started["boot_lang"] == "ca" and started["kiosk_lang"] == "ca"
    assert started["allowed"] == ["en", "ca"]
    assert started["course"] == started["effective_course"] == "example-course"
    assert started["robot_name"] == "Reception"
    assert started["pathways"] == ["Example pathway", "Second pathway"]
    # The hardware profile filled the expression values (an installed robot)...
    assert started["sources"]["EMORI_CUE_Z_MM"] == "hardware"
    # ...and the tuning measured on that robot, through the .env.example lines left empty
    # (a shipped 45 s deadline once ended a cold session the CM4 was still loading).
    assert (started["env"]["EMORI_SESSION_START_S"],
            started["sources"]["EMORI_SESSION_START_S"]) == ("130", "hardware")
    assert (started["env"]["EMORI_SESSION_START_MS"],
            started["sources"]["EMORI_SESSION_START_MS"]) == ("120000", "hardware")
    for lang, ids in (("ca", [LISTENING, TEAMWORK]), ("en", [FEEDBACK, TIMEKEEPING])):
        menu = started["menus"][lang]
        assert menu["source"] == "auto" and menu["authored"] == "pokamind"
        assert Path(menu["path"]).resolve() == (robot.auto_dir / f"voice_topics.{lang}.yaml"
                                                ).resolve()
        assert [menu["options"][key][0] for key in sorted(menu["options"])] == ids
    assert "Escolta activa" in started["menus"]["ca"]["prompt"]

    # readycheck on that robot: site_config PASSes and names where each value came from.
    gate = site_config_gate(robot, "https://api.pokamind.com")
    assert (gate.status, gate.code) == (readycheck.PASS, "site_config_ok"), gate.hint
    for key in DERIVED:
        assert f"{key}=pokamind" in gate.hint
    assert "contract 2" in gate.hint and SEED not in gate.hint

    # A spoken pick reaches the record's modules: by number and by the record's keyword.
    menu = VoiceMenu.from_yaml(robot.auto_dir / "voice_topics.ca.yaml")
    assert menu.options[menu.match("dos")].module_id == TEAMWORK
    assert menu.options[menu.match("activa")].module_id == LISTENING


def test_a_v1_record_configures_the_robot_and_drafts_the_menu_from_titles(
        monkeypatch, robot, pokamind_v1):
    reply = install_token(monkeypatch, robot, pokamind_v1)
    assert reply.status_code == 200 and reply.json()["scope_recorded"] is True
    # The robot asked for contract 2; this server answered with today's body.
    assert {contract for _p, contract, _s in pokamind_v1.seen} == {"2"}

    snapshot = json.loads(robot.scope_file.read_text(encoding="utf-8"))
    assert snapshot["contract"] == 1
    assert snapshot["whoami"]["default_language"] is None

    ca, en = robot.auto_menu("ca"), robot.auto_menu("en")
    assert (ca["authored"], en["authored"]) == ("title_draft", "title_draft")
    assert [ca["options"][k]["module_id"] for k in ("1", "2")] == [LISTENING, TEAMWORK]
    # Names cut from the screen titles (at the first comma, colon or bracket).
    assert [ca["options"][k]["name"] for k in ("1", "2")] == ["Active listening",
                                                             "Teamwork"]
    assert [en["options"][k]["name"] for k in ("1", "2")] == ["Giving feedback",
                                                             "Time management"]
    assert all(option["keywords"] for option in ca["options"].values())

    started = robot.next_start()
    assert started["scope"] == "ok"
    assert {key: started["sources"].get(key) for key in DERIVED} == {
        key: "pokamind" for key in DERIVED}
    assert started["env"]["EMORI_COURSE_ID"] == "example-course"
    assert started["env"]["EMORI_ROBOT_NAME"] == "Reception"
    # No default_language in v1: the record's first supported language.
    assert started["env"]["EMORI_LANG"] == "en" and started["boot_lang"] == "en"
    assert started["menus"]["ca"]["source"] == "auto"
    assert started["menus"]["ca"]["authored"] == "title_draft"
    # Number selection works on a drafted menu.
    menu = VoiceMenu.from_yaml(robot.auto_dir / "voice_topics.ca.yaml")
    assert menu.options[menu.match("u")].module_id == LISTENING


def test_the_menu_numbering_is_stable_across_a_re_sync_and_applies_live(
        monkeypatch, robot, pokamind_v2):
    assert install_token(monkeypatch, robot, pokamind_v2).status_code == 200
    # The restart: the host app now runs with the token.
    client = hostapp(monkeypatch, robot, pokamind_v2, token=_golden())
    served = client.get("/api/admin/menu?lang=ca", headers=HEADER).json()
    assert served["source"] == "auto" and served["authored"] == "pokamind"
    assert [served["menu"]["options"][k]["module_id"] for k in ("1", "2")] == [LISTENING, TEAMWORK]

    # Pokamind reorders the record and adds a module at the front.
    modules = pokamind_v2.record["modules"]
    listening, feedback, teamwork, timekeeping, gone = modules
    pokamind_v2.record["modules"] = [
        _module(NEWCOMER, "Difficult conversations", "ca", menu_name="Converses difícils",
                menu_keywords=["converses"]),
        teamwork, timekeeping, listening, feedback, gone]

    outcome = client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert outcome["ok"] is True and outcome["menus_changed"] is True

    # The numbers already spoken keep their modules; the newcomer comes after them.
    ca = robot.auto_menu("ca")
    assert [ca["options"][k]["module_id"] for k in ("1", "2", "3")] == [LISTENING, TEAMWORK,
                                                                        NEWCOMER]
    en = robot.auto_menu("en")
    assert [en["options"][k]["module_id"] for k in ("1", "2")] == [FEEDBACK, TIMEKEEPING]
    snapshot = json.loads(robot.scope_file.read_text(encoding="utf-8"))
    assert snapshot["menu_order"]["ca"] == [LISTENING, TEAMWORK, NEWCOMER]
    # Live: the running host app serves the rebuilt menu with no restart.
    served = client.get("/api/admin/menu?lang=ca", headers=HEADER).json()
    assert [served["menu"]["options"][k]["module_id"] for k in ("1", "2", "3")] == [
        LISTENING, TEAMWORK, NEWCOMER]
    assert client.get("/api/admin/site", headers=HEADER).json()["restart_pending"] is False

    # A second identical sync changes nothing (asked again at once: the 30 s pace of
    # "Sync now" is not what this test is about).
    from emori_adapter.hostapp import pokamind_scope

    monkeypatch.setattr(pokamind_scope, "ADMIN_MIN_INTERVAL_S", 0.0)
    again = client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert again["ok"] is True and again["menus_changed"] is False
    assert again["changed_keys"] == []


def test_the_admin_page_shows_where_each_value_came_from(monkeypatch, robot, pokamind_v2):
    assert install_token(monkeypatch, robot, pokamind_v2).status_code == 200
    client = hostapp(monkeypatch, robot, pokamind_v2, token=_golden())

    site = client.get("/api/admin/site", headers=HEADER).json()
    rows = {row["key"]: row for row in site["keys"]}
    for key in DERIVED:
        assert rows[key]["source"] == "pokamind", key
    assert rows["EMORI_COURSE_ID"]["value"] == "example-course"
    assert site["scope"]["status"] == "ok" and site["scope"]["contract"] == 2
    assert site["scope"]["derived"]["EMORI_LANG"] == "ca"
    assert SEED not in json.dumps(site)


# --- local values win; the kill switch; refusals change nothing -------------------------


def test_explicit_env_values_win_over_the_record(monkeypatch, robot, pokamind_v2):
    local_menu = robot.root / "configs" / "local_ca.yaml"
    shutil.copy2(robot.root / "configs" / "voice_topics.ca.yaml", local_menu)
    robot.env_set(EMORI_COURSE_ID="site-course", EMORI_LANG="en",
                  EMORI_ROBOT_NAME="Lobby", EMORI_PATHWAYS="Local pathway",
                  EMORI_VOICE_TOPICS_CA="configs/local_ca.yaml",
                  EMORI_CUE_Z_MM="12")

    assert install_token(monkeypatch, robot, pokamind_v2).status_code == 200
    started = robot.next_start()

    assert started["scope"] == "ok"
    assert {key: started["env"][key] for key in DERIVED} == {
        "EMORI_COURSE_ID": "site-course", "EMORI_PATHWAYS": "Local pathway",
        "EMORI_ROBOT_NAME": "Lobby", "EMORI_LANG": "en"}
    assert {key: started["sources"][key] for key in DERIVED} == {key: "env" for key in DERIVED}
    assert started["sources"]["EMORI_CUE_Z_MM"] == "env"
    assert started["env"]["EMORI_CUE_Z_MM"] == "12"
    assert started["boot_lang"] == "en" and started["course"] == "site-course"
    assert started["robot_name"] == "Lobby" 
    assert started["menus"]["ca"]["source"] == "env"
    assert Path(started["menus"]["ca"]["path"]).name == "local_ca.yaml"
    # The language with no override still gets the record's menu.
    assert started["menus"]["en"]["source"] == "auto"

    # A later change in the record never queues a restart for a locally-set key.
    before = json.loads(robot.scope_file.read_text(encoding="utf-8"))
    after = copy.deepcopy(before)
    after["whoami"].update(course_id="other-course", display_name="Other",
                           pathways=["Other"], default_language="en")
    assert site_settings.restart_reasons(before, after, sources=started["sources"],
                                         environ={}) == []
    # ...where a robot that follows its record would restart for it.
    following = {key: "pokamind" for key in DERIVED}
    assert site_settings.restart_reasons(before, after, sources=following,
                                         environ={}) == ["scope_changed"]


def test_site_auto_0_switches_every_derivation_off(monkeypatch, robot, pokamind_v2):
    robot.env_set(EMORI_SITE_AUTO="0")

    assert install_token(monkeypatch, robot, pokamind_v2).status_code == 200
    # The sync itself still ran (the scope is recorded for when it is switched on)...
    assert robot.scope_file.exists()
    started = robot.next_start()

    # ...but nothing is taken from it.
    assert started["scope"] == "auto_off"
    assert "pokamind" not in started["sources"].values()
    assert not any(started["env"].get(key) for key in DERIVED)
    assert started["boot_lang"] == "en" and started["robot_name"] == "EMORI"
    assert started["course"] == "emori" and started["pathways"] == []
    assert started["allowed"] == ["ca", "en"]
    # The built menu is not read: the robot falls back to the shipped menus.
    assert started["menus"]["ca"]["source"] == "dev"
    assert started["menus"]["en"]["source"] == "dev"


_BAD_ANSWERS = {
    "revoked": lambda fake: fake.record.update(status="revoked"),
    "revoked_by_pokamind": lambda fake: setattr(
        fake, "reply", (401, {"error_code": "robot_revoked"})),
    "another_robot": lambda fake: fake.record.update(robot_id="another-robot"),
    "another_key": lambda fake: fake.record["credential"].update(
        key_fingerprint="ZZZZZZZZZZZZZZZZ"),
}
_BAD_CODES = {"revoked": "robot_not_active", "revoked_by_pokamind": "robot_revoked",
              "another_robot": "whoami_robot_id_mismatch",
              "another_key": "whoami_key_mismatch"}


@pytest.mark.parametrize("answer", sorted(_BAD_ANSWERS))
def test_a_refused_token_configures_nothing(monkeypatch, robot, pokamind_v2, answer):
    _BAD_ANSWERS[answer](pokamind_v2)

    reply = install_token(monkeypatch, robot, pokamind_v2)

    assert reply.status_code == 422
    assert reply.json()["code"] == _BAD_CODES[answer]
    assert not robot.token_file.exists()
    assert not robot.scope_file.exists()
    assert list(robot.auto_dir.iterdir()) == []
    started = robot.next_start()
    assert started["scope"] == "no_token"
    assert "pokamind" not in started["sources"].values()
    assert started["menus"]["ca"]["source"] == "dev"


@pytest.mark.parametrize("answer", sorted(_BAD_ANSWERS))
def test_a_refused_re_sync_keeps_the_robot_as_it_was(monkeypatch, robot, pokamind_v2,
                                                     answer):
    assert install_token(monkeypatch, robot, pokamind_v2).status_code == 200
    client = hostapp(monkeypatch, robot, pokamind_v2, token=_golden())
    kept = {path: path.read_bytes() for path in
            (robot.scope_file, *sorted(robot.auto_dir.iterdir()))}
    env_before, sources_before = robot.configured_environment()

    _BAD_ANSWERS[answer](pokamind_v2)
    outcome = client.post("/api/admin/scope/sync", headers=HEADER).json()
    verify = client.post("/api/admin/pokamind/verify", headers=HEADER).json()

    assert outcome["ok"] is False and outcome["code"] in (_BAD_CODES[answer],
                                                          "robot_not_active")
    assert outcome["changed_keys"] == [] and outcome["restart_reasons"] == []
    assert verify["scope_recorded"] is False
    # The last good snapshot and menus stay, byte for byte, and so does what they give.
    assert {path: path.read_bytes() for path in kept} == kept
    assert sorted(robot.auto_dir.iterdir()) == sorted(p for p in kept
                                                      if p.parent == robot.auto_dir)
    env_after, sources_after = robot.configured_environment()
    assert {key: env_after.get(key) for key in DERIVED} == {
        key: env_before.get(key) for key in DERIVED}
    assert sources_after == sources_before
    assert client.get("/api/admin/site", headers=HEADER).json()["restart_pending"] is False
