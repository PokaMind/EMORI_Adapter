"""The spoken topic menu, built from this robot's Pokamind record.

Every verified whoami (the background sync, the admin page's Verify and Replace token,
``token_verify --write-scope`` at install) leaves a scope snapshot behind
(:mod:`emori_adapter.site_settings`). :func:`rebuild_auto` turns its ``modules`` into
one menu file per language under ``configs/site/auto/voice_topics.<lang>.yaml``, which
the hostapp serves when nothing outranks it (``EMORI_VOICE_TOPICS_<LANG>``, a menu saved
on the phone admin page) and ``EMORI_SITE_AUTO`` is not ``0``. Rebuilt on every sync;
the hostapp clears its menu cache, so a change is spoken without a restart.

Per language (the kiosk languages, ``ca`` and ``en``) a menu offers the record's modules
with ``available`` true, no ``problem`` and a language whose base code (lower case, cut
at ``-`` or ``_``) is that language, in record order, at most six (the number words stop
at six). The options are numbered one up in menu order: the modules already on the
menu first, in their previous order (``menu_order``, kept in the snapshot), then new
ones in record order. When a module leaves the scope, every later option moves up a
number: the robot speaks the whole menu every time, so nobody has to remember one.

Each option's copy comes from the record when it carries it (``menu_name``,
``menu_keywords``); otherwise the robot drafts it. A drafted name is the module title cut
at its first ``,`` ``:`` ``;`` ``(`` or `` - `` (a comma would be dropped from the
recognition grammar anyway), at most 40 characters; drafted keywords are the name and its
longer words, with accent-free twins for a recogniser built without diacritics. A
keyword one option shares with another (or that sits inside another's) is dropped from
both: :meth:`VoiceMenu.match` takes the first option whose keyword it hears. The file
records which it is (``authored``: ``pokamind``, ``title_draft`` or ``mixed``): readycheck
warns about a draft, and the admin page offers to save a curated copy.

Pure functions, plus one atomic file write per language. Nothing here logs a module id,
a title or a name: counts and codes only.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import yaml

from emori_adapter import site_settings
from emori_adapter.hostapp import voice_topics

log = logging.getLogger("emori.hostapp.menu_builder")

MAX_OPTIONS = voice_topics.MAX_OPTIONS
MAX_DRAFT_NAME_CHARS = 40
MAX_KEYWORDS = 12
LANGUAGES = site_settings.SUPPORTED_LANGS
MENU_FILE = "voice_topics.{lang}.yaml"
MENU_FILE_MODE = 0o600

GENERATED_FROM = "pokamind_whoami"
AUTHORED_POKAMIND = "pokamind"      # every option's name came from the record
AUTHORED_DRAFT = "title_draft"      # every option's name was drafted from its title
AUTHORED_MIXED = "mixed"

# Why a module of the record is not on a language's menu. A module's own ``problem``
# (not_found, not_visible_to_robot, language_mismatch) is reported as itself.
SKIP_NO_LANGUAGE = "no_language"
SKIP_UNAVAILABLE = "unavailable"
SKIP_OVER_LIMIT = "over_menu_limit"
SKIP_DUPLICATE = "duplicate"
SKIP_ID_INVALID = "module_id_invalid"
_PROBLEM_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

# The prompt is read aloud: an opening, one "<number> for <name>" per option, and the
# last one joined with "or". The number words are voice_topics' canonical ones.
_PROMPTS: dict[str, tuple[str, str, str]] = {
    "en": ("Welcome. To choose your practice, say ", "{number} for {name}", ", or "),
    "ca": ("Hola. Per triar la teva pràctica, digues ", "{number} per {name}", ", o "),
}
_FALLBACK_NAMES = {"en": "Option {n}", "ca": "Opció {n}"}

# Words that say nothing about which module a person means. Only words of four or more
# letters can become a keyword, so only those are listed.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "about", "after", "also", "among", "before", "being", "between", "both", "does",
        "each", "even", "from", "have", "here", "into", "just", "like", "more", "most",
        "other", "over", "same", "some", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "under", "very", "what",
        "when", "where", "which", "while", "with", "within", "without", "your", "yours",
        # menu words: the prompt itself says them
        "practice", "module", "modules", "course", "session", "training", "part",
        "option", "number", "topic", "level", "unit", "lesson", "introduction",
    }),
    "ca": frozenset({
        "aquest", "aquesta", "aquests", "aquestes", "aquell", "aquella", "aquells",
        "aquelles", "altre", "altra", "altres", "cada", "contra", "dels", "dins", "durant",
        "entre", "fins", "fora", "molt", "molta", "molts", "moltes", "nostre", "nostra",
        "pels", "perquè", "perque", "quan", "quin", "quina", "quins", "quines", "segons",
        "sense", "seva", "seves", "seus", "sobre", "també", "tambe", "teva", "teves",
        "teus", "tota", "tots", "totes", "unes", "vostre", "vostra",
        # menu words: the prompt itself says them
        "pràctica", "practica", "mòdul", "modul", "mòduls", "moduls", "curs", "sessió",
        "sessio", "formació", "formacio", "part", "opció", "opcio", "número", "numero",
        "tema", "nivell", "unitat", "lliçó", "llico", "introducció", "introduccio",
    }),
}
# Words a cut name must not end on ("Habits and", "Hàbits i").
_TRAILING: dict[str, frozenset[str]] = {
    "en": frozenset({"a", "an", "and", "at", "by", "for", "from", "in", "into", "of",
                     "on", "or", "the", "to", "with"}),
    "ca": frozenset({"a", "al", "als", "amb", "de", "del", "dels", "el", "els", "en",
                     "i", "la", "les", "o", "per", "pel", "pels", "un", "una", "d'",
                     "l'"}),
}

_NUMERIC_PREFIX = re.compile(r"^\s*\d+\s*-\s*")
_NAME_CUT = re.compile(r"[,:;(]| [-–—] ")
_NAME_EDGE = " \t.!?¡¿\"'–—-/"
_WORD_SPLIT = re.compile(r"[^\w·]+")

# What a title loses before a name is drafted from it, and what a record's own copy may
# not hold: menu copy is page data, and markup or a control character in it is never
# meant to be spoken (voice_topics.menu_problems refuses the same).
_MARKUP_CHARS = frozenset('<>"`')
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _unsafe(ch: str) -> bool:
    return ch in _MARKUP_CHARS or unicodedata.category(ch) in _REFUSED_CATEGORIES


def _has_unsafe(text: str) -> bool:
    return any(_unsafe(ch) for ch in text)


def _without_unsafe(text: str) -> str:
    """``text`` with markup removed and every control/format character as a space."""

    return "".join(" " if unicodedata.category(ch) in _REFUSED_CATEGORIES
                   else "" if ch in _MARKUP_CHARS else ch for ch in text)


_REBUILD_LOCK = threading.Lock()


# --------------------------------------------------------------------------- copy
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _fold(text: str) -> str:
    """``"accés"`` -> ``"acces"``: the twin a lexicon without diacritics emits."""

    decomposed = unicodedata.normalize("NFKD", text)
    return _nfc("".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def _lang(code: str) -> str:
    return str(code or "").strip().lower().replace("_", "-").split("-", 1)[0]


def _valid_menu_name(value: Any) -> Optional[str]:
    """A record's ``menu_name`` as the menu will use it, or None when it cannot be one."""

    if not isinstance(value, str) or "," in value or _has_unsafe(value):
        return None
    name = " ".join(_nfc(value).split())
    if not name or len(name) > voice_topics.MAX_NAME_CHARS:
        return None
    return name


