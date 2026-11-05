"""The screenless verbal menu runs a CONSTRAINED grammar with a confidence gate.

Open Vosk recognition let a passer-by's digit homophone start a session, and three
non-exact results silently auto-started topic 1. These pin: a per-language grammar built
from the menu, a scored /api/stt grammar path, and a kiosk that re-prompts and then hands
back to the badge/button fallback instead of guessing.
"""

from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import pages, server
from emori_adapter.hostapp.voice_topics import VoiceMenu


# --- grammar construction (pure, no audio) ------------------------------------------

def test_catalan_menu_yields_a_catalan_grammar(monkeypatch):
    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    menu = server._load_menu("ca")
    phrases = set(menu.grammar_phrases("ca"))
    assert {"u", "dos", "tres"} <= phrases
    # English number words must not leak into a Catalan grammar: they are OOV in the ca
    # lexicon (Vosk drops them with a warning) and only widen the false-accept surface.
    assert not {"one", "two", "three", "too", "tree"} & phrases
    # The menu's own Catalan keywords stay selectable. The Catalan menu is deployment
    # data, so this pins the relationship rather than one course's vocabulary.
    keywords = {" ".join(k.lower().split())
                for o in menu.options.values() for k in o.keywords}
    assert keywords and {k for k in keywords if "," not in k} <= phrases


def test_english_menu_yields_an_english_grammar():
    phrases = VoiceMenu.default_menu().grammar_phrases("en")
    assert {"one", "two", "three", "difficult", "motivation"} <= set(phrases)
    assert not {"u", "dues", "tercer"} & set(phrases)


def test_grammar_drops_bare_digits_commas_and_duplicates():
    phrases = VoiceMenu.default_menu().grammar_phrases("en")
    assert not {"1", "2", "3"} & set(phrases)   # digits are the BUTTON path, not speech
    assert not any("," in p for p in phrases)   # /api/stt splits grammar on commas
    assert len(phrases) == len(set(phrases))
    assert all(p and p == p.strip().lower() for p in phrases)


def test_grammar_only_offers_keys_this_menu_has():
    menu = VoiceMenu(prompt="say one",
                     options={"1": {"module_id": "M1", "name": "Only"}},
                     default=None)
    phrases = menu.grammar_phrases("en")
    assert "one" in phrases
    assert not {"two", "three"} & set(phrases)


def _menu6(prompt: str = "digues un número") -> VoiceMenu:
    """The six-option course shape."""
    return VoiceMenu(prompt=prompt,
                     options={str(n): {"module_id": f"M{n}", "name": f"Tema {n}"}
                              for n in range(1, 7)},
                     default=None)


def test_grammar_scales_to_a_six_option_menu():
    ca = set(_menu6().grammar_phrases("ca"))
    assert {"quatre", "cinc", "sis"} <= ca            # cardinals the prompt asks for
    assert {"quart", "cinquè", "cinque", "sisè", "sise"} <= ca   # ordinals + unaccented
    en = set(_menu6().grammar_phrases("en"))
    assert {"four", "five", "six", "fourth", "fifth", "sixth"} <= en


def test_grammar_offers_four_to_six_only_when_the_menu_has_them():
    # The shipped three-option menus must not widen their grammar with words no option
    # can be reached by: every extra word is one more way stray speech force-aligns.
    for lang, extra in (("ca", {"quatre", "cinc", "sis", "quart", "cinquè", "sisè"}),
                        ("en", {"four", "five", "six", "fourth", "fifth", "sixth"})):
        assert not extra & set(VoiceMenu.default_menu().grammar_phrases(lang))


def test_unknown_language_drops_number_words_but_keeps_keywords():
    phrases = VoiceMenu.default_menu().grammar_phrases("de")
    assert not {"one", "two", "u", "dos"} & set(phrases)
    assert "motivation" in phrases


