"""The hostapp half of the phone's Menu tab: ``/api/admin/menu*``.

Pinned:

* ``GET`` says where each language's menu comes from (``site``, ``auto``, ...), what it
  is, how it was authored, and which of the record's modules it offers and why not;
* a saved menu is validated, every module in it must be one this robot's Pokamind record
  can start, it is written atomically to ``configs/site/voice_topics.<lang>.yaml`` and
  it is live at once (the menu cache is dropped -- no restart);
* reset removes the saved menu and the built one is back, live;
* the preview speaks the prompt out of the robot's own speaker, only while idle.
"""

from __future__ import annotations

import threading

import pytest
import yaml

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import server
from emori_adapter.hostapp.voice_topics import VoiceMenu
from tests.test_admin_api import HEADER, _IdleRuntime
from tests.test_admin_api_site import build
from tests.test_pokamind_whoami import GOLDEN_TOKEN

MODULES = [
    {"module_id": "AAAAAAAAAAAAAAAAAAAA", "title": "Customer greetings",
     "category": "Example pathway", "language": "en", "available": True, "problem": None},
    {"module_id": "BBBBBBBBBBBBBBBBBBBB", "title": "Handling complaints: the basics",
     "category": "Example pathway", "language": "en", "available": True, "problem": None},
    {"module_id": "CCCCCCCCCCCCCCCCCCCC", "title": "Not offered", "category": None,
     "language": "en", "available": False, "problem": "not_visible_to_robot"},
    {"module_id": "DDDDDDDDDDDDDDDDDDDD", "title": "Escolta activa",
     "category": "Example pathway", "language": "ca", "available": True, "problem": None},
]


def _saved_menu(*module_ids: str) -> dict:
    names = ["Greetings", "Complaints", "Third topic"]
    return {
        "prompt": "Welcome. Say one for greetings or two for complaints.",
        "options": {str(n): {"module_id": module_id, "name": names[n - 1],
                             "keywords": [names[n - 1].lower()]}
                    for n, module_id in enumerate(module_ids, 1)},
        "default": "1",
    }


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    spoken: list = []
    token = robot_token.parse_token(GOLDEN_TOKEN)
    rig = build(monkeypatch, tmp_path, token=token,
                speak=lambda text, lang: spoken.append((text, lang)))
    rig.whoami.overrides = {"modules": MODULES, "languages": ["en", "ca"]}
    rig.spoken = spoken
    synced = rig.client.post("/api/admin/scope/sync", headers=HEADER).json()
    assert synced["ok"] is True
    return rig


def _join_preview(timeout_s: float = 2.0) -> None:
    for thread in threading.enumerate():
        if thread.name == "menu-preview-speak":
            thread.join(timeout=timeout_s)


# --- GET ----------------------------------------------------------------------------


def test_the_menu_built_from_the_record_is_reported_with_its_modules(rig):
    reply = rig.client.get("/api/admin/menu?lang=en", headers=HEADER)

    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    body = reply.json()
    assert body["lang"] == "en" and body["source"] == "auto"
    assert body["authored"] == "title_draft"  # no menu_name in the record: drafted
    assert body["errors"] == [] and body["saved"] is False and body["auto_available"]
    options = body["menu"]["options"]
    assert [options[k]["module_id"] for k in sorted(options)] == [
        "AAAAAAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBBBBBB"]
    assert set(body["menu"]) == {"prompt", "options", "default"}
    rows = {row["module_id"]: row for row in body["modules"]}
    assert rows["AAAAAAAAAAAAAAAAAAAA"]["number"] == "1"
    assert rows["CCCCCCCCCCCCCCCCCCCC"]["eligible"] is False
    assert rows["CCCCCCCCCCCCCCCCCCCC"]["reason"] == "not_visible_to_robot"
    # Another language's module is not a row of this one.
    assert "DDDDDDDDDDDDDDDDDDDD" not in rows


def test_without_a_language_the_kiosk_language_is_reported(rig):
    body = rig.client.get("/api/admin/menu", headers=HEADER).json()

    assert body["lang"] == rig.app.state.lang_state.current()