def draft_name(title: Any, *, lang: str, number: int) -> str:
    """The name a module without a ``menu_name`` is offered under.

    The title without a leading ``"3-"``, cut at its first ``,`` ``:`` ``;`` ``(`` or
    `` - ``, at most :data:`MAX_DRAFT_NAME_CHARS` characters at a word boundary, never
    ending on a word like "and"/"i". ``"Option N"`` / ``"Opció N"`` when nothing is left.
    """

    text = _without_unsafe(_nfc(title)) if isinstance(title, str) else ""
    text = _NUMERIC_PREFIX.sub("", text, count=1)
    cut = _NAME_CUT.search(text)
    if cut is not None:
        text = text[:cut.start()]
    words = " ".join(text.replace("\r", " ").replace("\n", " ").split()).strip(
        _NAME_EDGE).split()
    kept: list[str] = []
    for word in words:
        if len(" ".join(kept + [word])) > MAX_DRAFT_NAME_CHARS:
            break
        kept.append(word)
    if not kept and words:  # one word longer than the limit: cut inside it
        kept = [words[0][:MAX_DRAFT_NAME_CHARS]]
    trailing = _TRAILING.get(_lang(lang), frozenset())
    while len(kept) > 1 and kept[-1].lower() in trailing:
        kept.pop()
    name = " ".join(kept).strip(_NAME_EDGE)
    if not any(ch.isalnum() for ch in name):
        template = _FALLBACK_NAMES.get(_lang(lang), _FALLBACK_NAMES["en"])
        return template.format(n=number)
    return name


