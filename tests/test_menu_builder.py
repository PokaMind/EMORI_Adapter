"""The spoken menu the robot builds from its Pokamind record (hostapp/menu_builder.py).

Pins the selection rules of the wire contract (available, no problem, the kiosk
language's base code, record order, at most six, offered ids keep their place), the copy
the robot drafts when the record carries none, and the file it writes: atomic, removed
when a language has nothing to offer, and always a menu the hostapp can load and match.
"""

from __future__ import annotations

import os
import stat

import pytest
import yaml

from emori_adapter import site_settings
from emori_adapter.hostapp import menu_builder as mb
from emori_adapter.hostapp.voice_topics import VoiceMenu, validate_menu_dict


def _module(module_id: str, title: str | None = "A topic", language: str | None = "en", *,
            available: bool = True, problem: str | None = None, **extra) -> dict:
    row = {"module_id": module_id, "title": title, "category": "Example pathway",
           "language": language, "available": available, "problem": problem}
    row.update(extra)
    return row


def _whoami(*modules: dict) -> dict:
    return {"robot_id": "test-robot-01", "display_name": "Reception", "status": "active",
            "languages": ["ca", "en"], "pathways": [], "identity_domains": [],
            "course_id": "", "org": "example-org", "partner": "emori-robot",
            "credential": {"kind": "fleet", "key_fingerprint": None, "key_version": 1},
            "features": {"feature_a": False, "feature_b": False, "feature_c": False},
            "modules": list(modules)}


def _snapshot(whoami: dict, menu_order: dict | None = None) -> dict:
    return {"schema": 1, "robot_id": whoami["robot_id"], "whoami": whoami,
            "menu_order": menu_order or {}}


def _options(draft: dict) -> dict[str, str]:
    return {key: option["module_id"] for key, option in draft["menu"]["options"].items()}


# --------------------------------------------------------------------------- selection
def test_only_available_problem_free_modules_of_the_language_are_offered():
    whoami = _whoami(
        _module("EN_OK", "Listening"),
        _module("EN_OFF", "Switched off", available=False),
        _module("EN_HIDDEN", "Hidden", problem="not_visible_to_robot"),
        _module("GONE", None, None, available=False, problem="not_found"),
        _module("NO_LANG", "Somewhere", None),
        _module("CA_OK", "Escolta", "ca"),
        _module("CA_REGION", "Equip", "ca-ES"),
        _module("ES_OK", "Escucha", "es"),
        _module("EN_OK", "Listening again"),  # the same id twice
        _module("bad id!", "Broken"),
    )
    drafts = mb.build_drafts(whoami, previous_order={})

    assert drafts["en"]["order"] == ["EN_OK"]
    assert drafts["ca"]["order"] == ["CA_OK", "CA_REGION"]  # base code of ca-ES is ca
    assert {s["module_id"]: s["reason"] for s in drafts["en"]["skipped"]} == {
        "EN_OFF": "unavailable",
        "EN_HIDDEN": "not_visible_to_robot",
        "GONE": "not_found",
        "NO_LANG": "no_language",
        "EN_OK": "duplicate",
        "bad id!": "module_id_invalid",
    }
    # a module with no language can be on no menu: every language says why
    assert {"module_id": "NO_LANG", "reason": "no_language"} in drafts["ca"]["skipped"]
    # another language's module is not a row of this one at all
    assert all(s["module_id"] not in ("ES_OK", "CA_OK") for s in drafts["en"]["skipped"])


def test_the_record_order_is_the_menu_order():
    whoami = _whoami(*(_module(f"M{n}", f"Topic {chr(64 + n)}") for n in (3, 1, 2)))
    draft = mb.build_drafts(whoami)["en"]
    assert _options(draft) == {"1": "M3", "2": "M1", "3": "M2"}
    assert draft["order"] == ["M3", "M1", "M2"]


def test_an_offered_module_keeps_its_place_across_a_scope_change():
    before = _whoami(*(_module(i, f"Topic {i}") for i in ("A", "B", "C")))
    first = mb.build_drafts(before)["en"]["order"]
    assert first == ["A", "B", "C"]

    # The record now lists a new module first, reorders the rest and drops B.
    after = _whoami(_module("N", "Topic N"), _module("C", "Topic C"), _module("A", "Topic A"))
    draft = mb.build_drafts(after, previous_order={"en": first})["en"]
    # A and C keep their relative order ahead of the newcomer; B's slot closes up.
    assert draft["order"] == ["A", "C", "N"]
    assert _options(draft) == {"1": "A", "2": "C", "3": "N"}