def test_a_menu_built_from_the_record_yields_a_clean_grammar_per_language():
    """The robot's own menu (hostapp/menu_builder.py) feeds the same grammar path: its
    language's number words, its keywords, nothing with a comma, nothing English in a
    Catalan grammar."""
    from emori_adapter.hostapp import menu_builder

    def _module(module_id, title, lang):
        return {"module_id": module_id, "title": title, "category": None,
                "language": lang, "available": True, "problem": None}

    whoami = {"modules": [_module(f"CA{n}", title, "ca") for n, title in enumerate(
        ("Converses difícils, nivell 1", "Escolta activa", "Treball en equip",
         "Gestió del temps", "Parlar en públic", "Resolució de conflictes"), start=1)]
        + [_module("EN1", "Active listening: the basics", "en")]}
    drafts = menu_builder.build_drafts(whoami)

    ca = VoiceMenu.model_validate(drafts["ca"]["menu"])
    phrases = ca.grammar_phrases("ca")
    assert {"u", "dos", "tres", "quatre", "cinc", "sis"} <= set(phrases)
    assert {"converses difícils", "converses dificils", "escolta activa"} <= set(phrases)
    assert not {"one", "two", "three"} & set(phrases)
    assert not any("," in phrase for phrase in phrases)
    assert ca.match("quatre") == "4" and ca.match("escolta activa") == "2"

    en = VoiceMenu.model_validate(drafts["en"]["menu"])
    assert en.grammar_phrases("en")[:1] == ["one"]
    assert "active listening" in en.grammar_phrases("en")
    assert not {"two", "u", "dos"} & set(en.grammar_phrases("en"))


def test_match_still_accepts_both_languages():
    # grammar_phrases is per-language; match() must stay language-agnostic (the Web Speech
    # fallback and the manual page feed it either language's tokens).
    menu = VoiceMenu.default_menu()
    assert menu.match("dues") == "2"
    assert menu.match("two") == "2"


# --- /api/stt grammar path now reports a confidence ---------------------------------

class _ScoringSttEngine:
    """An engine that reports per-word confidence (the real VoskEngine shape)."""

    available = True

    def __init__(self, text: str = "dos", conf: float = 0.94):
        self.calls: list[tuple[list[str], str]] = []
        self._out = (text, conf)

    def transcribe(self, pcm, lang="en", sample_rate=16000):
        return "open recognition"

    def transcribe_grammar(self, pcm, phrases, lang="en", sample_rate=16000):
        return self._out[0]

    def transcribe_grammar_conf(self, pcm, phrases, lang="en", sample_rate=16000):
        self.calls.append((list(phrases), lang))
        return self._out


def _speech_client(engine):
    from fastapi import FastAPI

    from emori_adapter.hostapp import speech_api

    app = FastAPI()
    speech_api.mount(app, tts=None, stt=engine)
    return TestClient(app)


def test_stt_grammar_returns_a_confidence():
    engine = _ScoringSttEngine(text="dos", conf=0.94)
    body = _speech_client(engine).post(
        "/api/stt?lang=ca&grammar=u,dos,tres", content=b"\x00\x01" * 16000).json()
    assert body == {"text": "dos", "conf": 0.94}
    assert engine.calls == [(["u", "dos", "tres"], "ca")]


def test_stt_grammar_without_a_scorer_keeps_the_legacy_shape():
    # The wake/keyword engines expose no scorer; that response must not change
    # (tests/test_speech.py:105-119 asserts the exact dict).
    from test_speech import _FakeSttEngine

    body = _speech_client(_FakeSttEngine()).post(
        "/api/stt?lang=en&grammar=hey%20emori", content=b"\x00\x01" * 16000).json()
    assert body == {"text": "hey emori"}


# --- VoskEngine confidence maths (no model on disk) ---------------------------------

