"""Where the robot's spoken menu comes from, and why a deploy cannot change it.

The invariant: a tracked development menu must never overwrite the menu a robot serves.

The repository ships menus under ``configs/`` (``configs/voice_topics.yaml`` is the
simulator's development menu, whose module ids are placeholders). Every
file-level deploy -- ``git archive``, ``sudo tar -x`` of the repo over /opt/emori --
rewrites those files. If the runtime read a path the REPOSITORY also ships ahead of the
robot's own menu, every such deploy would silently replace it: against Pokamind every
session would die at ``init_session`` with ``Module ... not found``.

So the tiers, highest first (``server._menu_tiers``):

* ``env``  -- ``EMORI_VOICE_TOPICS_<LANG>`` / ``EMORI_VOICE_TOPICS``;
* ``site`` -- ``configs/site/voice_topics.<lang>.yaml`` under the install root, saved on
  the admin page (never tracked: no deploy carries one);
* ``prod`` -- ``configs/prod/``, a menu a fork of this repository ships;
* ``auto`` -- ``configs/site/auto/``, built from THIS robot's Pokamind record (read only
  with ``EMORI_SITE_AUTO`` on and a scope snapshot of the token in force);
* ``dev``  -- the tracked menus; then the built-in one.

These tests run in a temporary CWD that is a faithful miniature of /opt/emori (the
install root is pointed there too), so the "deploy" is a real file write rather than a
mocked one.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from emori_adapter import robot_token, site_settings
from emori_adapter.hostapp import server
from emori_adapter.hostapp.server import (
    _load_menu,
    _menu_source,
    menu_override_env,
    menu_source,
    prod_menu_source,
)

PROD_EN = "example-en-module-1"  # the installed English menu's module
PROD_CA = "example-ca-module-1"  # the installed Catalan menu's module
SITE_EN = "SiteSavedEnglish0001"  # the menu saved on the admin page
AUTO_EN = "BuiltFromTheRecord01"  # the menu built from the Pokamind record
DEV_ID = "dev-en-module-1"  # the tracked development menu's module: dead against Pokamind
GOLDEN_TOKEN = "pkr1.test-robot-01.AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _menu(path: Path, module_id: str, *, header: str = "installed menu") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {header}\n"
        "prompt: say one\n"
        "options:\n"
        "  '1':\n"
        f"    module_id: {module_id}\n"
        "    name: Topic one\n",
        encoding="utf-8",
    )


@pytest.fixture()
def rig(tmp_path, monkeypatch) -> Path:
    """A /opt/emori miniature: the installed prod menus, plus the repo's tracked ones."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    for name in ("EMORI_VOICE_TOPICS", "EMORI_VOICE_TOPICS_EN", "EMORI_VOICE_TOPICS_CA",
                 "EMORI_SITE_AUTO"):
        monkeypatch.delenv(name, raising=False)
    _menu(tmp_path / "configs/prod/voice_topics.yaml", PROD_EN)
    _menu(tmp_path / "configs/prod/voice_topics.ca.yaml", PROD_CA)
    # What a `git archive` / `tar -x` of the repository puts on the robot.
    _menu(tmp_path / "configs/voice_topics.yaml", DEV_ID, header="development menu")
    _menu(tmp_path / "configs/voice_topics.ca.yaml", "AnOlderCatalanId01")
    _menu(tmp_path / "configs/voice_topics.example.yaml", "EXAMPLE00000000001")
    return tmp_path


def _deploy_tarball_over(root: Path) -> None:
    """Exactly what `sudo tar -x` of the repo does: rewrite every tracked config."""

    _menu(root / "configs/voice_topics.yaml", DEV_ID, header="development menu")
    _menu(root / "configs/voice_topics.ca.yaml", "AnOlderCatalanId01")


def _with_token_and_scope(monkeypatch, root: Path, *, robot_id: str = "test-robot-01"):
    """This process runs with the golden token, and a scope snapshot of ``robot_id``."""

    token = robot_token.parse_token(GOLDEN_TOKEN)
    monkeypatch.setattr(server, "ROBOT_TOKEN", token)
    whoami = {"robot_id": robot_id, "display_name": "Example Site", "status": "active",
              "credential": {"kind": "fleet", "key_fingerprint": None, "key_version": 1},
              "org": "example-org", "partner": "emori-robot", "course_id": "",
              "languages": ["ca", "en"], "pathways": [], "identity_domains": [],
              "modules": [], "features": {"feature_a": False, "feature_b": False,
                                          "feature_c": True}}
    snapshot = site_settings.merge_scope_snapshot(
        None, whoami, contract=1,
        key_fingerprint=token.fingerprint if robot_id == token.robot_id else None,
        key_version=1, now=time.time())
    site_settings.write_scope_snapshot(snapshot, root=root)
    return token


