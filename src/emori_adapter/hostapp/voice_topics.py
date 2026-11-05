"""Screenless **verbal** topic selection: a spoken number -> a Pokamind module.

The robot has no display, so the learner does not pick a topic on screen. Instead the
robot speaks a short menu ("say one for ..., two for ..., three for ...") and listens; the
recognised utterance is matched to a menu option whose ``module_id`` is then carried
through partner SSO so Pokamind starts that module directly and **skips its topic
picker**.

This module is the pure, testable core: load a menu (YAML or the built-in default) and map
a free-form recognised string to an option key. The actual speech capture lives at the
edges (browser Web Speech API in the simulator, offline Vosk on the robot) and only feeds
``VoiceMenu.match`` the recognised text -- so the mapping is unit-testable with no audio.

:func:`validate_menu_dict` is the one check every menu written on the robot passes first
(the menu built from this robot's Pokamind record, hostapp/menu_builder.py, and a menu
saved on the phone admin page): stable slugs, and a menu with none of them always loads.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict

# Spoken/STT forms for the digits 1..6, including common homophones an STT engine emits.
# English ("to"/"too" for two, "tree" for three, "won" for one) and Catalan:
# "u" is the isolated numeral for 1 - the say_numbers prompt
# says "u" to bias toward the cleanest token; "un"/"una" are the cardinal/article forms
# Vosk-ca may emit (also Catalan's indefinite article, so slightly false-positive prone -
# QR/gesture stays the primary path). "dos"/"dues" for 2, "tres" for 3.
# 4..6 exist because a course can have six modules; a
# menu only ever offers the keys it actually has, so the extra words stay out of a
# three-option grammar (``grammar_phrases``/``match`` both filter on ``self.options``).
# The Catalan ordinals are listed BOTH accented ("cinquè") and unaccented ("cinque"):
# which one comes back is a property of the lexicon the Vosk model was built with, not of
# the speaker, and an unmatched ordinal is a silently unreachable menu option.
# Per-language sets. A Vosk *grammar* must not mix languages: an English word is simply
# missing from the ca lexicon (Vosk logs "Ignoring word missing in vocabulary" and drops
# it) while adding one more way for stray speech to be forced onto a menu word.
# ORDER MATTERS: the first non-digit form of each key is the CANONICAL spoken word
# (``spoken_numbers``) - what the robot's own menu hint invites people to say.
_NUMBER_WORDS_BY_LANG: dict[str, dict[str, str]] = {
    "en": {
        "1": "1", "one": "1", "won": "1", "first": "1",
        "2": "2", "two": "2", "to": "2", "too": "2", "second": "2",
        "3": "3", "three": "3", "tree": "3", "third": "3",
        "4": "4", "four": "4", "fourth": "4",
        "5": "5", "five": "5", "fifth": "5",
        "6": "6", "six": "6", "sixth": "6",
    },
    "ca": {
        "1": "1", "u": "1", "un": "1", "una": "1", "primer": "1",
        "2": "2", "dos": "2", "dues": "2", "segon": "2",
        "3": "3", "tres": "3", "tercer": "3",
        "4": "4", "quatre": "4", "quart": "4",
        "5": "5", "cinc": "5", "cinquè": "5", "cinque": "5", "cinqué": "5",
        "6": "6", "sis": "6", "sisè": "6", "sise": "6", "sisé": "6",
    },
}

# Flat union. ``match`` stays deliberately language-AGNOSTIC: the Web Speech fallback and
# the manual page can feed either language's tokens, and tests/test_voice_topics.py
# exercises English and Catalan against the same menus. No word may mean two different
# keys across languages or this union silently drops one of them (pinned by
# test_no_number_word_means_two_different_topics_across_languages).
_NUMBER_WORDS: dict[str, str] = {
    word: key
    for words in _NUMBER_WORDS_BY_LANG.values()
    for word, key in words.items()
}

# Homophones of a number word, kept for ``match`` (Web Speech and the typed path really
# do return "to"/"won") but kept OUT of the Vosk grammar. In a grammar they tie with the
# number they sound like - "one" and "won" score 295.85 each - and Vosk's word
# confidence is the posterior split between them: every clean "one" scored exactly 0.500
# and every "two" 0.333 (measured on the robot across several sessions). That was
# never a short-word problem; without the homophones the same audio scores 1.0.
_GRAMMAR_HOMOPHONES_BY_LANG: dict[str, frozenset[str]] = {
    "en": frozenset({"won", "to", "too"}),
}

# Carrier phrases: things people really say at a menu that are NOT a choice. A
# constrained grammar forces every utterance onto SOME phrase in it, so without these
# "Option." / "Done." came back as option 1 and "I want to." as option 2 (measured with
# the robot's own model and voice). A carrier gives stray speech somewhere harmless to
# land: it matches no option, so the page re-prompts (bounded by maxTries). Never add a
# sound-alike of a number word here - "what"/"want" stole an accented "one", "the" stole
# "two". Catalan stays empty until its model has been probed the same way.
_GRAMMAR_CARRIERS_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": ("sorry", "pardon", "repeat", "again", "yes", "yeah", "okay", "wait", "go on",
           "done", "hmm", "i don't know", "please", "number", "option", "that", "which",
           "can you"),
    "ca": (),
}

# Tokeniser for ``match``: split on everything that is not a word character, and on the
# underscore too (so "option_3" still yields "option"/"3"). It must NOT be [^a-z0-9]+ --
# that cut "cinquè" into "cinqu", making every accented Catalan ordinal unmatchable.
_TOKEN_SPLIT = re.compile(r"[\W_]+")


def _lang_code(lang: str) -> str:
    """Normalise a language tag: ``ca-ES`` / ``ca_ES`` / ``CA`` all index as ``ca``."""
    return lang.split("-")[0].split("_")[0].lower()


def _canonical_numbers(lang: str) -> dict[str, str]:
    """``{"1": "one", "2": "two", ...}``: the first non-digit word listed per key."""

    canonical: dict[str, str] = {}
    for word, key in _NUMBER_WORDS_BY_LANG.get(_lang_code(str(lang or "")), {}).items():
        if not word.isdigit():
            canonical.setdefault(key, word)
    return canonical


def spoken_numbers(lang: str, n: int) -> list[str]:
    """The words a ``n``-option menu in ``lang`` invites: ``("ca", 3)`` -> u, dos, tres.

    The canonical form of each key, as :meth:`VoiceMenu.spoken_numbers` uses it. Stops
    at the last number ``lang`` has a word for; ``[]`` for a language with none.
    """

    canonical = _canonical_numbers(lang)
    words: list[str] = []
    for index in range(1, max(0, int(n)) + 1):
        word = canonical.get(str(index))
        if word is None:
            break
        words.append(word)
    return words


# --- validating a menu before it is written -------------------------------------------
# The menu number words stop at six (_NUMBER_WORDS_BY_LANG), so no menu offers more.
MAX_OPTIONS = 6
# What one option may be called: it is read aloud inside the prompt and shown on a button.
MAX_NAME_CHARS = 60
MAX_KEYWORD_CHARS = 60
# The prompt is one spoken sentence; the longest a six-option menu needs is far below.
MAX_PROMPT_CHARS = 600
# Characters no name or keyword may hold: menu copy is page DATA (the pages escape it as
# well), and none of these belongs in a spoken option.
_MARKUP_CHARS = frozenset('<>"`')
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _has_markup_or_control(text: str) -> bool:
    return any(ch in _MARKUP_CHARS or unicodedata.category(ch) in _REFUSED_CATEGORIES
               for ch in text)
# The id shape Pokamind's robot API uses for a module (the same rule as whoami's rows).
_MODULE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,1500}\Z")
_OPTION_KEY_RE = re.compile(r"[1-9]\Z")

# Stable slugs, in the order they are reported. Each names what the menu author must
# change; none of them carries a value.
MENU_PROBLEMS = (
    "menu_not_mapping",       # the menu is not a mapping at all
    "prompt_missing",         # no prompt text to speak
    "prompt_invalid",         # longer than MAX_PROMPT_CHARS, or holds < or >
    "options_empty",          # no option (or options is not a mapping)
    "too_many_options",       # more than MAX_OPTIONS
    "option_key_not_number",  # a key that is not "1".."6"
    "module_id_invalid",      # not ^[A-Za-z0-9_-]{1,1500}$ (or the option is not a mapping)
    "name_invalid",           # empty, a comma, markup or a control character, too long
    "keyword_invalid",        # not a list of strings; empty, markup, a control
                              # character, or too long
    "keyword_has_comma",      # /api/stt splits its grammar on commas
    "keyword_conflict",       # equal to, or inside, another option's keyword
    "default_not_an_option",  # default names no option
)


def normalised_keyword(keyword: str) -> str:
    """A keyword as :meth:`VoiceMenu.match` compares it: lower case, hyphens as spaces."""

    return " ".join(keyword.lower().replace("-", " ").split())


def menu_problems(data: Any, lang: str = "en") -> list[dict]:
    """Every problem with ``data`` as a menu, one row each: ``{code, option, keyword}``.

    ``option`` is the option key the problem is in (``None`` for the menu as a whole) and
    ``keyword`` the index of the keyword in that option's list (``None`` otherwise), so a
    caller can drop exactly the keyword at fault. :func:`validate_menu_dict` is the
    slug-only view of the same check.

    ``lang`` selects the number words: an option key must be a number ``lang`` can say
    (``1``..``6`` for ``ca`` and ``en``; a language with no words keeps ``1``..``6``).
    """

    problems: list[dict] = []

    def _add(code: str, option: Optional[str] = None, keyword: Optional[int] = None) -> None:
        problems.append({"code": code, "option": option, "keyword": keyword})

    if not isinstance(data, Mapping):
        _add("menu_not_mapping")
        return problems
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        _add("prompt_missing")
    elif len(prompt) > MAX_PROMPT_CHARS or "<" in prompt or ">" in prompt:
        _add("prompt_invalid")
    options = data.get("options")
    if not isinstance(options, Mapping) or not options:
        _add("options_empty")
        options = {}
    if len(options) > MAX_OPTIONS:
        _add("too_many_options")
    sayable = set(_canonical_numbers(lang)) or {str(n) for n in range(1, MAX_OPTIONS + 1)}

    keywords_by_option: list[tuple[str, int, str]] = []
    for key, option in options.items():
        label = key if isinstance(key, str) else None
        if (not isinstance(key, str) or _OPTION_KEY_RE.match(key) is None
                or int(key) > MAX_OPTIONS or key not in sayable):
            _add("option_key_not_number", label)
        option = option if isinstance(option, Mapping) else {}
        module_id = option.get("module_id")
        if not isinstance(module_id, str) or _MODULE_ID_RE.match(module_id) is None:
            _add("module_id_invalid", label)
        name = option.get("name")
        if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS
                or "," in name or _has_markup_or_control(name)):
            _add("name_invalid", label)
        keywords = option.get("keywords", [])
        if not isinstance(keywords, list):
            _add("keyword_invalid", label)
            continue
        for index, keyword in enumerate(keywords):
            if (not isinstance(keyword, str) or not normalised_keyword(keyword)
                    or len(keyword) > MAX_KEYWORD_CHARS
                    or _has_markup_or_control(keyword)):
                _add("keyword_invalid", label, index)
            elif "," in keyword:
                _add("keyword_has_comma", label, index)
            else:
                keywords_by_option.append((str(key), index, normalised_keyword(keyword)))

    # A keyword inside another option's keyword is one that option can steal: match()
    # tests each option's keywords as substrings of the utterance, in menu order.
    for key, index, keyword in keywords_by_option:
        if any(other_key != key and keyword in other
               for other_key, _, other in keywords_by_option):
            _add("keyword_conflict", key, index)

    default = data.get("default")
    if default is not None and (not isinstance(default, str) or default not in options):
        _add("default_not_an_option")
    return problems


def validate_menu_dict(data: Any, lang: str = "en") -> list[str]:
    """The problems with ``data`` as a spoken menu for ``lang``, as stable slugs.

    ``[]`` means the menu is valid, and then :meth:`VoiceMenu.model_validate` accepts it.
    Each slug of :data:`MENU_PROBLEMS` appears at most once, in that order. Unknown
    top-level keys (``generated_from``, ``authored``, ...) are not a problem.
    """

    found = {row["code"] for row in menu_problems(data, lang)}
    return [code for code in MENU_PROBLEMS if code in found]


class VoiceOption(BaseModel):
    # Any other key an option carries (a saved menu's own notes) is ignored.
    model_config = ConfigDict(extra="ignore")

    module_id: str
    name: str
    # Extra spoken triggers for this option (the module's own keywords), e.g.
    # ["difficult", "conversation"]. Number words are handled globally.
    keywords: list[str] = []


class VoiceMenu(BaseModel):
    # A menu file may carry keys the matcher has no use for: the menu the robot builds
    # from its Pokamind record writes ``generated_from`` and ``authored`` (which the admin
    # page and readycheck read off the file). Any unknown top-level key is ignored.
    model_config = ConfigDict(extra="ignore")

    prompt: str
    options: dict[str, VoiceOption]  # keyed by the spoken number, e.g. "1".."3"
    default: Optional[str] = None  # option key to fall back to when nothing matches

    @classmethod
    def from_yaml(cls, path: str | Path) -> "VoiceMenu":
        import yaml

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    @classmethod
    def default_menu(cls) -> "VoiceMenu":
        """Built-in menu of the demo modules (placeholder ids). Override via YAML."""
        return cls(
            prompt=(
                "Welcome. To choose your practice, say one for Difficult Conversations, "
                "two for Consultative Sales, or three for Motivation to Learn."
            ),
            options={
                "1": VoiceOption(module_id="demo-module-1",
                                 name="Difficult Conversations",
                                 keywords=["difficult", "conversation", "conversations"]),
                "2": VoiceOption(module_id="demo-module-2",
                                 name="Consultative Sales",
                                 keywords=["consultative", "sales technique", "selling"]),
                "3": VoiceOption(module_id="demo-module-3",
                                 name="Motivation to Learn",
                                 keywords=["motivation", "motivate", "learn", "learning"]),
            },
            default="1",
        )

    def match(self, text: Optional[str]) -> Optional[str]:
        """Map a recognised utterance to an option key, or None if nothing matches.

        Resolution order: an explicit number token ("two", "2", "number 2") wins; otherwise
        an option's own keyword ("difficult conversations"). Number words are only honoured
        for keys that actually exist in this menu.

        Number words naming DIFFERENT topics in one utterance return None. A constrained
        Vosk grammar force-aligns stray speech onto menu words, so "I do not want to" comes
        back as "one two" (measured on the robot); taking the first token would
        silently start topic 1. Nobody answering a six-way menu names two of them, so
        that shape is garbage rather than a choice. Repeating ONE topic is still a choice.
        """
        if not text:
            return None
        norm = text.strip().lower()
        # Tokenise on non-word characters so "number two." / "option-3" tokenise cleanly
        # while an accented Catalan ordinal ("cinquè") survives as one token.
        tokens = [t for t in _TOKEN_SPLIT.split(norm) if t]

        # 1) explicit number tokens (most reliable; what the menu actually asks for)
        spoken = [key for key in (_NUMBER_WORDS.get(tok) for tok in tokens)
                  if key and key in self.options]
        if len(set(spoken)) > 1:
            return None
        if spoken:
            return spoken[0]

        # 2) option keywords (lets someone say the topic name instead of a number).
        # Hyphens read as spaces on both sides: the grammar sends "team work", because a
        # Vosk lexicon has no "team-work", and the text comes back that way.
        spaced = norm.replace("-", " ")
        for key, opt in self.options.items():
            for kw in opt.keywords:
                if kw.lower().replace("-", " ") in spaced:
                    return key
        return None

    def grammar_phrases(self, lang: str = "en") -> list[str]:
        """Phrases for a CONSTRAINED Vosk grammar: everything this menu can legitimately
        hear, in ``lang``.

        The number words for ``lang`` whose key exists in this menu, then each option's own
        keywords. Bare digits are dropped (``"1"``/``"2"``/``"3"`` serve the on-screen
        button path at pages.py, and a Vosk lexicon holds word forms, not digits), as are
        phrases containing a comma (``/api/stt`` splits its ``grammar=`` on commas,
        speech_api.py). Lowercased, whitespace-normalised, insertion-ordered, deduped.

        An empty result means the caller must stay on OPEN recognition rather than send a
        grammar that can only match ``[unk]``.
        """
        phrases: list[str] = []
        seen: set[str] = set()

        def _add(raw: str) -> None:
            phrase = " ".join(str(raw).lower().replace("-", " ").split())
            if not phrase or "," in phrase or phrase.isdigit() or phrase in seen:
                return
            seen.add(phrase)
            phrases.append(phrase)

        code = _lang_code(lang)
        base = _NUMBER_WORDS_BY_LANG.get(code, {})
        tied = _GRAMMAR_HOMOPHONES_BY_LANG.get(code, frozenset())
        for word, key in base.items():
            if key in self.options and word not in tied:
                _add(word)
        for opt in self.options.values():
            for kw in opt.keywords:
                _add(kw)
        for carrier in _GRAMMAR_CARRIERS_BY_LANG.get(code, ()):
            _add(carrier)
        return phrases

    def spoken_numbers(self, lang: str = "en") -> list[str]:
        """The one word per option this menu should INVITE, in ``lang``, in menu order.

        The canonical form is the first non-digit spelling listed for that key in
        ``_NUMBER_WORDS_BY_LANG`` ("one"/"u", not "won"/"primer"). The kiosk's
        "Say one, two, or three" hint is rendered from this rather than written into the
        copy, so a six-option course cannot invite only three of its modules.

        Empty for a language with no number words: the caller then has no hint to render
        (the numbered buttons and the menu prompt itself still stand).
        """
        canonical = _canonical_numbers(lang)
        return [canonical[key] for key in self.options if key in canonical]

    def resolve(self, text: Optional[str]) -> tuple[Optional[str], Optional[VoiceOption]]:
        """``match`` then fall back to ``default``. Returns (key, option) or (None, None)."""
        key = self.match(text)
        if key is None and self.default and self.default in self.options:
            key = self.default
        if key is None:
            return None, None
        return key, self.options[key]
