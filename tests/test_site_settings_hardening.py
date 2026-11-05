"""The settings layer's edges: what a value may hold, and what the record may supply.

* A settings file is cut into lines where systemd cuts it (CR and LF only), and no
  value may hold a character that ends a line for anything else (``str.splitlines``
  separators, other controls, format characters): a saved value can never carry a
  second ``KEY=VALUE`` line past the checks of the key it was saved under.
* ``POKAMIND_BASE`` is an origin: no path.
* A display name holds no markup, whether typed on the admin page or in the record.
* ``EMORI_PATHWAYS`` is derived only when every record pathway can be listed.
* The boot language and the language offer pass over a language the record offers no
  module in.
"""

from __future__ import annotations

import pytest

from emori_adapter import robot_token, site_settings as ss
from emori_adapter.hostapp import lang_state, menu_builder

# Every character str.splitlines() treats as a line end, besides CR and LF.
_OTHER_LINE_ENDS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


@pytest.fixture
def token():
    return robot_token.generate("test-robot-01")


def _whoami(**overrides) -> dict:
    body = {
        "robot_id": "test-robot-01", "display_name": "Reception", "status": "active",
        "credential": {"kind": "fleet", "key_fingerprint": None, "key_version": 1},
        "org": "example-org", "partner": "emori-robot", "course_id": "course-a",
        "languages": ["ca", "en"], "pathways": ["Example pathway"],
        "identity_domains": ["example.org"], "modules": [],
        "features": {"feature_a": False, "feature_b": False, "feature_c": True},
    }
    body.update(overrides)
    return body


def _module(module_id: str, language, *, available=True, problem=None) -> dict:
    return {"module_id": module_id, "title": "Title " + module_id, "category": "Cat",
            "language": language, "available": available, "problem": problem}


def _write_snapshot(root, token, whoami) -> None:
    snap = ss.merge_scope_snapshot(None, whoami, contract=1,
                                   key_fingerprint=token.fingerprint, key_version=1,
                                   now=1_000.0)
    ss.write_scope_snapshot(snap, root=root)


# --------------------------------------------------------------------------- lines
@pytest.mark.parametrize("separator", _OTHER_LINE_ENDS)
def test_a_value_cannot_smuggle_a_second_setting_into_the_file(tmp_path, separator):
    smuggled = f"course{separator}POKAMIND_BASE=https://elsewhere.example"

    assert ss.validate("EMORI_COURSE_ID", smuggled) == "value_has_newline"
    with pytest.raises(ss.SettingsError) as refused:
        ss.save_settings({"EMORI_COURSE_ID": smuggled}, root=tmp_path)
    assert refused.value.code == "value_has_newline"
    assert not ss.site_path(ss.SETTINGS_FILE, tmp_path).exists()
    with pytest.raises(ss.SettingsError):
        ss.write_env_file(tmp_path / "x.env", {"EMORI_COURSE_ID": smuggled})