class _FakeVosk:
    """Stands in for the ``vosk`` module inside VoskEngine."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.grammar = None
        self.words_enabled = False

    def KaldiRecognizer(self, model, rate, grammar=None):
        self.grammar = grammar
        outer = self

        class _Rec:
            def SetWords(self, on):
                outer.words_enabled = bool(on)

            def AcceptWaveform(self, pcm):
                return True

            def FinalResult(self):
                return json.dumps(outer.payload)

        return _Rec()


def _engine_with(payload: dict):
    from emori_adapter.hostapp.speech_api import VoskEngine

    engine = VoskEngine.__new__(VoskEngine)  # bypass __init__ (imports vosk + loads a model)
    engine._vosk = _FakeVosk(payload)
    engine._paths = {"en": "models/vosk-en"}
    engine._loaded = {"models/vosk-en": object()}
    return engine


def test_conf_is_the_min_over_non_unk_words_and_unk_is_stripped():
    # The SHAPE below ([unk] inside `text`, [unk] scored 1.000) is what was measured with
    # vosk 0.3.45 + models/vosk-en + Piper voices/en.onnx: a
    # grammar FORCES stray audio onto a menu word, and Vosk scores its own [unk] garbage
    # token 1.000 - so the weak real word's conf is what must be gated on. The 0.42 is a
    # SYNTHETIC weak-word value, not an observed one (the bench measured 0.832 / 1.000).
    engine = _engine_with({
        "text": "two [unk]",
        "result": [
            {"word": "two", "conf": 0.42, "start": 0.06, "end": 0.21},
            {"word": "[unk]", "conf": 1.0, "start": 0.21, "end": 1.77},
        ],
    })
    text, conf = engine.transcribe_grammar_conf(b"\x00" * 3200, ["one", "two"], "en")
    assert text == "two"                        # the garbage token never reaches match()
    assert conf == pytest.approx(0.42)          # NOT 1.0
    assert engine._vosk.words_enabled is True   # SetWords(True) or there are no confs
    assert "[unk]" in engine._vosk.grammar


def test_pure_garbage_scores_zero_and_yields_no_text():
    # Real measurement: "The weather is cold today." under the menu grammar came back as
    # text "[unk]" with no scored menu word at all.
    engine = _engine_with({"text": "[unk]",
                           "result": [{"word": "[unk]", "conf": 1.0,
                                       "start": 0.0, "end": 1.8}]})
    assert engine.transcribe_grammar_conf(b"\x00" * 3200, ["one"], "en") == ("", 0.0)


def test_missing_word_detail_reports_a_neutral_confidence():
    # An engine build that ignores SetWords must not brick selection: the grammar is then
    # the only constraint left and the gate has nothing to judge.
    engine = _engine_with({"text": "tres"})
    assert engine.transcribe_grammar_conf(b"\x00" * 3200, ["tres"], "ca") == ("tres", 1.0)


# --- the kiosk page: grammar + gate + no silent default ------------------------------

def _kiosk_html(monkeypatch, lang: str = "ca") -> str:
    monkeypatch.setattr(server, "EMORI_LANG", lang)
    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    app = server._build_app(overrides={"idle": False, "watchdog": False, "gesture": False})
    return TestClient(app).get("/").text


def _menu_tuning_from(html: str) -> dict:
    return json.loads(html.split("const MENU=", 1)[1].split(";", 1)[0])


def _strings_from(html: str) -> dict:
    return json.loads(html.split("const LANG=", 1)[1].split(", S=", 1)[1].split(";\n", 1)[0])


def _options_from(html: str) -> list[dict]:
    return json.loads(html.split("const PROMPT=", 1)[1]
                      .split(", OPTIONS=", 1)[1].split(", demo=", 1)[0])


def test_page_ships_the_catalan_grammar_and_the_gate(monkeypatch):
    for name in ("EMORI_MENU_GRAMMAR", "EMORI_MENU_CONF_MIN",
                 "EMORI_MENU_MAX_TRIES", "EMORI_MENU_LISTEN_MS"):
        monkeypatch.delenv(name, raising=False)
    html = _kiosk_html(monkeypatch, "ca")
    assert "__MENU_TUNING__" not in html        # placeholder actually substituted
    tuning = _menu_tuning_from(html)
    grammar = tuning["grammar"].split(",")
    assert {"u", "dos", "tres"} <= set(grammar)
    assert "two" not in grammar
    assert tuning["confMin"] == 0.3 and tuning["maxTries"] == 3
    assert "&grammar='+encodeURIComponent(MENU.grammar)" in html
    assert "conf<MENU.confMin" in html


def _menu_yaml(tmp_path, options: int) -> str:
    """An ``options``-sized menu on disk (a robot's menu is built from its Pokamind
    record and changes size with it, so page tests pin their own)."""
    p = tmp_path / f"voice_topics.{options}.yaml"
    p.write_text(
        'prompt: "Hola."\n'
        'options:\n'
        + "".join(f'  "{n}": {{module_id: MOD{n}, name: Tema {n}}}\n'
                  for n in range(1, options + 1))
        + 'default: null\n',
        encoding="utf-8",
    )
    return str(p)


def _menu_page(monkeypatch, tmp_path, lang: str, options: int) -> str:
    monkeypatch.setenv("EMORI_VOICE_TOPICS", _menu_yaml(tmp_path, options))
    monkeypatch.setattr(server, "EMORI_LANG", lang)
    for name in ("EMORI_MENU_GRAMMAR", "EMORI_MENU_CONF_MIN",
                 "EMORI_MENU_MAX_TRIES", "EMORI_MENU_LISTEN_MS"):
        monkeypatch.delenv(name, raising=False)
    app = server._build_app(overrides={"idle": False, "watchdog": False, "gesture": False})
    return TestClient(app).get("/").text


def test_page_scales_grammar_buttons_and_the_number_hint_to_six(monkeypatch, tmp_path):
    """Every per-option surface on the landing page follows the menu, not the number 3.

    The grammar (what Vosk may hear), the fallback buttons (what a hand can tap) and the
    "Digues ..." hint (what the screen tells a person to say) all come from the same menu;
    a six-option course that still invites three is a course whose last three modules are
    unreachable.
    """
    html = _menu_page(monkeypatch, tmp_path, "ca", 6)

    assert {"quatre", "cinc", "sis"} <= set(_menu_tuning_from(html)["grammar"].split(","))
    assert [o["key"] for o in _options_from(html)] == ["1", "2", "3", "4", "5", "6"]
    strings = _strings_from(html)
    assert strings["say_numbers"] == "Digues u, dos, tres, quatre, cinc o sis."
    assert "{numbers}" not in html      # placeholder actually substituted
    assert "u, dos, tres, quatre, cinc o sis" in strings["too_dark"]


def test_the_number_hint_is_unchanged_for_a_three_option_menu(monkeypatch, tmp_path):
    assert _strings_from(_menu_page(monkeypatch, tmp_path, "ca", 3))["say_numbers"] == \
        "Digues u, dos o tres."
    assert _strings_from(_menu_page(monkeypatch, tmp_path, "en", 3))["say_numbers"] == \
        "Say one, two, or three."


def test_grammar_kill_switch_restores_open_recognition(monkeypatch):
    monkeypatch.setenv("EMORI_MENU_GRAMMAR", "0")
    assert _menu_tuning_from(_kiosk_html(monkeypatch, "ca"))["grammar"] == ""


def test_voice_menu_endpoint_exposes_the_grammar(monkeypatch):
    monkeypatch.setattr(server, "EMORI_LANG", "ca")
    monkeypatch.delenv("EMORI_VOICE_TOPICS", raising=False)
    app = server._build_app(overrides={"idle": False, "watchdog": False, "gesture": False})
    assert "dos" in TestClient(app).get("/api/voice-menu").json()["grammar"].split(",")


def test_kiosk_never_auto_commits_the_default_topic():
    # The screenless trap this task removes: `tries++; if(tries>=3){ beginSession(d,text); }`
    # started Converses difícils without the person ever choosing it.
    src = pages.VOICE_HTML
    assert "tries>=3" not in src
    # exactly two mentions remain: the EXACT-match call site and the function definition
    assert src.count("beginSession(") == 2
    assert "function onMiss()" in src and "function giveUp()" in src
    assert "S.menu_giving_up" in src


def test_give_up_string_exists_in_both_kiosk_languages():
    # tests/test_catalan_kiosk.py:18 pins set(UI_STRINGS["ca"]) == set(UI_STRINGS["en"]),
    # so adding this key to one language only breaks that suite.
    assert pages.UI_STRINGS["ca"]["menu_giving_up"]
    assert pages.UI_STRINGS["en"]["menu_giving_up"]
    assert "acreditació" in pages.UI_STRINGS["ca"]["menu_giving_up"]


# --- async reentrancy, give-up audio overlap, env clamps -----------------------------

def test_late_stt_and_select_answers_cannot_reenter_the_kiosk():
    """A /api/stt or /api/voice-select round-trip that outlives its listen window must be
    dropped. Vosk decoding a 3.5s chunk on the CM4 can take ~1s, so the 12s listen deadline
    routinely fires first: onMiss() then tears down the mic and re-prompts, and the stale
    answer would either double-count the miss (killing the freshly opened mic) or start a
    session from audio captured BEFORE the re-prompt."""
    src = pages.VOICE_HTML
    # every window open and every teardown bumps the generation
    assert "phase='listen'; listenGen++;" in src
    assert "function stopListen(){ listenGen++;" in src
    # both awaiting call sites capture it and re-check after the await
    assert src.count("const gen=listenGen;") == 2
    assert "if(navigating||phase!=='listen'||gen!==listenGen) return;" in src   # flushStt
    assert "if(navigating||gen!==listenGen) return;" in src                     # select


def test_give_up_opens_the_camera_only_after_it_finishes_speaking():
    """say() is fire-and-forget. Calling startCam() as a sibling statement re-scanned the
    badge the person is still holding within a frame, so onBadge() spoke a SECOND Audio
    element over the ~4s give-up line on the robot's single speaker - and then opened the
    mic over both, feeding the robot's own speech into the confidence gate."""
    src = pages.VOICE_HTML
    assert "say(S.menu_giving_up, startCam);" in src
    assert "say(S.menu_giving_up); startCam();" not in src