@pytest.mark.parametrize("lang", ["fr", "xx-YY", "../en"])
def test_a_language_this_robot_does_not_ship_is_a_400(rig, lang):
    reply = rig.client.get(f"/api/admin/menu?lang={lang}", headers=HEADER)

    assert (reply.status_code, reply.json()) == (400, {"error": "unsupported_lang"})


def test_a_robot_with_no_scope_still_reports_its_menu(monkeypatch, tmp_path):
    rig = build(monkeypatch, tmp_path, token=None)

    body = rig.client.get("/api/admin/menu?lang=en", headers=HEADER).json()

    assert body["source"] in ("dev", "builtin") and body["modules"] == []
    assert body["scope_status"] == "no_token"


# --- save ---------------------------------------------------------------------------


def test_a_saved_menu_is_validated_scoped_written_and_live_at_once(rig):
    reasons = rig.client.get("/api/admin/site", headers=HEADER).json()["restart_reasons"]
    before = rig.client.get("/api/voice-menu").json()  # cached by the kiosk
    assert [o["name"] for o in before["options"]] != ["Greetings", "Complaints"]

    reply = rig.client.post("/api/admin/menu/save", headers=HEADER, json={
        "lang": "en", "menu": {**_saved_menu("BBBBBBBBBBBBBBBBBBBB", "AAAAAAAAAAAAAAAAAAAA"),
                               "generated_from": "pokamind_whoami",
                               "authored": "title_draft"}})

    assert reply.status_code == 200
    assert reply.json() == {"saved": True, "lang": "en", "source": "site"}
    path = rig.root / "configs/site/voice_topics.en.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# This robot's spoken menu, saved on the phone admin page.")
    data = yaml.safe_load(text)
    # Only what the menu is: the built menu's provenance keys are not copied over.
    assert set(data) == {"prompt", "options", "default"}
    assert VoiceMenu.from_yaml(path).options["1"].module_id == "BBBBBBBBBBBBBBBBBBBB"
    after = rig.client.get("/api/voice-menu").json()
    assert [o["name"] for o in after["options"]] == ["Greetings", "Complaints"]
    status = rig.client.get("/api/admin/menu?lang=en", headers=HEADER).json()
    assert status["source"] == "site" and status["saved"] is True
    assert status["authored"] is None
    # A menu is live: it never adds a reason to restart.
    assert rig.client.get("/api/admin/site", headers=HEADER).json()[
        "restart_reasons"] == reasons


@pytest.mark.parametrize("menu, error", [
    ({"prompt": "", "options": {}}, ["prompt_missing", "options_empty"]),
    ("not a menu", ["menu_not_mapping"]),
    ({"prompt": "Say one", "options": {"1": {"module_id": "AAAAAAAAAAAAAAAAAAAA",
                                             "name": "A, B"}}}, ["name_invalid"]),
    ({"prompt": "Say one", "options": {"9": {"module_id": "AAAAAAAAAAAAAAAAAAAA",
                                             "name": "A"}}}, ["option_key_not_number"]),
])
def test_an_invalid_menu_is_refused_with_its_slugs_and_nothing_is_written(rig, menu,
                                                                        error):
    reply = rig.client.post("/api/admin/menu/save", headers=HEADER,
                            json={"lang": "en", "menu": menu})

    assert reply.status_code == 400
    assert reply.json()["error"] == "menu_invalid" and reply.json()["errors"] == error
    assert not (rig.root / "configs/site/voice_topics.en.yaml").exists()


@pytest.mark.parametrize("module_id", ["CCCCCCCCCCCCCCCCCCCC", "ZZZZZZZZZZZZZZZZZZZZ"])
def test_a_module_this_robot_cannot_start_is_refused(rig, module_id):
    reply = rig.client.post("/api/admin/menu/save", headers=HEADER, json={
        "lang": "en", "menu": _saved_menu("AAAAAAAAAAAAAAAAAAAA", module_id)})

    assert reply.status_code == 400
    assert reply.json() == {"error": "module_not_in_scope", "options": ["2"]}
    assert not (rig.root / "configs/site/voice_topics.en.yaml").exists()