def _usable_keyword(keyword: str) -> bool:
    """A keyword :meth:`VoiceMenu.match` and the recognition grammar can both use."""

    normalised = voice_topics.normalised_keyword(keyword)
    return (bool(normalised) and len(keyword) <= voice_topics.MAX_KEYWORD_CHARS
            and "," not in keyword and not _has_unsafe(keyword))


def _carriers(lang: str) -> frozenset[str]:
    return frozenset(voice_topics._GRAMMAR_CARRIERS_BY_LANG.get(_lang(lang), ()))


def _says_a_number(keyword: str) -> bool:
    """A keyword holding a number word ("to", "first", "una") selects THAT number in
    :meth:`VoiceMenu.match`, whichever option the keyword belongs to."""

    tokens = voice_topics._TOKEN_SPLIT.split(keyword.lower())
    return any(token in voice_topics._NUMBER_WORDS for token in tokens if token)


def draft_keywords(name: str, *, lang: str) -> list[str]:
    """Keywords for an option whose record carries none: the whole name (lower case),
    then its words of four or more letters that are not filler, each followed by its
    accent-free twin. Nothing that says a number or a carrier phrase; at most
    :data:`MAX_KEYWORDS`."""

    code = _lang(lang)
    stop = _STOPWORDS.get(code, frozenset()) | {
        word for carrier in _carriers(code) for word in carrier.split()}
    carriers = _carriers(code)
    lowered = " ".join(_nfc(name).lower().split())
    candidates = [lowered]
    for word in _WORD_SPLIT.split(lowered):
        letters = sum(ch.isalpha() for ch in word)
        if (letters >= 4 and not any(ch.isdigit() for ch in word) and "_" not in word
                and word not in stop):
            candidates.append(word)
    keywords: list[str] = []
    for candidate in candidates:
        if (not _usable_keyword(candidate) or _says_a_number(candidate)
                or voice_topics.normalised_keyword(candidate) in carriers):
            continue
        for form in (candidate, _fold(candidate)):
            if form not in keywords:
                keywords.append(form)
    return keywords[:MAX_KEYWORDS]


def _record_keywords(value: Any) -> Optional[list[str]]:
    """A record's ``menu_keywords``, lower-cased, or None when it carries none.

    An empty list is the record saying "no keywords" and is kept; a list of which no
    entry is usable counts as absent (the keywords are then drafted from the name).
    """

    if not isinstance(value, (list, tuple)):
        return None
    keywords: list[str] = []
    for item in value:
        if not isinstance(item, str) or "\r" in item or "\n" in item:
            continue
        keyword = " ".join(_nfc(item).lower().split())
        if _usable_keyword(keyword) and keyword not in keywords:
            keywords.append(keyword)
    if value and not keywords:
        return None
    return keywords[:MAX_KEYWORDS]


def _drop_shared_keywords(options: Mapping[str, dict]) -> None:
    """Remove every keyword equal to, or inside, ANOTHER option's keyword (in place)."""

    every = [(key, voice_topics.normalised_keyword(keyword))
             for key, option in options.items() for keyword in option["keywords"]]
    for key, option in options.items():
        option["keywords"] = [
            keyword for keyword in option["keywords"]
            if not any(other_key != key and voice_topics.normalised_keyword(keyword) in other
                       for other_key, other in every)
        ]