def test_handback_to_the_scanner_preserves_the_identified_badge():
    """A voice-menu give-up or a mic hard-fail hands the SAME, already-identified
    staff member back to the badge+button fallback.  Clearing qr there downgraded
    their very next topic-button tap to an ANONYMOUS session (select() posts the
    current qr).  The hand-back must keep qr: an ACTUAL new visitor is still
    re-identified authoritatively by the re-opened camera (onBadge overwrites qr)
    or by the explicit anonymous button (onBadge('')), exactly as the normal
    identified state already behaves."""
    src = pages.VOICE_HTML
    # Neither hand-back may zero a still-valid identity...
    assert "tries=0; phase='scan'; qr=''; lastQr=''; lastQrAt=0;" not in src
    # ...but both still drop the 3s badge debounce and reset the miss counter, so
    # re-showing the SAME badge re-greets and a DIFFERENT badge overwrites qr.
    assert src.count("tries=0; phase='scan'; lastQr=''; lastQrAt=0;") == 2
    # The camera (re-opened after the give-up line) is what re-establishes identity
    # on a real new visitor, and the topic buttons attribute to the retained badge.
    assert "say(S.menu_giving_up, startCam);" in src
    assert "select(o.key)" in src


@pytest.mark.parametrize("value", ["0", "-1"])
def test_zero_or_negative_max_tries_is_clamped(monkeypatch, value):
    # maxTries=0 satisfies `tries>=MENU.maxTries` on the FIRST miss: the kiosk would give
    # up before ever re-prompting.
    monkeypatch.setenv("EMORI_MENU_MAX_TRIES", value)
    monkeypatch.delenv("EMORI_MENU_LISTEN_MS", raising=False)
    assert server._menu_tuning(VoiceMenu.default_menu(), "en")["maxTries"] == 1