def test_without_this_robots_scope_no_menu_can_be_saved(monkeypatch, tmp_path):
    rig = build(monkeypatch, tmp_path, token=robot_token.parse_token(GOLDEN_TOKEN))

    reply = rig.client.post("/api/admin/menu/save", headers=HEADER, json={
        "lang": "en", "menu": _saved_menu("AAAAAAAAAAAAAAAAAAAA")})

    assert (reply.status_code, reply.json()) == (
        409, {"error": "scope_unavailable", "code": "scope_missing"})


@pytest.mark.parametrize("payload", [{"lang": "fr", "menu": {}}, {"menu": {}}, None])
def test_a_save_for_no_shipped_language_is_a_400(rig, payload):
    reply = rig.client.post("/api/admin/menu/save", headers=HEADER, json=payload)

    assert (reply.status_code, reply.json()) == (400, {"error": "unsupported_lang"})


# --- reset --------------------------------------------------------------------------


def test_reset_goes_back_to_the_menu_built_from_the_record(rig):
    rig.client.post("/api/admin/menu/save", headers=HEADER, json={
        "lang": "en", "menu": _saved_menu("AAAAAAAAAAAAAAAAAAAA")})
    assert [o["name"] for o in rig.client.get("/api/voice-menu").json()["options"]] == [
        "Greetings"]

    reply = rig.client.post("/api/admin/menu/reset", headers=HEADER, json={"lang": "en"})

    assert reply.json() == {"reset": True, "lang": "en", "source": "auto"}
    assert not (rig.root / "configs/site/voice_topics.en.yaml").exists()
    names = [o["name"] for o in rig.client.get("/api/voice-menu").json()["options"]]
    assert "Greetings" not in names and len(names) == 2
    again = rig.client.post("/api/admin/menu/reset", headers=HEADER, json={"lang": "en"})
    assert again.json()["reset"] is False


def test_reset_is_per_language(rig):
    for lang in ("en", "ca"):
        site_settings.site_path(f"configs/site/voice_topics.{lang}.yaml").write_text(
            yaml.safe_dump(_saved_menu("AAAAAAAAAAAAAAAAAAAA")), encoding="utf-8")

    rig.client.post("/api/admin/menu/reset", headers=HEADER, json={"lang": "ca"})

    assert server.menu_source("en") == "site" and server.menu_source("ca") == "auto"


# --- preview ------------------------------------------------------------------------


def test_the_preview_speaks_the_served_prompt_in_its_language(rig):
    reply = rig.client.post("/api/admin/menu/preview", headers=HEADER, json={"lang": "ca"})

    assert reply.json() == {"spoken": True, "lang": "ca"}
    _join_preview()
    (text, lang), = rig.spoken
    assert lang == "ca" and text.startswith("Hola.")


def test_the_preview_can_speak_an_unsaved_draft(rig):
    draft = _saved_menu("AAAAAAAAAAAAAAAAAAAA")

    reply = rig.client.post("/api/admin/menu/preview", headers=HEADER,
                            json={"lang": "en", "menu": draft})
    bad = rig.client.post("/api/admin/menu/preview", headers=HEADER,
                          json={"lang": "en", "menu": {"prompt": ""}})

    assert reply.status_code == 200
    _join_preview()
    assert rig.spoken == [(draft["prompt"], "en")]
    assert bad.status_code == 400 and bad.json()["error"] == "menu_invalid"


@pytest.mark.parametrize("runtime", [_IdleRuntime(mode="mode_a"),
                                     _IdleRuntime(reserved=True)])
def test_the_preview_never_speaks_over_a_visitor(monkeypatch, tmp_path, runtime):
    spoken: list = []
    rig = build(monkeypatch, tmp_path, runtime=runtime,
                speak=lambda text, lang: spoken.append(text))

    reply = rig.client.post("/api/admin/menu/preview", headers=HEADER, json={"lang": "en"})

    assert (reply.status_code, reply.json()) == (409, {"error": "busy"})
    _join_preview()
    assert spoken == []


def test_the_preview_is_refused_during_a_training_session(rig):
    from emori_adapter.hostapp.driver import SessionInfo

    rig.app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1",
                                                   lang="en"))

    reply = rig.client.post("/api/admin/menu/preview", headers=HEADER, json={"lang": "en"})

    assert reply.status_code == 409
    _join_preview()
    assert rig.spoken == []
