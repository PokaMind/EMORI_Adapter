"""Menu copy and the robot's name are page DATA, never markup.

The spoken menu's names, keywords and prompt come from the admin page's Menu tab, from
the Pokamind record (``menu_name``/``menu_keywords``) and from module titles; the robot's
name from the Settings tab or the record. The kiosk Chromium auto-grants the camera and
the microphone, so text that could end a ``<script>`` element, or be parsed as HTML,
would be a way in. Pinned here:

* every value substituted into a page is script-safe JSON (``pages.script_json``) and
  substituted in ONE pass (``pages.fill``: a value is never searched for another token);
* the option buttons are built with ``textContent``, never ``innerHTML``;
* a menu that holds markup or a control character is refused where it is written (the
  admin save, the builder), and a title's markup never reaches a drafted name;
* the dashboard's login page (no login needed to see it) escapes the robot's name too.
"""

from __future__ import annotations

import json
import re

import pytest

from emori_adapter.hostapp import dashboard, menu_builder, pages, voice_topics

PAYLOAD = "</script><svg onload=alert(1)>"


# --------------------------------------------------------------------------- helpers
def test_script_json_cannot_end_a_script_element():
    text = pages.script_json({"name": PAYLOAD, "amp": "a & b", "sep": "x" + chr(0x2028)})

    assert "<" not in text and ">" not in text and "&" not in text
    assert chr(0x2028) not in text
    assert json.loads(text) == {"name": PAYLOAD, "amp": "a & b", "sep": "x" + chr(0x2028)}


def test_fill_never_searches_a_value_it_inserted():
    template = 'const A=__A__, B=__B__;'

    out = pages.fill(template, {"__A__": '"__B__"', "__B__": "1"})

    assert out == 'const A="__B__", B=1;'


def test_fill_leaves_unknown_tokens_and_takes_the_longest_key():
    out = pages.fill("__X__ __XY__ __Z__", {"__X__": "1", "__XY__": "2"})

    assert out == "1 2 __Z__"


def test_the_option_buttons_are_text_not_markup():
    voice = pages.VOICE_HTML

    assert "o.name+'</span>'" not in voice
    assert re.search(r"n\.textContent=o\.name", voice)
    assert "innerHTML='<b>'+o.key" not in voice


# --------------------------------------------------------------------------- the kiosk
def _menu_file(tmp_path, *, name=PAYLOAD, prompt="Say one. " + PAYLOAD) -> str:
    path = tmp_path / "menu.yaml"
    path.write_text(json.dumps({
        "prompt": prompt,
        "options": {"1": {"module_id": "AAAAAAAAAAAAAAAAAAAA", "name": name,
                          "keywords": ["one"]}},
        "default": "1"}), encoding="utf-8")
    return str(path)


def test_the_landing_page_escapes_a_hostile_menu(monkeypatch, tmp_path):
    """A menu file is not re-validated when it is served (an override or an old file
    may hold anything): the page must still carry it as data."""

    from tests.test_admin_api_site import build

    menu = _menu_file(tmp_path, name="Intro __OPTIONS__ " + PAYLOAD)
    rig = build(monkeypatch, tmp_path, env={"EMORI_VOICE_TOPICS_EN": menu})

    page = rig.client.get("/").text

    assert PAYLOAD not in page
    assert "<svg" not in page
    # The value arrived intact, as JSON data: nothing was spliced into it.
    match = re.search(r"const PROMPT=(.*?), OPTIONS=(.*?), demo=", page)
    assert match is not None
    assert json.loads(match.group(1)) == "Say one. " + PAYLOAD
    assert json.loads(match.group(2)) == [{"key": "1",
                                           "name": "Intro __OPTIONS__ " + PAYLOAD}]


def test_the_session_page_escapes_the_line_it_announces(monkeypatch, tmp_path):
    from tests.test_admin_api_site import build

    rig = build(monkeypatch, tmp_path)
    monkeypatch.setattr(pages, "start_cover_line", lambda lang, topic: PAYLOAD)

    page = rig.client.get("/session?sid=walkin").text

    assert PAYLOAD not in page
    match = re.search(r"const START_LINE=(.*?);", page)
    assert match is not None and json.loads(match.group(1)) == PAYLOAD


# --------------------------------------------------------------------------- the writers
def _menu(name="Topic", keywords=("topic",), prompt="Say one for Topic.") -> dict:
    return {"prompt": prompt, "default": "1",
            "options": {"1": {"module_id": "AAAA", "name": name,
                              "keywords": list(keywords)}}}


@pytest.mark.parametrize("name", [PAYLOAD, 'A "quoted" one', "back`tick", "a" + chr(0x200e)
                                  + "b"])
def test_a_name_with_markup_or_a_control_is_refused(name):
    assert "name_invalid" in voice_topics.validate_menu_dict(_menu(name=name), "en")


@pytest.mark.parametrize("keyword", ["<b>", "x" + chr(0x2028) + "y", '"q"'])
def test_a_keyword_with_markup_or_a_control_is_refused(keyword):
    assert "keyword_invalid" in voice_topics.validate_menu_dict(
        _menu(keywords=[keyword]), "en")


def test_a_prompt_with_markup_or_too_long_is_refused():
    assert voice_topics.validate_menu_dict(_menu(prompt="Say <b>one</b>."),
                                           "en") == ["prompt_invalid"]
    long = "Say one for Topic. " + "x" * voice_topics.MAX_PROMPT_CHARS
    assert voice_topics.validate_menu_dict(_menu(prompt=long), "en") == ["prompt_invalid"]
    assert voice_topics.validate_menu_dict(_menu(name="Drets i qualitat, l'accés"),
                                           "ca") == ["name_invalid"]  # the comma only
    assert voice_topics.validate_menu_dict(_menu(name="Drets i l'accés"), "ca") == []


def test_the_admin_page_names_every_menu_problem():
    from emori_adapter.admin import page

    for code in voice_topics.MENU_PROBLEMS:
        assert f" {code}:" in page.APP_HTML, code


def test_a_title_with_markup_drafts_a_harmless_name():
    for title in ["<svg onload=alert`1`>", "Intro </script><svg onload=alert`2`>"]:
        name = menu_builder.draft_name(title, lang="en", number=1)
        assert not any(ch in name for ch in '<>"`'), name


def test_a_record_name_or_keyword_with_markup_is_not_used():
    who = {"modules": [{"module_id": "AAAA", "title": "Rights and access",
                        "category": "c", "language": "en", "available": True,
                        "problem": None, "menu_name": "<b>Rights</b>",
                        "menu_keywords": ["<i>x</i>", "rights"]}]}

    menu = menu_builder.build_drafts(who)["en"]["menu"]

    option = menu["options"]["1"]
    assert option["name"] == "Rights and access"   # drafted from the title instead
    assert option["keywords"] == ["rights"]
    assert voice_topics.validate_menu_dict(menu, "en") == []


# --------------------------------------------------------------------------- dashboard
def test_the_dashboard_pages_escape_the_robot_name(monkeypatch):
    monkeypatch.setattr(dashboard, "robot_name", lambda: PAYLOAD + "__ERROR__")

    login = dashboard._login_html("bad")
    board = dashboard._dashboard_html()

    for page in (login, board):
        assert PAYLOAD not in page
        assert "</script><svg onload" not in page
    assert '"bad"' in login   # the error token itself was filled