@pytest.mark.parametrize("value", ["0", "-5", "17"])
def test_a_too_short_listen_window_is_clamped(monkeypatch, value):
    # listenMs=0 makes armListenDeadline() fire on the next tick, spinning
    # listen -> onMiss -> listen through every try in milliseconds.
    monkeypatch.setenv("EMORI_MENU_LISTEN_MS", value)
    monkeypatch.delenv("EMORI_MENU_MAX_TRIES", raising=False)
    assert server._menu_tuning(VoiceMenu.default_menu(), "en")["listenMs"] == 1000


def test_valid_tuning_values_are_not_clamped(monkeypatch):
    monkeypatch.setenv("EMORI_MENU_MAX_TRIES", "5")
    monkeypatch.setenv("EMORI_MENU_LISTEN_MS", "20000")
    tuning = server._menu_tuning(VoiceMenu.default_menu(), "en")
    assert tuning["maxTries"] == 5 and tuning["listenMs"] == 20000


def test_grammar_helpers_share_one_spelling_of_the_unknown_token():
    # Two literals for the same magic token in adjacent methods is how they drift apart.
    from emori_adapter.hostapp import speech_api

    import inspect

    assert speech_api.VoskEngine._UNK == "[unk]"
    for name in ("transcribe_grammar", "transcribe_grammar_conf"):
        body = inspect.getsource(getattr(speech_api.VoskEngine, name))
        code = body.split('"""')[-1]  # ignore the docstrings, which quote the token
        assert '"[unk]"' not in code and "'[unk]'" not in code, name
        assert "self._UNK" in code, name