@pytest.mark.parametrize("separator", _OTHER_LINE_ENDS)
def test_a_file_is_read_in_the_lines_systemd_reads(tmp_path, separator):
    """A hand-edited file holding such a character: one line, one key -- and the
    value is refused at apply (never two keys, never the hidden one)."""

    path = ss.site_path(ss.SETTINGS_FILE, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(f"EMORI_COURSE_ID=c{separator}POKAMIND_BASE=https://elsewhere.example\n"
                     .encode("utf-8"))

    read = ss.read_env_file(path)
    assert list(read) == ["EMORI_COURSE_ID"]
    env: dict = {}
    sources = ss.apply(env, root=tmp_path)
    assert "POKAMIND_BASE" not in env and "POKAMIND_BASE" not in sources
    assert "EMORI_COURSE_ID" not in env


def test_cr_and_lf_still_end_a_line(tmp_path):
    path = tmp_path / "s.env"
    path.write_bytes(b"EMORI_COURSE_ID=a\r\nEMORI_ROBOT_NAME=B\rEMORI_LANG=ca\n")

    assert ss.read_env_file(path) == {"EMORI_COURSE_ID": "a", "EMORI_ROBOT_NAME": "B",
                                      "EMORI_LANG": "ca"}


@pytest.mark.parametrize("bad", ["a\tb", "a\u200eb", "a\u202eb", "a\x00b", "a\x7fb"])
def test_controls_and_format_characters_are_refused(bad):
    assert ss.validate("EMORI_SMTP_USER", bad) == "value_has_newline"


def test_ordinary_text_still_passes():
    assert ss.validate("EMORI_COURSE_ID", "Curs d'acollida — 2026 (ç, ñ, ü)") is None
    assert ss.validate("EMORI_ROBOT_NAME", "Recepció l'Hospital") is None


# --------------------------------------------------------------------------- origins
def test_an_origin_has_no_path():
    key = "POKAMIND_BASE"
    assert ss.validate(key, "https://api.example.org") is None
    assert ss.validate(key, "https://api.example.org/") is None
    assert ss.validate(key, "https://api.example.org:8443") is None
    assert ss.validate(key, "https://api.example.org/relay") == "value_not_origin"
    assert ss.validate(key, "https://api.example.org/a?b=c") == "value_not_https"
    assert ss.validate(key, "https://api.example.org:port") == "value_not_https"
    assert ss.validate(key, "http://api.example.org") == "value_not_https"
    assert ss.validate(key, "http://127.0.0.1:8095") is None


# --------------------------------------------------------------------------- the name
@pytest.mark.parametrize("name", ["</script><script>x()</script>", 'A "name"', "a`b`",
                                  "<b>"])
def test_a_robot_name_holds_no_markup(name):
    assert ss.validate("EMORI_ROBOT_NAME", name) == "value_has_markup"


def test_the_record_never_supplies_a_name_with_markup():
    derived = ss.derive_from_scope({"whoami": _whoami(
        display_name="</script><img src=x onerror=alert(1)>")})

    assert "EMORI_ROBOT_NAME" not in derived
    assert ss.derive_from_scope({"whoami": _whoami()})["EMORI_ROBOT_NAME"] == "Reception"


# --------------------------------------------------------------------------- pathways
def test_pathways_are_derived_only_when_every_one_can_be_listed():
    whole = ss.derive_from_scope({"whoami": _whoami(pathways=["A", "B"])})
    comma = ss.derive_from_scope({"whoami": _whoami(pathways=["A", "B, with a comma"])})
    blank = ss.derive_from_scope({"whoami": _whoami(pathways=["A", "", "  "])})

    assert whole["EMORI_PATHWAYS"] == "A,B"
    # An allowlist without the comma pathway would drop every badge carrying it: none.
    assert "EMORI_PATHWAYS" not in comma
    # An empty entry names no pathway a badge could carry: not a reason to give up.
    assert blank["EMORI_PATHWAYS"] == "A"


def test_a_star_says_no_allowlist_and_is_never_overwritten(tmp_path, token, monkeypatch):
    from emori_adapter.hostapp import identity

    _write_snapshot(tmp_path, token, _whoami(pathways=["A"]))
    env = {"POKAMIND_ROBOT_TOKEN": token.reveal(), "EMORI_PATHWAYS": "*"}

    sources = ss.apply(env, root=tmp_path)

    assert env["EMORI_PATHWAYS"] == "*" and sources["EMORI_PATHWAYS"] == "env"
    monkeypatch.setenv("EMORI_PATHWAYS", "*")
    assert identity.pathway_allowlist() == ()
    assert identity._roster_pathway("Anything at all") == "Anything at all"


# --------------------------------------------------------------------------- language
def test_the_boot_language_passes_over_a_language_without_modules():
    who = _whoami(languages=["en", "ca"], modules=[_module("M1", "ca"), _module("M2", "ca")])

    assert ss.scope_language(who) == "ca"
    assert ss.scope_language(dict(who, default_language="en")) == "ca"


def test_a_record_without_languages_boots_where_its_modules_are():
    who = _whoami(languages=[], modules=[_module("M1", "ca"), _module("M2", "ca"),
                                         _module("M3", "en")])

    assert ss.scope_language(who) == "ca"
    assert ss.scope_language(_whoami(languages=[], modules=[])) == ""


def test_with_modules_in_every_language_the_record_order_decides():
    who = _whoami(languages=["en", "ca"], modules=[_module("M1", "ca"), _module("M2", "en")])

    assert ss.scope_language(who) == "en"
    assert ss.scope_language(dict(who, default_language="ca")) == "ca"


def test_when_no_listed_language_has_a_module_the_record_still_decides():
    who = _whoami(languages=["en"], modules=[_module("M1", "ca")])

    assert ss.scope_language(who) == "en"


def test_a_module_without_a_language_means_nothing_can_be_told():
    who = _whoami(languages=["en", "ca"], modules=[_module("M1", "ca"), _module("M2", None)])

    assert ss.offered_module_languages(who) == {}
    assert ss.scope_language(who) == "en"
    assert lang_state.scope_languages({"whoami": who}) == ("en", "ca")


def test_the_language_offer_drops_a_language_without_modules():
    who = _whoami(languages=["en", "ca"], modules=[_module("M1", "ca"),
                                                   _module("M2", "en", available=False)])

    assert lang_state.scope_languages({"whoami": who}) == ("ca",)
    # Never down to nothing.
    none = _whoami(languages=["en"], modules=[_module("M1", "ca")])
    assert lang_state.scope_languages({"whoami": none}) == ("en",)


def test_offered_modules_follow_the_menu_builders_rule():
    """site_settings cannot import the hostapp: its count must be the builder's."""

    modules = [_module("AAAA", "ca"), _module("BBBB", "ca-ES"), _module("CCCC", "en_GB"),
               _module("DDDD", "en", available=False), _module("EEEE", "en",
                                                               problem="not_found"),
               _module("FFFF", "es")]
    who = _whoami(modules=modules)

    drafts = menu_builder.build_drafts(who)
    counted = ss.offered_module_languages(who)

    assert counted == {lang: len(d["order"]) for lang, d in drafts.items() if d["order"]}