def build_prompt(names: list[str], lang: str) -> str:
    """``"Welcome. To choose your practice, say one for A, two for B, or three for C."``"""

    code = _lang(lang)
    opening, part, last_join = _PROMPTS.get(code, _PROMPTS["en"])
    numbers = voice_topics.spoken_numbers(code if code in _PROMPTS else "en", len(names))
    parts = [part.format(number=number, name=name) for number, name in zip(numbers, names)]
    if len(parts) > 1:
        return opening + ", ".join(parts[:-1]) + last_join + parts[-1] + "."
    return opening + "".join(parts) + "."


# --------------------------------------------------------------------------- selection
def _modules(whoami: Any) -> list[Mapping[str, Any]]:
    if not isinstance(whoami, Mapping) and hasattr(whoami, "to_dict"):
        whoami = whoami.to_dict()
    modules = whoami.get("modules") if isinstance(whoami, Mapping) else None
    if not isinstance(modules, (list, tuple)):
        return []
    return [module for module in modules if isinstance(module, Mapping)]


def _previous(previous_order: Any, lang: str) -> list[str]:
    if not isinstance(previous_order, Mapping):
        return []
    ids = previous_order.get(lang)
    if not isinstance(ids, (list, tuple)):
        return []
    return [module_id for module_id in ids if isinstance(module_id, str)]


def _plan(modules: list[Mapping[str, Any]], lang: str,
          previous: list[str]) -> tuple[list[Mapping[str, Any]], list[dict], list[dict]]:
    """(offered modules in menu order, skipped rows, one row per module of ``lang``).

    A module belongs to ``lang``'s rows when its language base code is ``lang`` or it
    has no usable language (it can be on no menu, and every language should say why).
    """

    eligible: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    rows: list[dict] = []
    for module in modules:
        module_id = module.get("module_id")
        language = module.get("language")
        code = _lang(language) if isinstance(language, str) else ""
        if code and code != lang:
            continue
        problem = module.get("problem")
        if not isinstance(module_id, str) or voice_topics._MODULE_ID_RE.match(
                module_id) is None:
            reason: Optional[str] = SKIP_ID_INVALID
        elif problem is not None:
            reason = problem if isinstance(problem, str) and _PROBLEM_RE.match(
                problem) else SKIP_UNAVAILABLE
        elif module.get("available") is not True:
            reason = SKIP_UNAVAILABLE
        elif not code:
            reason = SKIP_NO_LANGUAGE
        elif module_id in seen:
            reason = SKIP_DUPLICATE
        else:
            reason = None
            seen.add(module_id)
            eligible.append(module)
        rows.append({"module": module, "reason": reason})

    by_id = {m["module_id"]: m for m in eligible}
    order: list[str] = []
    for module_id in previous:
        if module_id in by_id and module_id not in order:
            order.append(module_id)
    order += [m["module_id"] for m in eligible if m["module_id"] not in order]
    offered_ids = order[:MAX_OPTIONS]
    for row in rows:
        module_id = row["module"].get("module_id")
        if row["reason"] is None and module_id not in offered_ids:
            row["reason"] = SKIP_OVER_LIMIT
    skipped = [{"module_id": row["module"].get("module_id"), "reason": row["reason"]}
               for row in rows if row["reason"] is not None]
    # over-limit rows listed in menu order, after the rest, like the order itself
    skipped.sort(key=lambda s: (s["reason"] == SKIP_OVER_LIMIT,
                                order.index(s["module_id"])
                                if s["reason"] == SKIP_OVER_LIMIT else 0))
    for row in rows:
        module_id = row["module"].get("module_id")
        row["number"] = (str(offered_ids.index(module_id) + 1)
                         if row["reason"] is None else None)
    return [by_id[i] for i in offered_ids], skipped, rows


def _menu(offered: list[Mapping[str, Any]], lang: str) -> Optional[dict]:
    if not offered:
        return None
    options: dict[str, dict] = {}
    authored = 0
    for number, module in enumerate(offered, start=1):
        name = _valid_menu_name(module.get("menu_name"))
        if name is not None:
            authored += 1
        else:
            name = draft_name(module.get("title"), lang=lang, number=number)
        keywords = _record_keywords(module.get("menu_keywords"))
        if keywords is None:
            keywords = draft_keywords(name, lang=lang)
        options[str(number)] = {"module_id": module["module_id"], "name": name,
                                "keywords": keywords}
    _drop_shared_keywords(options)
    if authored == len(offered):
        label = AUTHORED_POKAMIND
    elif authored == 0:
        label = AUTHORED_DRAFT
    else:
        label = AUTHORED_MIXED
    return {
        "prompt": build_prompt([o["name"] for o in options.values()], lang),
        "options": options,
        "default": "1",
        "generated_from": GENERATED_FROM,
        "authored": label,
    }