def test_when_a_module_leaves_the_scope_every_later_option_moves_up_a_number():
    """The decided numbering rule: the menu is spoken in full each time, so a module that
    leaves the scope is not kept as a gap -- the options after it move up one."""
    before = _whoami(*(_module(i, f"Topic {i}") for i in ("A", "B", "C", "D")))
    first = mb.build_drafts(before)["en"]
    assert _options(first) == {"1": "A", "2": "B", "3": "C", "4": "D"}

    after = _whoami(*(_module(i, f"Topic {i}") for i in ("A", "C", "D")))
    draft = mb.build_drafts(after, previous_order={"en": first["order"]})["en"]

    assert _options(draft) == {"1": "A", "2": "C", "3": "D"}
    # ...and the prompt the robot speaks says the new numbers.
    prompt = draft["menu"]["prompt"]
    assert prompt.index("one for") < prompt.index("two for") < prompt.index("three for")
    assert "four" not in prompt


def test_the_menu_stops_at_six_and_says_which_were_left_out():
    whoami = _whoami(*(_module(f"M{n}", f"Topic {n}") for n in range(1, 9)))
    draft = mb.build_drafts(whoami)["en"]
    assert draft["order"] == [f"M{n}" for n in range(1, 7)]
    assert list(draft["menu"]["options"]) == ["1", "2", "3", "4", "5", "6"]
    assert [s for s in draft["skipped"] if s["reason"] == "over_menu_limit"] == [
        {"module_id": "M7", "reason": "over_menu_limit"},
        {"module_id": "M8", "reason": "over_menu_limit"}]


def test_a_newcomer_waits_behind_six_offered_modules():
    whoami = _whoami(*(_module(f"M{n}", f"Topic {n}") for n in range(7, 0, -1)))
    previous = {"en": [f"M{n}" for n in range(1, 7)]}
    draft = mb.build_drafts(whoami, previous_order=previous)["en"]
    assert draft["order"] == previous["en"]
    assert {"module_id": "M7", "reason": "over_menu_limit"} in draft["skipped"]


def test_a_whoami_object_is_accepted_as_well_as_its_dict():
    class _Whoami:
        def to_dict(self):
            return _whoami(_module("A", "Topic A"))

    assert mb.build_drafts(_Whoami())["en"]["order"] == ["A"]


def test_a_language_without_a_template_is_not_built_and_junk_is_harmless():
    assert set(mb.build_drafts(_whoami(), languages=("de", "ca", "CA-es"))) == {"ca"}
    assert mb.build_drafts({"modules": "nope"})["en"]["menu"] is None
    assert mb.build_drafts(None)["ca"] == {"menu": None, "order": [], "skipped": []}


# --------------------------------------------------------------------------- the copy
@pytest.mark.parametrize("title, name", [
    ("Listening, empathy and trust", "Listening"),
    ("Communication: the basics", "Communication"),
    ("Budgeting; planning", "Budgeting"),
    ("Talking to customers (advanced)", "Talking to customers"),
    ("Team meetings - part two", "Team meetings"),
    ("3- Gestió del temps", "Gestió del temps"),
    ("12 - Escolta activa", "Escolta activa"),
    ("  Active   listening.  ", "Active listening"),
    ("Planning the week ahead with a small team of volunteers",
     "Planning the week ahead with a small"),
    ("Habits and (routines)", "Habits"),
    # one word longer than the limit is cut inside it
    ("Supercalifragilisticexpialidocious-and-then-some-more-words-here",
     "Supercalifragilisticexpialidocious-and-t"),
])
def test_a_draft_name_is_the_title_cut_short(title, name):
    drafted = mb.draft_name(title, lang="en", number=1)
    assert drafted == name
    assert len(drafted) <= mb.MAX_DRAFT_NAME_CHARS and "," not in drafted


def test_a_title_that_leaves_nothing_is_named_by_its_number():
    assert mb.draft_name(None, lang="en", number=4) == "Option 4"
    assert mb.draft_name("(draft)", lang="ca", number=2) == "Opció 2"
    assert mb.draft_name("3- ", lang="en", number=3) == "Option 3"


def test_a_catalan_cut_never_ends_on_a_connecting_word():
    assert mb.draft_name("Hàbits i (rutines)", lang="ca", number=1) == "Hàbits"
    assert mb.draft_name("Gestió del temps: eines", lang="ca", number=1) == "Gestió del temps"


