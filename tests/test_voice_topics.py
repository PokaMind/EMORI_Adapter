"""Verbal topic selection: spoken utterance -> Pokamind module key (screenless robot)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from emori_adapter.hostapp.voice_topics import (
    MENU_PROBLEMS,
    VoiceMenu,
    menu_problems,
    spoken_numbers,
    validate_menu_dict,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def menu() -> VoiceMenu:
    return VoiceMenu.default_menu()


@pytest.fixture()
def menu6() -> VoiceMenu:
    """A SIX-option menu, like the shipped Catalan menu.

    A course can have six roleplay modules, so the menu the robot speaks -- and every
    number word it must understand -- goes to six, not three.
    """
    return VoiceMenu(
        prompt="Digues un número.",
        options={str(n): {"module_id": f"M{n}", "name": f"Tema {n}"} for n in range(1, 7)},
        default=None,
    )


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("one", "1"),
        ("1", "1"),
        ("number one please", "1"),
        ("two", "2"),
        ("to", "2"),          # common STT homophone
        ("too", "2"),
        ("option 2", "2"),
        ("three", "3"),
        ("tree", "3"),         # common STT homophone
        ("I'll do number three", "3"),
        ("first", "1"),
        ("second", "2"),
        ("third", "3"),
        # Catalan (production language): u/un/una->1, dos/dues->2, tres->3
        ("u", "1"),
        ("un", "1"),
        ("una", "1"),
        ("dos", "2"),
        ("dues", "2"),
        ("tres", "3"),
        ("digues tres si us plau", "3"),
    ],
)
def test_number_words_map_to_keys(menu: VoiceMenu, utterance: str, expected: str) -> None:
    assert menu.match(utterance) == expected


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("difficult conversations", "1"),
        ("the difficult conversation one", "1"),  # both keyword + number -> number wins, still 1
        ("consultative selling", "2"),
        ("motivation to learn", "3"),  # 'to' is a number homophone but 'motivation' keyword + 'to'->2? number wins
    ],
)
def test_keyword_match(menu: VoiceMenu, utterance: str, expected: str) -> None:
    # Note: 'motivation to learn' contains 'to' (->2) AND 'motivation' (->3). Numbers win,
    # so this asserts the documented precedence rather than the topic name.
    result = menu.match(utterance)
    if utterance == "motivation to learn":
        assert result == "2"  # 'to' number token wins over the 'motivation' keyword
    else:
        assert result == expected


@pytest.mark.parametrize(
    "utterance,expected",
    [
        # English 4..6, cardinal + ordinal + digit
        ("four", "4"),
        ("4", "4"),
        ("number four please", "4"),
        ("fourth", "4"),
        ("five", "5"),
        ("option 5", "5"),
        ("fifth", "5"),
        ("six", "6"),
        ("I'll do number six", "6"),
        ("sixth", "6"),
        # Catalan 4..6. The ACCENTED ordinals are what a ca lexicon emits; the unaccented
        # spellings are what a lexicon built without diacritics emits for the same word.
        ("quatre", "4"),
        ("quart", "4"),
        ("cinc", "5"),
        ("cinquè", "5"),
        ("cinque", "5"),
        ("sis", "6"),
        ("sisè", "6"),
        ("sise", "6"),
        ("digues quatre si us plau", "4"),
    ],
)
def test_number_words_four_to_six_map_to_keys(
    menu6: VoiceMenu, utterance: str, expected: str
) -> None:
    assert menu6.match(utterance) == expected


def test_accented_number_words_survive_tokenisation(menu6: VoiceMenu) -> None:
    """The tokeniser must keep a Catalan accent inside the word.

    Splitting on ``[^a-z0-9]+`` cut "cinquè" into "cinqu" + "" -- an accented ordinal
    could then never match, silently reducing the Catalan menu to its unaccented forms.
    """
    assert menu6.match("cinquè.") == "5"
    assert menu6.match("motivació sis") == "6"


def test_three_option_menu_ignores_four_to_six(menu: VoiceMenu) -> None:
    # Number words are only honoured for keys the menu actually has (unchanged rule).
    for utterance in ("four", "five", "six", "quatre", "cinc", "sis"):
        assert menu.match(utterance) is None


@pytest.mark.parametrize("utterance", ["one two", "two one", "three two", "un dos"])
def test_conflicting_number_words_are_ambiguous(menu: VoiceMenu, utterance: str) -> None:
    """Two DIFFERENT topics in one utterance means we did not understand the answer.

    A constrained Vosk grammar force-aligns stray speech onto menu words, so "I do not
    want to" can come back as "one two". Taking the first token would silently start
    topic 1. Nobody answering a three-way menu names two
    topics, so this shape is garbage, not a choice.
    """
    assert menu.match(utterance) is None


@pytest.mark.parametrize(
    "utterance",
    ["four five", "six four", "cinc sis", "quatre dos", "one six", "sisè quatre"],
)
def test_conflicting_number_words_are_ambiguous_up_to_six(
    menu6: VoiceMenu, utterance: str
) -> None:
    """The ambiguity rule is the menu's only defence against force-aligned stray speech,
    and a six-way menu gives Vosk twice as many words to force stray audio onto."""
    assert menu6.match(utterance) is None


def test_repeating_the_same_topic_is_still_that_topic_up_to_six(menu6: VoiceMenu) -> None:
    assert menu6.match("six six") == "6"
    assert menu6.match("cinc cinquè") == "5"


def test_spoken_numbers_are_the_words_the_robot_should_say(menu6: VoiceMenu) -> None:
    # One canonical word per key, in menu order - what the "say ..." hint is built from.
    assert menu6.spoken_numbers("en") == ["one", "two", "three", "four", "five", "six"]
    assert menu6.spoken_numbers("ca") == ["u", "dos", "tres", "quatre", "cinc", "sis"]
    assert VoiceMenu.default_menu().spoken_numbers("ca") == ["u", "dos", "tres"]
    assert menu6.spoken_numbers("ca-ES") == menu6.spoken_numbers("ca")  # locale tag tolerated
    assert menu6.spoken_numbers("de") == []  # no number words -> no hint to build


def test_no_number_word_means_two_different_topics_across_languages() -> None:
    """``match`` is language-AGNOSTIC: every language's words are flattened into one map.

    A word that meant 3 in one language and 5 in another would be silently overwritten by
    whichever language is declared last, so one of the two menus would mis-start a module.
    """
    from emori_adapter.hostapp.voice_topics import _NUMBER_WORDS_BY_LANG

    seen: dict[str, str] = {}
    for words in _NUMBER_WORDS_BY_LANG.values():
        for word, key in words.items():
            assert seen.setdefault(word, key) == key, word


def test_six_option_menu_from_yaml_resolves_catalan_numbers(tmp_path) -> None:
    p = tmp_path / "voice_topics.ca.yaml"
    p.write_text(
        'prompt: "Digues u, dos, tres, quatre, cinc o sis."\n'
        'options:\n'
        + "".join(
            f'  "{n}": {{module_id: MOD{n}, name: Tema {n}}}\n' for n in range(1, 7)
        )
        + 'default: "1"\n',
        encoding="utf-8",
    )
    menu = VoiceMenu.from_yaml(p)
    assert menu.match("quatre") == "4"
    assert menu.match("cinc") == "5"
    assert menu.match("sis") == "6"
    assert menu.resolve("sisè")[1].module_id == "MOD6"


def test_repeating_the_same_topic_is_still_that_topic(menu: VoiceMenu) -> None:
    # Only CONFLICTING topics are ambiguous. "one one" / "number one, one" is emphasis.
    assert menu.match("one one") == "1"
    assert menu.match("first one") == "1"


def test_unrecognised_returns_none(menu: VoiceMenu) -> None:
    assert menu.match("banana") is None
    assert menu.match("") is None
    assert menu.match(None) is None


def test_resolve_falls_back_to_default(menu: VoiceMenu) -> None:
    key, opt = menu.resolve("banana")
    assert key == "1"  # default
    assert opt.module_id == "demo-module-1"


def test_resolve_recognised(menu: VoiceMenu) -> None:
    key, opt = menu.resolve("three")
    assert key == "3"
    assert opt.name == "Motivation to Learn"
    assert opt.module_id == "demo-module-3"


def test_match_only_honours_existing_keys() -> None:
    # A menu with only "1" must not return "2"/"3" even if those number words are spoken.
    menu = VoiceMenu(
        prompt="say one",
        options={"1": {"module_id": "M1", "name": "Only"}},
        default="1",
    )
    assert menu.match("two") is None
    assert menu.match("one") == "1"


def test_from_yaml_roundtrip(tmp_path) -> None:
    p = tmp_path / "voice_topics.yaml"
    p.write_text(
        'prompt: "hi"\n'
        'options:\n'
        '  "1": {module_id: AAA, name: Alpha, keywords: [alpha]}\n'
        '  "2": {module_id: BBB, name: Beta}\n'
        'default: "1"\n',
        encoding="utf-8",
    )
    menu = VoiceMenu.from_yaml(p)
    assert menu.options["1"].module_id == "AAA"
    assert menu.match("alpha") == "1"
    assert menu.match("two") == "2"


# --- spoken_numbers(lang, n): the words a menu of n options invites -----------------

def test_spoken_numbers_function_matches_the_menu_method() -> None:
    assert spoken_numbers("ca", 3) == ["u", "dos", "tres"]
    assert spoken_numbers("en", 6) == ["one", "two", "three", "four", "five", "six"]
    assert spoken_numbers("ca-ES", 2) == ["u", "dos"]
    assert spoken_numbers("en", 9) == spoken_numbers("en", 6)  # the words stop at six
    assert spoken_numbers("de", 3) == [] and spoken_numbers("en", 0) == []
    six = VoiceMenu(prompt="p", options={str(n): {"module_id": f"M{n}", "name": f"T{n}"}
                                         for n in range(1, 7)})
    for lang in ("ca", "en"):
        assert spoken_numbers(lang, 6) == six.spoken_numbers(lang)


# --- validate_menu_dict: the check every menu written on the robot passes first -----

def _valid() -> dict:
    return {
        "prompt": "Welcome. Say one for Alpha, or two for Beta.",
        "options": {
            "1": {"module_id": "MOD_A-1", "name": "Alpha", "keywords": ["alpha", "alpha team"]},
            "2": {"module_id": "MOD_B-2", "name": "Beta", "keywords": ["beta"]},
        },
        "default": "1",
    }


def _broken(change) -> dict:
    data = _valid()
    change(data)
    return data


def test_a_valid_menu_has_no_problem_and_loads() -> None:
    assert validate_menu_dict(_valid(), "en") == []
    assert validate_menu_dict(VoiceMenu.default_menu().model_dump(), "en") == []
    assert VoiceMenu.model_validate(_valid()).match("beta") == "2"


@pytest.mark.parametrize("data, slug", [
    ([1, 2], "menu_not_mapping"),
    ("prompt: hi", "menu_not_mapping"),
    (None, "menu_not_mapping"),
    (_broken(lambda d: d.pop("prompt")), "prompt_missing"),
    (_broken(lambda d: d.update(prompt="   ")), "prompt_missing"),
    (_broken(lambda d: d.update(prompt=5)), "prompt_missing"),
    (_broken(lambda d: d.update(options={})), "options_empty"),
    (_broken(lambda d: d.pop("options")), "options_empty"),
    (_broken(lambda d: d.update(options=[{"module_id": "A", "name": "A"}])), "options_empty"),
    (_broken(lambda d: d.update(options={str(n): {"module_id": f"M{n}", "name": f"T{n}"}
                                         for n in range(1, 8)})), "too_many_options"),
    (_broken(lambda d: d["options"].update({"7": {"module_id": "M7", "name": "Seven"}})),
     "option_key_not_number"),
    (_broken(lambda d: d["options"].update({"three": {"module_id": "M3", "name": "T"}})),
     "option_key_not_number"),
    (_broken(lambda d: d["options"].update({3: {"module_id": "M3", "name": "T"}})),
     "option_key_not_number"),
    (_broken(lambda d: d["options"].update({"0": {"module_id": "M0", "name": "T"}})),
     "option_key_not_number"),
    (_broken(lambda d: d["options"]["1"].update(module_id="has space")), "module_id_invalid"),
    (_broken(lambda d: d["options"]["1"].update(module_id="")), "module_id_invalid"),
    (_broken(lambda d: d["options"]["1"].update(module_id="M" * 1501)), "module_id_invalid"),
    (_broken(lambda d: d["options"]["1"].pop("module_id")), "module_id_invalid"),
    (_broken(lambda d: d["options"].update({"1": "not an option"})), "module_id_invalid"),
    (_broken(lambda d: d["options"]["1"].update(name="")), "name_invalid"),
    (_broken(lambda d: d["options"]["1"].update(name="Habits, routines")), "name_invalid"),
    (_broken(lambda d: d["options"]["1"].update(name="Two\nlines")), "name_invalid"),
    (_broken(lambda d: d["options"]["1"].update(name="N" * 61)), "name_invalid"),
    (_broken(lambda d: d["options"]["1"].update(name=7)), "name_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords="alpha")), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=None)), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["", "alpha"])), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=[" - "])), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=[5])), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["a\nb"])), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["k" * 61])), "keyword_invalid"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["alpha, beta"])),
     "keyword_has_comma"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["beta"])), "keyword_conflict"),
    (_broken(lambda d: d["options"]["1"].update(keywords=["bet"])), "keyword_conflict"),
    (_broken(lambda d: d["options"]["2"].update(keywords=["alpha-team"])),
     "keyword_conflict"),
    (_broken(lambda d: d.update(default="3")), "default_not_an_option"),
    (_broken(lambda d: d.update(default=1)), "default_not_an_option"),
])
def test_every_problem_has_its_own_stable_slug(data, slug) -> None:
    found = validate_menu_dict(data, "en")
    assert slug in found
    assert set(found) <= set(MENU_PROBLEMS)


def test_slugs_come_once_each_in_a_stable_order() -> None:
    data = {"options": {"1": {"module_id": "bad id", "name": "a,b"},
                        "2": {"module_id": "also bad", "name": ""}},
            "default": "9"}
    found = validate_menu_dict(data, "ca")
    assert found == ["prompt_missing", "module_id_invalid", "name_invalid",
                     "default_not_an_option"]
    assert found == [slug for slug in MENU_PROBLEMS if slug in found]


def test_a_conflicting_keyword_is_reported_where_it_is() -> None:
    data = _valid()
    data["options"]["2"]["keywords"] = ["beta", "alpha"]  # equal to option 1's "alpha"
    rows = [row for row in menu_problems(data, "en") if row["code"] == "keyword_conflict"]
    # both sides of an equal pair, each by option key and keyword index; "alpha" also
    # sits inside option 1's "alpha team"
    assert {(row["option"], row["keyword"]) for row in rows} == {("1", 0), ("2", 1)}


def test_a_keyword_repeated_inside_one_option_is_no_conflict() -> None:
    data = _valid()
    data["options"]["1"]["keywords"] = ["alpha", "alpha", "alpha team"]
    assert validate_menu_dict(data, "en") == []


def test_a_menu_without_a_default_or_keywords_is_valid() -> None:
    data = {"prompt": "Digues u.", "options": {"1": {"module_id": "M1", "name": "Tema"}}}
    assert validate_menu_dict(data, "ca") == []
    data["default"] = None
    assert validate_menu_dict(data, "ca") == []


@pytest.mark.parametrize("data", [
    {"prompt": "p", "options": {"1": {"module_id": "M1", "name": "N", "keywords": [5]}}},
    {"prompt": "p", "options": {"1": {"module_id": "M1", "name": "N", "keywords": None}}},
    {"prompt": "p", "options": {1: {"module_id": "M1", "name": "N"}}},
    {"prompt": "p", "options": {"1": {"module_id": "M1", "name": 7}}},
    {"prompt": 5, "options": {"1": {"module_id": "M1", "name": "N"}}},
    {"prompt": "p", "options": {"1": {"module_id": "M1", "name": "N"}}, "default": 1},
    {"prompt": "p", "options": {"1": {"name": "N"}}},
])
def test_what_the_hostapp_cannot_load_the_check_refuses(data) -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        VoiceMenu.model_validate(data)
    assert validate_menu_dict(data, "en") != []


def test_the_shipped_menus_pass_the_check_and_their_prompts_name_their_options() -> None:
    for name, lang in (("configs/voice_topics.yaml", "en"),
                       ("configs/voice_topics.ca.yaml", "ca"),
                       ("configs/voice_topics.example.yaml", "en")):
        data = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        assert validate_menu_dict(data, lang) == [], name
        menu = VoiceMenu.model_validate(data)
        for option in menu.options.values():
            assert option.name in " ".join(menu.prompt.split()), (name, option.name)
        # every option is reachable by its own keyword, and only by it
        for key, option in menu.options.items():
            for keyword in option.keywords:
                assert menu.match(keyword) == key, (name, keyword)


def test_the_tracked_development_menus_name_only_placeholder_modules() -> None:
    """The tracked menus are development data: their ids can never reach a learner."""
    for name, prefix in (("configs/voice_topics.yaml", "dev-en-module-"),
                         ("configs/voice_topics.ca.yaml", "example-ca-module-"),
                         ("configs/voice_topics.example.yaml", "demo-module-")):
        menu = VoiceMenu.from_yaml(ROOT / name)
        assert all(o.module_id.startswith(prefix) for o in menu.options.values()), name
    assert len(VoiceMenu.from_yaml(ROOT / "configs/voice_topics.ca.yaml").options) == 6


# --- extra top-level keys: the robot's own menu records where it came from ----------

def test_from_yaml_tolerates_the_built_menu_keys_and_any_unknown_key(tmp_path) -> None:
    p = tmp_path / "voice_topics.ca.yaml"
    p.write_text(
        "# built by the robot\n"
        "generated_from: pokamind_whoami\n"
        "authored: title_draft\n"
        "some_future_key: {nested: true}\n"
        'prompt: "Digues u per Alfa, o dos per Beta."\n'
        "options:\n"
        "  '1': {module_id: AAA, name: Alfa, keywords: [alfa], note: not read}\n"
        "  '2': {module_id: BBB, name: Beta}\n"
        "default: '1'\n",
        encoding="utf-8",
    )
    menu = VoiceMenu.from_yaml(p)
    assert set(menu.model_dump()) == {"prompt", "options", "default"}
    # matching is exactly what it is for a menu without them
    assert menu.match("dos") == "2" and menu.match("alfa") == "1"
    assert menu.resolve("res")[1].module_id == "AAA"
    assert validate_menu_dict(yaml.safe_load(p.read_text(encoding="utf-8")), "ca") == []