def test_the_installed_production_menu_outranks_the_repositorys_own_copy(rig):
    assert _menu_source("en") == "configs/prod/voice_topics.yaml"
    assert _menu_source("ca") == "configs/prod/voice_topics.ca.yaml"
    assert _load_menu("en").options["1"].module_id == PROD_EN
    assert _load_menu("ca").options["1"].module_id == PROD_CA
    assert (menu_source("en"), menu_source("ca")) == ("prod", "prod")


def test_a_tar_of_the_repo_over_the_robot_can_no_longer_change_the_menu(rig):
    before = (_load_menu("en").options["1"].module_id,
              _load_menu("ca").options["1"].module_id)

    _deploy_tarball_over(rig)  # a tracked dev menu must never overwrite the installed one

    after = (_load_menu("en").options["1"].module_id,
             _load_menu("ca").options["1"].module_id)
    assert after == before == (PROD_EN, PROD_CA)
    # And specifically: the development module id is not reachable.
    assert DEV_ID not in after


def test_no_post_deploy_step_is_required_for_the_menu_to_be_right(rig):
    # The property that matters more than the ordering itself: a code-only redeploy
    # that skips install.sh entirely still leaves the correct menu live, so nothing
    # depends on an install step being re-run.
    _deploy_tarball_over(rig)
    assert _menu_source("en") == "configs/prod/voice_topics.yaml"
    assert _menu_source("ca") == "configs/prod/voice_topics.ca.yaml"


# --- the site tier: a menu saved on the admin page ----------------------------------


def test_a_menu_saved_on_the_admin_page_outranks_every_shipped_menu(rig, monkeypatch):
    _with_token_and_scope(monkeypatch, rig)
    _menu(rig / "configs/site/auto/voice_topics.en.yaml", AUTO_EN)
    _menu(rig / "configs/site/voice_topics.en.yaml", SITE_EN, header="saved on the robot")

    assert _menu_source("en") == str(rig / "configs/site/voice_topics.en.yaml")
    assert menu_source("en") == "site"
    assert _load_menu("en").options["1"].module_id == SITE_EN
    # ...per language: Catalan still comes from its own installed file.
    assert menu_source("ca") == "prod"
    # ...and no deploy can touch it: configs/site is never in the repository.
    _deploy_tarball_over(rig)
    assert _load_menu("en").options["1"].module_id == SITE_EN


def test_an_override_still_outranks_the_saved_menu(rig, monkeypatch):
    _menu(rig / "configs/site/voice_topics.ca.yaml", SITE_EN)
    monkeypatch.setenv("EMORI_VOICE_TOPICS_CA", "configs/voice_topics.ca.yaml")

    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"
    assert menu_source("ca") == "env"


def test_a_saved_menu_exists_only_for_a_language_this_robot_ships(rig):
    _menu(rig / "configs/site/voice_topics.fr.yaml", SITE_EN)

    assert server.site_menu_path("fr") is None
    assert server.site_menu_path("ca-ES") == str(rig / "configs/site/voice_topics.ca.yaml")
    assert menu_source("fr") != "site"


# --- the auto tier: built from this robot's Pokamind record --------------------------


def test_the_menu_built_from_the_record_outranks_only_the_development_menus(rig,
                                                                           monkeypatch):
    _with_token_and_scope(monkeypatch, rig)
    _menu(rig / "configs/site/auto/voice_topics.en.yaml", AUTO_EN)

    # A robot that still has an installed production menu keeps it...
    assert menu_source("en") == "prod"
    # ...and one that does not (the public tree ships none) speaks the built menu,
    # never the development one.
    (rig / "configs/prod/voice_topics.yaml").unlink()
    assert menu_source("en") == "auto"
    assert _menu_source("en") == str(rig / "configs/site/auto/voice_topics.en.yaml")
    assert _load_menu("en").options["1"].module_id == AUTO_EN


def test_the_built_menu_is_off_with_the_derivation_kill_switch(rig, monkeypatch):
    _with_token_and_scope(monkeypatch, rig)
    _menu(rig / "configs/site/auto/voice_topics.en.yaml", AUTO_EN)
    (rig / "configs/prod/voice_topics.yaml").unlink()
    monkeypatch.setenv("EMORI_SITE_AUTO", "0")

    assert menu_source("en") == "dev"
    assert _load_menu("en").options["1"].module_id == DEV_ID


@pytest.mark.parametrize("case", ["no_token", "another_robot", "no_snapshot"])
def test_the_built_menu_is_read_only_for_the_token_in_force(rig, monkeypatch, case):
    """Right after a token is replaced (before the restart), the snapshot and the menus
    on disk are the NEW robot's: the token still in force could not start them."""

    if case == "another_robot":
        _with_token_and_scope(monkeypatch, rig, robot_id="another-robot-02")
    elif case == "no_snapshot":
        monkeypatch.setattr(server, "ROBOT_TOKEN", robot_token.parse_token(GOLDEN_TOKEN))
    else:
        monkeypatch.setattr(server, "ROBOT_TOKEN", None)
    _menu(rig / "configs/site/auto/voice_topics.en.yaml", AUTO_EN)
    (rig / "configs/prod/voice_topics.yaml").unlink()

    assert menu_source("en") == "dev"