def test_the_record_menu_copy_is_used_verbatim():
    whoami = _whoami(
        _module("A", "Listening, empathy and trust", "ca", menu_name="Escolta i empatia",
                menu_keywords=["Escolta", "EMPATIA", "escolta i empatia"]),
        _module("B", "Meetings", "ca", menu_name="Reunions d'equip",
                menu_keywords=[]),
    )
    menu = mb.build_drafts(whoami)["ca"]["menu"]
    assert menu["options"]["1"] == {"module_id": "A", "name": "Escolta i empatia",
                                    "keywords": ["escolta", "empatia", "escolta i empatia"]}
    # an explicit empty list is the record saying "no keywords", not "draft some"
    assert menu["options"]["2"]["keywords"] == []
    assert menu["authored"] == "pokamind"


def test_record_keywords_of_which_none_is_usable_count_as_absent():
    whoami = _whoami(_module("A", "x", "ca", menu_name="Escolta activa",
                             menu_keywords=["a,b", 7]))
    keywords = mb.build_drafts(whoami)["ca"]["menu"]["options"]["1"]["keywords"]
    assert keywords == mb.draft_keywords("Escolta activa", lang="ca")


def test_an_unusable_menu_name_falls_back_to_the_title():
    whoami = _whoami(_module("A", "Escolta activa: nivell 1", "ca", menu_name="Escolta, activa"),
                     _module("B", "Treball en equip", "ca", menu_name="   "),
                     _module("C", "Gestió del temps", "ca", menu_name="Gestió\ndel temps"),
                     _module("D", "Parlar en públic", "ca", menu_name="P" * 61))
    menu = mb.build_drafts(whoami)["ca"]["menu"]
    assert [o["name"] for o in menu["options"].values()] == [
        "Escolta activa", "Treball en equip", "Gestió del temps", "Parlar en públic"]
    assert menu["authored"] == "title_draft"


def test_authored_says_whose_copy_the_menu_speaks():
    whoami = _whoami(_module("A", "Topic A", menu_name="Alpha"), _module("B", "Beta topics"))
    assert mb.build_drafts(whoami)["en"]["menu"]["authored"] == "mixed"
    assert mb.build_drafts(_whoami(_module("B", "Beta")))["en"]["menu"]["authored"] == \
        "title_draft"


def test_drafted_keywords_are_the_name_and_its_longer_words():
    assert mb.draft_keywords("Active Listening", lang="en") == [
        "active listening", "active", "listening"]
    # filler, menu words and short words never become a keyword
    assert mb.draft_keywords("Practice with your team", lang="en") == [
        "practice with your team", "team"]


def test_drafted_keywords_carry_accent_free_twins():
    assert mb.draft_keywords("Gestió del temps", lang="ca") == [
        "gestió del temps", "gestio del temps", "gestió", "gestio", "temps"]
    assert "comunicacio" in mb.draft_keywords("Comunicació", lang="ca")


def test_drafted_keywords_never_say_a_number_or_a_carrier():
    # match() takes any number word it hears ("to" is two, "una" is one), so a keyword
    # holding one would select that number, not its own option.
    keywords = mb.draft_keywords("Talking to customers", lang="en")
    assert keywords == ["talking", "customers"]
    assert "una conversa difícil" not in mb.draft_keywords("Una conversa difícil", lang="ca")
    assert "first" not in mb.draft_keywords("First contact", lang="en")
    assert "again" not in mb.draft_keywords("Again and again", lang="en")


def test_a_keyword_two_options_share_is_dropped_from_both():
    whoami = _whoami(
        _module("A", "Difficult conversations"),
        _module("B", "Difficult feedback"),
        _module("C", "Feedback", menu_name="Feedback desk",
                menu_keywords=["feedback desk", "desk"]),
    )
    options = mb.build_drafts(whoami)["en"]["menu"]["options"]
    assert options["1"]["keywords"] == ["difficult conversations", "conversations"]
    # "difficult" is shared with option 1; "feedback" sits inside option 3's
    # "feedback desk"
    assert options["2"]["keywords"] == ["difficult feedback"]
    assert options["3"]["keywords"] == ["feedback desk", "desk"]
    menu = mb.build_drafts(whoami)["en"]["menu"]
    assert validate_menu_dict(menu, "en") == []


def test_a_record_keyword_inside_another_options_keyword_costs_only_that_keyword():
    whoami = _whoami(
        _module("A", "x", "ca", menu_name="Escolta", menu_keywords=["escolta", "empatia"]),
        _module("B", "y", "ca", menu_name="Escolta activa",
                menu_keywords=["escolta activa", "activa", "a,b", "two\nlines", 7]),
    )
    options = mb.build_drafts(whoami)["ca"]["menu"]["options"]
    assert options["1"]["keywords"] == ["empatia"]
    assert options["2"]["keywords"] == ["escolta activa", "activa"]
    assert set(options) == {"1", "2"}  # never the option