def build_drafts(whoami: Any, *, previous_order: Optional[Mapping[str, list[str]]] = None,
                 languages: Iterable[str] = LANGUAGES) -> dict[str, dict]:
    """One draft per language: ``{lang: {"menu", "order", "skipped"}}``.

    ``menu`` is the menu mapping (``prompt``, ``options`` ``{"1": {module_id, name,
    keywords}}``, ``default`` ``"1"``, ``generated_from`` ``"pokamind_whoami"``,
    ``authored``), or None when no module is eligible for that language; ``order`` the
    offered module ids in number order; ``skipped`` ``[{module_id, reason}]`` for the
    language's modules that are not offered (reasons: the module's own ``problem``,
    ``unavailable``, ``no_language``, ``duplicate``, ``module_id_invalid``,
    ``over_menu_limit``). ``whoami`` is a whoami body as a mapping (``Whoami.to_dict()``).
    A language without a prompt template (only ``ca`` and ``en`` have one) is left out.
    """

    modules = _modules(whoami)
    drafts: dict[str, dict] = {}
    for raw in languages:
        lang = _lang(raw)
        if lang not in _PROMPTS or lang in drafts:
            continue
        offered, skipped, _ = _plan(modules, lang, _previous(previous_order, lang))
        drafts[lang] = {"menu": _menu(offered, lang),
                        "order": [m["module_id"] for m in offered],
                        "skipped": skipped}
    return drafts


def module_rows(whoami: Any, lang: str, *,
                previous_order: Optional[Mapping[str, list[str]]] = None) -> list[dict]:
    """The record's modules as one language's menu sees them, in record order.

    Each row: ``{module_id, title, language, available, problem, eligible, number,
    reason}`` -- ``number`` the option it is offered as (``"1"``..``"6"``) or None, and
    ``reason`` why not (the slugs of :func:`build_drafts`). Rows are the modules in
    ``lang`` plus those with no usable language. For the admin page's Menu tab.
    """

    code = _lang(lang)
    _, _, rows = _plan(_modules(whoami), code, _previous(previous_order, code))
    out: list[dict] = []
    for row in rows:
        module = row["module"]
        out.append({
            "module_id": module.get("module_id"),
            "title": module.get("title"),
            "language": module.get("language"),
            "available": module.get("available") is True,
            "problem": module.get("problem"),
            "eligible": row["reason"] is None,
            "number": row["number"],
            "reason": row["reason"],
        })
    return out


# --------------------------------------------------------------------------- the files
def auto_menu_dir(out_dir: Optional[os.PathLike | str] = None) -> Path:
    """Where the built menus live: ``out_dir`` (a relative one against the install
    root), else ``configs/site/auto``."""

    return site_settings.site_path(str(out_dir if out_dir is not None
                                       else site_settings.AUTO_MENU_DIR))


def auto_menu_path(lang: str, out_dir: Optional[os.PathLike | str] = None) -> Path:
    """``configs/site/auto/voice_topics.<lang>.yaml`` in this install."""

    return auto_menu_dir(out_dir) / MENU_FILE.format(lang=_lang(lang))


def render_menu(menu: Mapping[str, Any], lang: str) -> str:
    """The YAML text of a built menu, with a header saying where it came from."""

    header = (
        "# The spoken topic menu this robot built from its Pokamind record.\n"
        "# Rebuilt on every verified sync, so an edit here does not last: save a menu on\n"
        f"# the phone admin page instead (configs/site/voice_topics.{_lang(lang)}.yaml).\n"
    )
    body = yaml.safe_dump(dict(menu), allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=88)
    return header + body