def test_every_tier_has_a_label(rig, monkeypatch):
    assert set(server.MENU_SOURCES) == {"env", "site", "prod", "auto", "dev", "builtin"}
    for name in ("configs/prod/voice_topics.yaml", "configs/voice_topics.yaml",
                 "configs/voice_topics.example.yaml"):
        (rig / name).unlink()
    assert menu_source("en") == "builtin"
    assert _menu_source("en") is None


# --- the simulator: no token, nothing synced, nothing saved ---------------------------


def test_the_simulator_speaks_the_development_menu(rig):
    """A box with no menu of its own (the simulator: no token, so nothing is built from
    Pokamind, nothing saved, no fork menu) speaks the tracked development menus. Its
    sessions are demo sessions, so their placeholder ids never reach Pokamind."""
    (rig / "configs/prod/voice_topics.yaml").unlink()
    (rig / "configs/prod/voice_topics.ca.yaml").unlink()

    assert _menu_source("en") == "configs/voice_topics.yaml"
    assert _load_menu("en").options["1"].module_id == DEV_ID
    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"
    assert menu_source("en") == "dev"


def test_every_box_ranks_the_tiers_the_same_way(rig, monkeypatch):
    """There is no development-box ordering: the robot's own menus always outrank the
    tracked ones, so a deploy that restores them can never change what is spoken."""
    _with_token_and_scope(monkeypatch, rig)
    _menu(rig / "configs/site/voice_topics.en.yaml", SITE_EN)
    _menu(rig / "configs/site/auto/voice_topics.en.yaml", AUTO_EN)

    candidates = server._menu_candidates("en")

    assert [Path(c).as_posix().split("configs/", 1)[1] for c in candidates] == [
        "site/voice_topics.en.yaml", "prod/voice_topics.en.yaml", "prod/voice_topics.yaml",
        "site/auto/voice_topics.en.yaml", "voice_topics.en.yaml", "voice_topics.yaml",
        "voice_topics.example.yaml"]
    assert menu_source("en") == "site"


def test_an_english_prod_menu_is_never_spoken_at_a_catalan_kiosk(rig):
    # configs/prod/voice_topics.yaml is the ENGLISH installed menu (the mapping
    # install.sh used). A Catalan robot that lost its own installed file must fall back
    # to a CATALAN file, not to English wearing a production label.
    (rig / "configs/prod/voice_topics.ca.yaml").unlink()

    assert prod_menu_source("ca") is None
    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"


def test_a_per_language_override_is_the_one_menu_decision_a_deploy_cannot_undo(rig,
                                                                               monkeypatch):
    """The escape hatch for a site whose installed Catalan menu is wrong: point
    EMORI_VOICE_TOPICS_CA at another file. `.env` is not tracked, so an override there
    survives every tar."""
    monkeypatch.setenv("EMORI_VOICE_TOPICS_CA", "configs/voice_topics.ca.yaml")

    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"
    assert menu_override_env("ca") == "EMORI_VOICE_TOPICS_CA"
    # ...and it is scoped: English is untouched.
    assert _menu_source("en") == "configs/prod/voice_topics.yaml"
    assert menu_override_env("en") == ""

    _deploy_tarball_over(rig)
    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"


def test_the_global_override_still_works_but_no_longer_freezes_both_languages(rig,
                                                                             monkeypatch):
    """A single ``EMORI_VOICE_TOPICS`` is the ONLY candidate for every language, so on
    its own the hidden language toggle would switch the voice, the pages and the SSO
    claim while the spoken menu stayed on one file. A per-language override wins over
    it, so one language can be redirected without pinning the other."""
    monkeypatch.setenv("EMORI_VOICE_TOPICS", "configs/voice_topics.yaml")

    assert _menu_source("en") == "configs/voice_topics.yaml"
    assert _menu_source("ca") == "configs/voice_topics.yaml"  # unchanged, historical

    monkeypatch.setenv("EMORI_VOICE_TOPICS_CA", "configs/prod/voice_topics.ca.yaml")
    assert _menu_source("ca") == "configs/prod/voice_topics.ca.yaml"
    assert _menu_source("en") == "configs/voice_topics.yaml"


def test_an_override_naming_a_file_that_is_not_there_falls_through_to_the_default(rig,
                                                                                 monkeypatch):
    # Unchanged behaviour, pinned because the gate depends on it: a configured-but-absent
    # path must not resolve to some neighbouring file that happens to exist.
    monkeypatch.setenv("EMORI_VOICE_TOPICS_EN", "configs/nowhere.yaml")

    assert _menu_source("en") is None
    assert menu_source("en") == "builtin"