@pytest.mark.parametrize("lang, names, prompt", [
    ("en", ["Active Listening", "Giving Feedback", "Time Management"],
     "Welcome. To choose your practice, say one for Active Listening, two for Giving "
     "Feedback, or three for Time Management."),
    ("en", ["Active Listening"], "Welcome. To choose your practice, say one for Active "
     "Listening."),
    ("ca", ["Converses difícils", "Escolta activa"],
     "Hola. Per triar la teva pràctica, digues u per Converses difícils, o dos per "
     "Escolta activa."),
    ("ca", [f"Tema {n}" for n in range(1, 7)],
     "Hola. Per triar la teva pràctica, digues u per Tema 1, dos per Tema 2, tres per "
     "Tema 3, quatre per Tema 4, cinc per Tema 5, o sis per Tema 6."),
])
def test_the_prompt_names_every_option_with_its_number_word(lang, names, prompt):
    assert mb.build_prompt(names, lang) == prompt


def test_a_built_menu_carries_its_provenance_and_default():
    menu = mb.build_drafts(_whoami(_module("A", "Topic A")))["en"]["menu"]
    assert (menu["default"], menu["generated_from"]) == ("1", "pokamind_whoami")


def test_module_rows_show_the_admin_page_every_module_of_the_language():
    whoami = _whoami(_module("A", "Topic A"), _module("B", "Off", available=False),
                     _module("C", "Escolta", "ca"))
    rows = mb.module_rows(whoami, "en")
    assert [(r["module_id"], r["eligible"], r["number"], r["reason"]) for r in rows] == [
        ("A", True, "1", None), ("B", False, None, "unavailable")]
    assert rows[0]["title"] == "Topic A" and rows[1]["available"] is False


# --------------------------------------------------------------------------- the files
def _sample_whoami() -> dict:
    return _whoami(
        _module("CA_1", "Converses difícils", "ca"),
        _module("CA_2", "Escolta activa", "ca", menu_name="Escolta activa",
                menu_keywords=["escolta", "escolta activa"]),
        _module("CA_3", "3- Treball en equip: casos", "ca"),
        _module("EN_1", "Active listening"),
        _module("EN_2", "Giving feedback, the basics"),
    )


def test_rebuild_writes_one_loadable_menu_per_language(tmp_path):
    order = mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
    assert order == {"ca": ["CA_1", "CA_2", "CA_3"], "en": ["EN_1", "EN_2"]}

    ca = VoiceMenu.from_yaml(tmp_path / "voice_topics.ca.yaml")
    assert ca.match("dos") == "2" and ca.match("tres") == "3"
    assert ca.resolve("u")[1].module_id == "CA_1"
    assert ca.match("treball en equip") == "3"
    assert ca.match("converses dificils") == "1"  # the accent-free twin
    assert ca.spoken_numbers("ca") == ["u", "dos", "tres"]
    assert ca.options["1"].name in ca.prompt

    en = VoiceMenu.from_yaml(tmp_path / "voice_topics.en.yaml")
    assert en.match("two") == "2" and en.resolve("number one")[1].module_id == "EN_1"
    assert en.options["2"].name == "Giving feedback"

    raw = yaml.safe_load((tmp_path / "voice_topics.ca.yaml").read_text(encoding="utf-8"))
    assert (raw["generated_from"], raw["authored"]) == ("pokamind_whoami", "mixed")
    assert validate_menu_dict(raw, "ca") == []


def test_a_language_with_nothing_to_offer_has_its_file_removed(tmp_path):
    mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
    assert (tmp_path / "voice_topics.en.yaml").exists()

    only_ca = _whoami(_module("CA_1", "Converses difícils", "ca"),
                      _module("EN_1", "Active listening", available=False))
    order = mb.rebuild_auto(_snapshot(only_ca, {"en": ["EN_1", "EN_2"]}), tmp_path)

    assert order == {"ca": ["CA_1"], "en": []}
    assert not (tmp_path / "voice_topics.en.yaml").exists()
    assert (tmp_path / "voice_topics.ca.yaml").exists()


