"""Supported kiosk languages are Catalan and English, and no other.

Locks the visitor-facing language set to {ca, en} across the UI strings, the spoken
end-of-session summary, and the verbal topic menu, and proves the Catalan menu config
is auto-selected for EMORI_LANG=ca.
"""

from __future__ import annotations


def test_ui_strings_are_catalan_and_english_only():
    from emori_adapter.hostapp.pages import UI_STRINGS

    assert "ca" in UI_STRINGS
    assert "en" in UI_STRINGS
    assert "es" not in UI_STRINGS
    # Every key present in English must exist in Catalan (strings_for reads S.<key>).
    assert set(UI_STRINGS["ca"]) == set(UI_STRINGS["en"])
    # Spot-check real Catalan copy, not an English fallback.
    assert "acreditació" in UI_STRINGS["ca"]["show_badge"]
    # The spoken-number hint is a TEMPLATE: the words come from the live menu (a six-option
    # course must not invite only three), so the copy carries the placeholder, not "u, dos".
    assert UI_STRINGS["ca"]["say_numbers"] == "Digues {numbers}."


def test_strings_for_resolves_catalan():
    import json

    from emori_adapter.hostapp.pages import strings_for

    payload = json.loads(strings_for("ca"))
    assert payload["hello"] == "Hola"
    assert "Gràcies" in payload["session_over"]
    # No caller-supplied menu -> the built-in three-option wording, unchanged.
    assert payload["say_numbers"] == "Digues u, dos o tres."
    assert json.loads(strings_for("ca", ["u", "dos", "tres", "quatre", "cinc", "sis"]))[
        "say_numbers"] == "Digues u, dos, tres, quatre, cinc o sis."
    # A language with no number words must not render "Digues ." on the kiosk.
    assert json.loads(strings_for("ca", []))["say_numbers"] == "Digues u, dos o tres."


def test_lang_bcp_has_catalan_and_no_other_language():
    from emori_adapter.hostapp import pages

    # The browser-fallback BCP-47 map (inside _SPEECH_JS): ca-ES in, es-ES out.
    assert "ca:'ca-ES'" in pages._SPEECH_JS
    assert "es-ES" not in pages._SPEECH_JS


def test_session_summary_and_tips_are_catalan_and_english_only():
    from emori_adapter.hostapp.driver import (_SUMMARY_NO_SIGNAL, _SUMMARY_TEXT,
                                              _TIPS)

    assert set(_SUMMARY_TEXT) == {"ca", "en"}
    assert set(_TIPS) == {"ca", "en"}
    assert "pràctica" in _SUMMARY_TEXT["ca"]
    assert set(_TIPS["ca"]) == {"escalation", "high", "low", "unmeasured"}
    # The no-measurement summary is a spoken string like any other and must not
    # fall back to English on a Catalan site.
    assert set(_SUMMARY_NO_SIGNAL) == {"ca", "en"}
    assert "pràctica" in _SUMMARY_NO_SIGNAL["ca"]
    for text in _SUMMARY_NO_SIGNAL.values():
        # It cannot claim a percentage -- there is no measurement behind it.
        assert "{avg}" not in text and "{style}" not in text


def test_number_words_include_catalan():
    from emori_adapter.hostapp.voice_topics import _NUMBER_WORDS

    assert _NUMBER_WORDS["u"] == "1"
    assert _NUMBER_WORDS["dos"] == "2"
    assert _NUMBER_WORDS["tres"] == "3"


def test_load_menu_selects_catalan_config_for_ca(monkeypatch):
    from emori_adapter.hostapp import server

    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    ca_menu = server._load_menu("ca")
    # WHICH course is configured is deployment data, so pin the wiring: the ca file is
    # the one picked up, its Catalan prompt names its own options, and Catalan number
    # words select them.
    assert ca_menu.options["1"].name in ca_menu.prompt
    assert ca_menu.match("u") == "1"
    assert "u" in ca_menu.grammar_phrases("ca")

    en_menu = server._load_menu("en")
    assert en_menu.prompt != ca_menu.prompt  # a distinct English menu is chosen for en