@pytest.mark.parametrize("lang", ["ca", "ca-ES", "CA", "ca_ES", " ca "])
def test_a_language_tag_resolves_to_the_same_menu_however_it_is_spelled(rig, lang):
    assert _menu_source(lang) == "configs/prod/voice_topics.ca.yaml"


@pytest.mark.parametrize("lang", ["", "../../etc", "en;rm -rf /", "e" * 40])
def test_an_unusable_language_tag_never_becomes_an_environment_lookup(rig, monkeypatch,
                                                                      lang):
    # The code is pasted into an env var NAME (EMORI_VOICE_TOPICS_<LANG>); anything that
    # is not a plain code must not build one, and must not crash the loader either.
    monkeypatch.setenv("EMORI_VOICE_TOPICS_EN", "configs/voice_topics.example.yaml")

    assert menu_override_env(lang) == ""
    assert _menu_source(lang) is not None  # still answers with SOMETHING sane


def test_prod_menu_source_reports_what_the_menu_should_be_independently_of_what_it_is(
        rig, monkeypatch):
    # readycheck compares the two; they have to be separately observable.
    monkeypatch.setenv("EMORI_VOICE_TOPICS_CA", "configs/voice_topics.ca.yaml")

    assert _menu_source("ca") == "configs/voice_topics.ca.yaml"
    assert prod_menu_source("ca") == "configs/prod/voice_topics.ca.yaml"


def test_a_robot_with_no_configs_at_all_still_loads_a_menu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(site_settings, "base_dir",
                        lambda root=None: Path(root) if root is not None else tmp_path)
    for name in ("EMORI_VOICE_TOPICS", "EMORI_VOICE_TOPICS_EN", "EMORI_VOICE_TOPICS_CA"):
        monkeypatch.delenv(name, raising=False)

    assert _menu_source("en") is None
    assert prod_menu_source("en") is None
    assert _load_menu("en").options  # the built-in default; readycheck FAILs on it


def test_install_sh_never_copies_a_menu_onto_a_tracked_path():
    """Copying a menu onto configs/voice_topics.{en,ca}.yaml is no guard: the very next
    tar overwrites the copy."""
    install = Path(__file__).resolve().parents[1] / "deploy/robot/install.sh"
    body = install.read_text(encoding="utf-8")

    assert "install -m 644 configs/prod/voice_topics.yaml" not in body
    assert "install -m 644 configs/prod/voice_topics.ca.yaml" not in body
    # ...and it still reports a robot's installed Catalan menu when it has one.
    assert "configs/prod/voice_topics.ca.yaml" in body


def test_the_repository_ships_no_production_menus_and_never_tracks_the_site_menus():
    root = Path(__file__).resolve().parents[1]

    assert not (root / "configs/prod").exists()
    ignored = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "configs/site/" in ignored


def test_readycheck_sees_the_development_menu_as_the_tier_it_is(rig):
    """readycheck judges a development menu by its tier and its placeholder ids: no
    marker in the file, no backend to ask."""
    from emori_adapter.hostapp.readycheck import SystemProbe

    (rig / "configs/prod/voice_topics.yaml").unlink()

    probe = SystemProbe().voice_menu("en")

    assert (probe.source, probe.tier) == ("configs/voice_topics.yaml", "dev")
    assert probe.options == (("1", DEV_ID),)
    installed = rig / "configs/prod/voice_topics.yaml"
    _menu(installed, PROD_EN)
    assert SystemProbe().voice_menu("en").tier == "prod"


def test_the_documented_environment_names_exist_in_the_env_example():
    root = Path(__file__).resolve().parents[1]
    body = (root / ".env.example").read_text(encoding="utf-8")

    for name in ("EMORI_VOICE_TOPICS_CA", "EMORI_VOICE_TOPICS"):
        assert name in body, f"{name} is referenced by readycheck hints but undocumented"
    # The clobber-prone copy must not be advertised as the place to put a menu.
    assert "configs/voice_topics.en.yaml" not in body


def test_the_loader_never_reads_outside_the_paths_it_documents(rig, monkeypatch):
    # A cheap guard against a future candidate list that quietly grows a parent-directory
    # hop: relative paths stay relative (the robot's CWD is /opt/emori) and the site
    # tiers stay under the install root.
    _with_token_and_scope(monkeypatch, rig)
    for lang in ("en", "ca"):
        for tier, path in server._menu_tiers(lang):
            if tier in ("site", "auto"):
                assert Path(path).resolve().is_relative_to(rig.resolve()), path
            else:
                assert not os.path.isabs(path), path
            assert ".." not in Path(path).parts, path