# --- the on-robot gate: does the ca LEXICON actually cover this grammar? -------------

@pytest.mark.skipif(
    not (os.path.isdir("models/vosk-ca") and os.path.exists("voices/ca.onnx")),
    reason="Catalan Vosk model / Piper voice not installed (scripts/get_voice_models.sh)",
)
def test_real_catalan_grammar_accepts_every_spoken_number(tmp_path):
    """Proves the ca lexicon covers the grammar for EVERY option the menu offers.

    This is the gate for options 4..6: quatre/cinc/sis are only reachable by voice
    if vosk-ca actually holds them, and an OOV word is dropped with nothing but a log line
    -- the option then simply cannot be chosen, which is invisible on a screenless robot.
    The dev tree ships models/vosk-en only, so this is SKIPPED here and must be run ON THE
    ROBOT. Mirrors the skip pattern in tests/test_speech.py.
    """
    import wave

    pytest.importorskip("vosk", reason="vosk not installed")
    from emori_adapter.hostapp.speech_api import VoskEngine
    from emori_adapter.speech.tts import PiperTts

    menu = server._load_menu("ca")
    words = menu.spoken_numbers("ca")
    assert words, "the Catalan menu offers no spoken number at all"
    phrases = menu.grammar_phrases("ca")
    tts = PiperTts({"ca": "voices/ca.onnx"}, out_dir=str(tmp_path))
    engine = VoskEngine({"ca": "models/vosk-ca"})

    for word in words:
        wav = tts.synthesize(f"Digues {word}.", "ca")
        assert wav
        with wave.open(wav, "rb") as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        text, conf = engine.transcribe_grammar_conf(pcm, phrases, "ca", rate)
        assert menu.match(text) == menu.match(word), \
            f"ca grammar did not recognise {word!r} (got {text!r})"
        # The gate is EMORI_MENU_CONF_MIN (0.30). The 0.500/0.333 once measured for "one"/
        # "two" were a tie with the grammar's own homophones, not a low score for short
        # words (server._menu_tuning's docstring). The discredited 0.6 seed is not the bar.
        assert conf >= 0.3, f"{word!r} scored {conf}, below the EMORI_MENU_CONF_MIN default"


# --- homophones and carriers (with "won" in the grammar, every "one" scored 0.500) ---

def test_the_en_grammar_leaves_out_the_homophones_but_match_still_maps_them():
    from emori_adapter.hostapp.voice_topics import VoiceMenu, VoiceOption

    menu = VoiceMenu(prompt="p", options={
        "1": VoiceOption(module_id="a", name="Team-Work Talk", keywords=["team-work"]),
        "2": VoiceOption(module_id="b", name="Feedback"),
        "3": VoiceOption(module_id="c", name="Planning")})
    phrases = menu.grammar_phrases("en")
    assert not {"won", "to", "too"} & set(phrases)
    assert {"one", "two", "three"} <= set(phrases)
    assert menu.match("to") == "2" and menu.match("won") == "1"
    # A hyphenated keyword goes into the grammar the way Vosk can say it, and the text
    # Vosk returns still selects it.
    assert "team work" in phrases and "team-work" not in phrases
    assert menu.match("team work") == "1"


def test_carriers_absorb_stray_speech_without_choosing_anything():
    from emori_adapter.hostapp.voice_topics import (
        _GRAMMAR_CARRIERS_BY_LANG, _NUMBER_WORDS, VoiceMenu, VoiceOption)

    menu = VoiceMenu(prompt="p", options={
        "1": VoiceOption(module_id="a", name="Listening", keywords=["listening"]),
        "2": VoiceOption(module_id="b", name="Feedback", keywords=["giving feedback"]),
        "3": VoiceOption(module_id="c", name="Planning", keywords=["time-keeping"])})
    phrases = menu.grammar_phrases("en")
    for carrier in _GRAMMAR_CARRIERS_BY_LANG["en"]:
        assert carrier in phrases
        assert menu.match(carrier) is None, carrier
        assert not set(carrier.split()) & set(_NUMBER_WORDS), carrier
        for opt in menu.options.values():
            for kw in opt.keywords:
                assert kw.replace("-", " ") not in carrier, (kw, carrier)
    # The answers a carrier must never swallow still choose.
    assert menu.match("number two please") == "2"
    assert menu.match("option one") == "1"