def _chmod(path: os.PathLike | str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # Windows: only the read-only bit
        pass


def atomic_write_text(target: os.PathLike | str, text: str) -> None:
    """``text`` into ``target``: same-directory temp file, fsync, ``os.replace``, 0600.

    A reader sees the old file or the new one, never half of one. Also what the admin
    page's menu save uses for ``configs/site/voice_topics.<lang>.yaml``. Raises OSError.
    """

    target = Path(target)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp",
                                     dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod(temporary, MENU_FILE_MODE)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _chmod(target, MENU_FILE_MODE)
    if os.name == "posix":  # the rename itself must survive a power cut too
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


def _cleaned(menu: dict, lang: str) -> dict:
    """``menu`` without any keyword :func:`voice_topics.menu_problems` objects to.

    The builder never produces one; this is the rule that a bad keyword costs that
    keyword and never the option.
    """

    for _ in range(2):
        bad = {(row["option"], row["keyword"])
               for row in voice_topics.menu_problems(menu, lang)
               if row["keyword"] is not None}
        if not bad:
            break
        for key, option in menu["options"].items():
            option["keywords"] = [keyword for index, keyword in enumerate(option["keywords"])
                                  if (key, index) not in bad]
    return menu


def _write_language(directory: Path, lang: str, draft: Mapping[str, Any]) -> bool:
    """Write (or remove) ``lang``'s file. True when the file now matches the draft."""

    target = directory / MENU_FILE.format(lang=lang)
    menu = draft["menu"]
    if menu is None:
        try:
            target.unlink()
            log.info("auto menu %s: removed (no eligible module, %d skipped)",
                     lang, len(draft["skipped"]))
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("auto menu %s: could not be removed (%s)", lang, type(exc).__name__)
            return False
        return True
    menu = _cleaned(menu, lang)
    problems = voice_topics.validate_menu_dict(menu, lang)
    if problems:
        log.warning("auto menu %s: not written (%s)", lang, ",".join(problems))
        return False
    try:
        text = render_menu(menu, lang)
        # Exactly what the hostapp will do with the file.
        voice_topics.VoiceMenu.model_validate(yaml.safe_load(text))
    except Exception as exc:  # noqa: BLE001 - never a file the hostapp cannot read
        log.warning("auto menu %s: not written (menu_unloadable: %s)", lang,
                    type(exc).__name__)
        return False
    try:
        if target.read_text(encoding="utf-8") == text:
            return True
    except (OSError, UnicodeDecodeError):
        pass
    try:
        atomic_write_text(target, text)
    except OSError as exc:
        log.warning("auto menu %s: could not be written (%s)", lang, type(exc).__name__)
        return False
    log.info("auto menu %s: %d option(s), authored=%s, %d skipped", lang,
             len(menu["options"]), menu["authored"], len(draft["skipped"]))
    return True


def rebuild_auto(snapshot: Mapping[str, Any],
                 out_dir: Optional[os.PathLike | str] = None) -> dict[str, list[str]]:
    """Rebuild ``configs/site/auto/voice_topics.<lang>.yaml`` from a scope snapshot.

    Builds :func:`build_drafts` from ``snapshot["whoami"]`` and
    ``snapshot.get("menu_order")``, checks each menu with
    :func:`voice_topics.validate_menu_dict` (a bad keyword is dropped, never the option),
    and writes each language's file atomically (0600; an unchanged file is not
    rewritten). A language with no eligible module has its file removed. ``out_dir``
    defaults to ``configs/site/auto``; a relative one is resolved against the install
    root.

    Returns the new ``menu_order``: ``{lang: [module_id, ...]}`` for ``ca`` and ``en``
    (``[]`` when a language offers nothing). A language whose file could not be written
    keeps its previous order, which still describes the file on disk. Never raises:
    ``{}`` when the snapshot is unusable or anything unexpected fails.
    """

    try:
        with _REBUILD_LOCK:
            if not isinstance(snapshot, Mapping) or not isinstance(
                    snapshot.get("whoami"), Mapping):
                log.warning("auto menu: not rebuilt (snapshot_unusable)")
                return {}
            previous = snapshot.get("menu_order")
            drafts = build_drafts(snapshot["whoami"], previous_order=previous)
            directory = auto_menu_dir(out_dir)
            orders: dict[str, list[str]] = {}
            for lang, draft in drafts.items():
                if _write_language(directory, lang, draft):
                    orders[lang] = list(draft["order"])
                else:
                    orders[lang] = _previous(previous, lang)
            return orders
    except Exception as exc:  # noqa: BLE001 - a sync must never die on its menu
        log.warning("auto menu: rebuild failed (%s)", type(exc).__name__)
        return {}
