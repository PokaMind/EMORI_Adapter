"""A robot in service keeps exactly the configuration it has (the compatibility rule).

Every robot in service sets its course, language, name and tuning keys explicitly in
``.env``, runs with a token of its own and a menu of its own, and
talks to a Pokamind that answers whoami with today's v1 body. Deployed onto this
release it must behave exactly as before. Proven here on the real start path (a fresh
process, systemd's environment for the unit, the robot's own tree; see
tests/test_token_provisioning_e2e.py):

* the process configuration -- every ``config.Settings`` field, the environment, the
  host app's module defaults, the learner identity, the kiosk language and the spoken
  menu of each language -- is IDENTICAL before the robot's first Pokamind sync and after
  a sync recorded a v1 record whose course, name and pathways all differ from the
  ``.env``;
* every explicit ``.env`` value flows through unchanged, with ``env`` as its source;
* the hardware-profile line the installer now adds, and ``EMORI_SITE_AUTO=0``, change
  nothing either;
* the recorded scope gives such a robot no reason to restart.

The probe below uses only names this release shares with the code robots run today, so
the same probe can be pointed at a tree of either.
"""

from __future__ import annotations

import json

import yaml

from emori_adapter import robot_token, site_settings
from tests.test_admin_api import HEADER, TOKEN
from tests.test_token_provisioning_e2e import (
    GOLDEN_TOKEN,
    FakePokamind,
    build_robot,
    hostapp,
    record,
)

# A robot in service: everything robot-specific is explicit. (All values are made up.)
IN_SERVICE_ENV = {
    "POKAMIND_BASE": "https://api.pokamind.com",
    "EMORI_PARTNER_NAME": "emori-robot",
    "EMORI_ROBOT_ID": "test-robot-01",
    "EMORI_ROBOT_NAME": "EMORI",
    "EMORI_COURSE_ID": "site-course",
    "EMORI_LANG": "ca",
    "EMORI_PATHWAYS": "Site pathway",
    "EMORI_ROSTER": "configs/roster.yaml",
    "EMORI_FIWARE": "0",
    "EMORI_ADMIN_TOKEN": TOKEN,
    "EMORI_ADMIN_PIN": "135790",
    "EMORI_GESTURE_MODE": "push_turn",
    "EMORI_GESTURE_INVERT": "1",
    # ...and the hardware tuning its .env set long before the Reachy Mini profile carried
    # it (values deliberately not the profile's, so a profile that won would show).
    "EMORI_SESSION_START_MS": "110000",
    "EMORI_SESSION_START_S": "125",
    "EMORI_CUE_BODY_YAW": "0",
    "EMORI_CUE_Z_MM": "16",
    "EMORI_CUE_AMP": "1.4",
    "EMORI_TT_OVERRIDES": '{"ca":{"guard":{"maxFires":20}}}',
}

# The robot's own menus (one-click-deploy adopts a robot's former menus into
# configs/site/ before the prune; configs/site outranks every shipped menu).
SITE_MENUS = {
    "ca": {"prompt": "Hola. Per triar la teva pràctica, digues u per Acollida, o dos per "
                     "Seguretat.",
           "options": {"1": {"module_id": "GGGGGGGGGGGGGGGGGGGG", "name": "Acollida",
                             "keywords": ["acollida"]},
                       "2": {"module_id": "HHHHHHHHHHHHHHHHHHHH", "name": "Seguretat",
                             "keywords": ["seguretat"]}},
           "default": "1"},
    "en": {"prompt": "Welcome. To choose your practice, say one for Welcoming.",
           "options": {"1": {"module_id": "IIIIIIIIIIIIIIIIIIII", "name": "Welcoming",
                             "keywords": ["welcoming"]}},
           "default": "1"},
}

# What the probe reads: only names shared with the code robots in service run today.
_PROBE = r'''
import dataclasses, json, os, sys
import emori_adapter
from emori_adapter.hostapp import identity, server
from emori_adapter.hostapp.lang_state import LangState
from emori_adapter import config

settings = config.load_settings()


def menu(lang):
    loaded = server._load_menu(lang)
    return {"prompt": loaded.prompt, "default": loaded.default,
            "options": {key: [o.module_id, o.name, list(o.keywords)]
                        for key, o in loaded.options.items()}}


print("PROBE=" + json.dumps({
    "package": emori_adapter.__file__,
    "settings": {f.name: getattr(settings, f.name) for f in dataclasses.fields(settings)
                 if f.name != "site_sources"},
    "env": {key: os.environ.get(key) for key in json.loads(sys.argv[1])},
    "server": {name: getattr(server, name, None)
               for name in ("POKAMIND_BASE", "PARTNER", "COURSE_ID", "EMORI_LANG")},
    "robot_name": identity.robot_name(),
    "pathways": list(identity._pathway_allowlist()),
    "kiosk_lang": LangState(default=server.EMORI_LANG).current(),
    "menus": {lang: menu(lang) for lang in ("ca", "en")},
}, sort_keys=True, default=str))
'''