def test_the_write_is_atomic_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
    before = (tmp_path / "voice_topics.en.yaml").read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["voice_topics.ca.yaml",
                                                          "voice_topics.en.yaml"]

    def _crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mb.os, "replace", _crash)
    changed = _whoami(_module("EN_9", "Time management"), _module("CA_1", "Converses", "ca"))
    order = mb.rebuild_auto(_snapshot(changed, {"en": ["EN_1", "EN_2"], "ca": ["CA_1"]}),
                            tmp_path)

    # the old file is whole, no temp file is left, and the order still describes it
    assert (tmp_path / "voice_topics.en.yaml").read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["voice_topics.ca.yaml",
                                                          "voice_topics.en.yaml"]
    assert order["en"] == ["EN_1", "EN_2"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_menu_file_is_private_to_the_robot_user(tmp_path):
    mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
    mode = stat.S_IMODE((tmp_path / "voice_topics.ca.yaml").stat().st_mode)
    assert mode == 0o600


def test_an_unchanged_menu_is_not_rewritten(tmp_path, monkeypatch):
    mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
    writes: list[str] = []
    real = mb.atomic_write_text
    monkeypatch.setattr(mb, "atomic_write_text",
                        lambda target, text: (writes.append(target.name), real(target, text)))
    mb.rebuild_auto(_snapshot(_sample_whoami(), {"ca": ["CA_1", "CA_2", "CA_3"]}), tmp_path)
    assert writes == []


def test_the_order_round_trips_through_the_snapshot(tmp_path):
    """The sync loop: merge -> rebuild -> menu_order stored -> the next merge carries it."""

    first = site_settings.merge_scope_snapshot(
        None, _sample_whoami(), contract=1, key_fingerprint=None, key_version=1, now=1.0)
    first["menu_order"] = mb.rebuild_auto(first, tmp_path)

    reordered = _sample_whoami()
    reordered["modules"] = [_module("EN_3", "Time management")] + list(
        reversed(reordered["modules"]))
    second = site_settings.merge_scope_snapshot(
        first, reordered, contract=1, key_fingerprint=None, key_version=1, now=2.0)
    second["menu_order"] = mb.rebuild_auto(second, tmp_path)

    assert second["menu_order"]["en"] == ["EN_1", "EN_2", "EN_3"]
    assert second["menu_order"]["ca"] == ["CA_1", "CA_2", "CA_3"]
    en = VoiceMenu.from_yaml(tmp_path / "voice_topics.en.yaml")
    assert en.resolve("three")[1].module_id == "EN_3"


def test_a_relative_directory_is_resolved_against_the_install_root(tmp_path, monkeypatch):
    monkeypatch.setattr(site_settings, "base_dir", lambda root=None: tmp_path)
    mb.rebuild_auto(_snapshot(_sample_whoami()), site_settings.AUTO_MENU_DIR)
    assert (tmp_path / "configs/site/auto/voice_topics.ca.yaml").exists()
    assert mb.auto_menu_path("ca-ES") == tmp_path / "configs/site/auto/voice_topics.ca.yaml"
    mb.rebuild_auto(_snapshot(_sample_whoami()))  # the default directory is the same one
    assert sorted(p.name for p in (tmp_path / "configs/site/auto").iterdir()) == [
        "voice_topics.ca.yaml", "voice_topics.en.yaml"]


@pytest.mark.parametrize("snapshot", [None, {}, {"whoami": None}, {"whoami": "x"}, []])
def test_rebuild_never_raises_and_touches_nothing_on_an_unusable_snapshot(tmp_path, snapshot):
    assert mb.rebuild_auto(snapshot, tmp_path) == {}
    assert list(tmp_path.iterdir()) == []


def test_rebuild_never_raises_on_an_unexpected_failure(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mb, "build_drafts", _boom)
    assert mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path) == {}


def test_a_menu_that_still_fails_validation_is_not_written(tmp_path, monkeypatch, caplog):
    real = mb.build_drafts

    def _broken(*args, **kwargs):
        drafts = real(*args, **kwargs)
        drafts["en"]["menu"]["options"]["1"]["name"] = "Broken, name"
        return drafts

    monkeypatch.setattr(mb, "build_drafts", _broken)
    with caplog.at_level("WARNING", logger="emori.hostapp.menu_builder"):
        order = mb.rebuild_auto(_snapshot(_sample_whoami(), {"en": ["OLD"]}), tmp_path)
    assert not (tmp_path / "voice_topics.en.yaml").exists()
    assert order["en"] == ["OLD"] and order["ca"] == ["CA_1", "CA_2", "CA_3"]
    assert "name_invalid" in caplog.text


def test_nothing_logged_names_a_module(tmp_path, caplog):
    with caplog.at_level("DEBUG", logger="emori.hostapp.menu_builder"):
        mb.rebuild_auto(_snapshot(_sample_whoami()), tmp_path)
        mb.rebuild_auto(_snapshot(_whoami()), tmp_path)
    assert caplog.records  # it does say what it did
    for secret in ("CA_1", "EN_1", "Converses", "listening"):
        assert secret not in caplog.text
