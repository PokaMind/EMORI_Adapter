"""A voice-menu advisory a fresh robot needs to see, WARN (never a changed exit code):

* ``voice_menu_modules_not_offered`` -- the menu built from the record leaves modules
  out (more than six in the kiosk language, or modules with no language). Counts only,
  never an id; only for the menu built from the record.
"""

from __future__ import annotations

import dataclasses

from emori_adapter.hostapp.readycheck import PASS, WARN, MenuProbe, evaluate
from tests.test_readycheck import (
    _MENU_OPTIONS,
    _by_name,
    _healthy,
    whoami,
    whoami_module,
)


def _gate(evidence, name):
    return _by_name(evaluate(evidence))[name]


# --- voice_menu_modules_not_offered ------------------------------------------------------

_AUTO = MenuProbe(True, "", _MENU_OPTIONS, source="configs/site/auto/voice_topics.ca.yaml",
                  lang="ca", digest="sha256:a7", tier="auto", authored="pokamind")


def _record(extra: int = 0, no_language: int = 0):
    modules = [whoami_module(module_id) for _key, module_id in _MENU_OPTIONS]
    modules += [whoami_module(f"extra-module-{n:02d}xxxxxxxxx") for n in range(extra)]
    modules += [dataclasses.replace(whoami_module(f"nolang-module-{n:02d}xxxxxxxx"),
                                    language=None) for n in range(no_language)]
    return whoami(modules=tuple(modules))


def test_a_record_with_more_modules_than_numbers_warns_with_counts_only():
    over = 7 - len(_MENU_OPTIONS)
    evidence = _healthy(menu=_AUTO, lang="ca", whoami=_record(extra=over, no_language=1))

    gate = _gate(evidence, "voice_menu_in_scope")

    assert (gate.status, gate.code) == (WARN, "voice_menu_modules_not_offered")
    assert "1 module(s) past the sixth in 'ca'" in gate.hint
    assert "1 module(s) with no language" in gate.hint
    assert "extra-module" not in gate.hint and "nolang-module" not in gate.hint


def test_a_record_the_menu_offers_whole_passes():
    gate = _gate(_healthy(menu=_AUTO, lang="ca", whoami=_record()), "voice_menu_in_scope")

    assert (gate.status, gate.code) == (PASS, "menu_in_scope")


def test_a_menu_saved_on_the_robot_is_somebodys_choice():
    site = dataclasses.replace(_AUTO, tier="site",
                               source="configs/site/voice_topics.ca.yaml")
    evidence = _healthy(menu=site, lang="ca", whoami=_record(extra=7))

    assert _gate(evidence, "voice_menu_in_scope").code == "menu_in_scope"