_KEYS = json.dumps(sorted(set(IN_SERVICE_ENV) | set(site_settings.REGISTRY)
                          | {site_settings.PROFILE_ENV, site_settings.AUTO_ENV}))


def _without(report: dict, *keys: str) -> dict:
    """``report`` minus the named variables (the ones a test changed on purpose)."""

    out = json.loads(json.dumps(report))
    for key in keys:
        out["env"].pop(key)
    return out


def _in_service(robot) -> None:
    """Make the fresh tree a robot in service: its .env, its token, its menus."""

    lines = []
    for key, value in IN_SERVICE_ENV.items():
        quoted = f"'{value}'" if value.startswith("{") else value
        lines.append(f"{key}={quoted}")
    (robot.root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    robot_token.save_token(robot_token.parse_token(GOLDEN_TOKEN), robot.token_file)
    for lang, menu in SITE_MENUS.items():
        (robot.root / "configs" / "site" / f"voice_topics.{lang}.yaml").write_text(
            yaml.safe_dump(menu, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_a_robot_in_service_behaves_exactly_as_before(monkeypatch, tmp_path):
    robot = build_robot(tmp_path, monkeypatch)
    _in_service(robot)

    # Deployed onto this release, before its first Pokamind sync.
    before = robot.run_probe(_PROBE, "PROBE", _KEYS)

    # Every explicit value is what the process runs with.
    for key, value in IN_SERVICE_ENV.items():
        assert before["env"][key] == value, key
    assert before["server"] == {"POKAMIND_BASE": "https://api.pokamind.com",
                                "PARTNER": "emori-robot", "COURSE_ID": "site-course",
                                "EMORI_LANG": "ca"}
    assert before["robot_name"] == "EMORI" and before["pathways"] == ["Site pathway"]
    assert before["kiosk_lang"] == "ca"
    for lang, menu in SITE_MENUS.items():
        assert before["menus"][lang]["options"] == {
            key: [o["module_id"], o["name"], o["keywords"]]
            for key, o in menu["options"].items()}

    # Pokamind answers with today's v1 body; its record disagrees with every local value.
    pokamind = FakePokamind(robot_token.parse_token(GOLDEN_TOKEN), contract=1)
    pokamind.record = record(course_id="record-course", display_name="Record Name",
                             pathways=["Record pathway"], languages=["ca", "en"])
    try:
        client = hostapp(monkeypatch, robot, pokamind,
                         token=robot_token.parse_token(GOLDEN_TOKEN))
        outcome = client.post("/api/admin/scope/sync", headers=HEADER).json()
    finally:
        pokamind.close()
    assert outcome["ok"] is True
    snapshot = json.loads(robot.scope_file.read_text(encoding="utf-8"))
    assert snapshot["contract"] == 1

    # After the sync: the very same process configuration, field for field.
    after = robot.run_probe(_PROBE, "PROBE", _KEYS)
    assert after == before

    # The settings layer itself says every robot-specific value is the .env's...
    started = robot.next_start()
    assert started["scope"] == "ok"
    for key in IN_SERVICE_ENV:
        if key in site_settings.REGISTRY and IN_SERVICE_ENV[key]:
            assert started["sources"].get(key) == "env", key
    assert "pokamind" not in started["sources"].values()
    # ...so the recorded scope is no reason to restart this robot.
    assert site_settings.restart_reasons(None, snapshot, sources=started["sources"],
                                         environ={}) == []


def test_the_installer_profile_line_and_the_kill_switch_change_nothing(monkeypatch,
                                                                       tmp_path):
    robot = build_robot(tmp_path, monkeypatch)
    _in_service(robot)
    pokamind = FakePokamind(robot_token.parse_token(GOLDEN_TOKEN), contract=1)
    try:
        client = hostapp(monkeypatch, robot, pokamind,
                         token=robot_token.parse_token(GOLDEN_TOKEN))
        assert client.post("/api/admin/scope/sync", headers=HEADER).json()["ok"] is True
    finally:
        pokamind.close()
    baseline = robot.run_probe(_PROBE, "PROBE", _KEYS)

    # install.sh now adds the hardware profile on every deploy: every key it could
    # supply is explicit on a robot in service.
    robot.env_set(EMORI_HARDWARE_PROFILE="reachy-mini-wireless")
    profiled = robot.run_probe(_PROBE, "PROBE", _KEYS)
    assert profiled["env"][site_settings.PROFILE_ENV] == "reachy-mini-wireless"
    assert _without(profiled, site_settings.PROFILE_ENV) == _without(
        baseline, site_settings.PROFILE_ENV)

    robot.env_set(EMORI_SITE_AUTO="0")
    switched_off = robot.run_probe(_PROBE, "PROBE", _KEYS)
    assert switched_off["env"][site_settings.AUTO_ENV] == "0"
    changed = (site_settings.PROFILE_ENV, site_settings.AUTO_ENV)
    assert _without(switched_off, *changed) == _without(baseline, *changed)
