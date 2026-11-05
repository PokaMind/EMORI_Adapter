"""``one-click-deploy.sh`` step 2b: a robot's production menus survive the move to a
tree that ships none.

A robot deployed from a tree with ``configs/prod/voice_topics*.yaml`` speaks those menus.
The public tree ships no ``configs/prod``, so the deploy's prune (step 3) would retire
them and the robot would fall back to another menu. Step 2b copies a menu that was
really this robot's into ``configs/site/`` (robot-local, never shipped, read first) --
only from a file the last deploy shipped and this one does not, never a placeholder,
never over an existing file, and never when ``.env`` points the language elsewhere.

The two steps are RUN here, in bash, in a scratch install root.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "robot" / "one-click-deploy.sh"

REAL_EN = "AAAAAAAAAAAAAAAAAAAA"
REAL_CA = "BBBBBBBBBBBBBBBBBBBB"


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _blocks() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    return text[text.index("# 2b. Adopt"):text.index("# 4. Internet decides the mode")]


def _menu(module_id: str) -> str:
    return ("prompt: say one\n"
            "options:\n"
            "  '1':\n"
            f"    module_id: {module_id}\n"
            "    name: Topic one\n")


class _Robot:
    """A scratch /opt/emori whose LAST deploy shipped the production menus."""

    def __init__(self, tmp_path: Path, *, en: str = REAL_EN, ca: str = REAL_CA,
                 env_text: str = "", ships_prod: bool = False, last_listed: bool = True):
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        self.bash = bash
        self.root = tmp_path / "emori"
        (self.root / "configs" / "prod").mkdir(parents=True)
        (self.root / "artifacts").mkdir()
        self.prod_en = self.root / "configs" / "prod" / "voice_topics.yaml"
        self.prod_ca = self.root / "configs" / "prod" / "voice_topics.ca.yaml"
        self.prod_en.write_text(_menu(en), encoding="utf-8", newline="\n")
        self.prod_ca.write_text(_menu(ca), encoding="utf-8", newline="\n")
        self.site_en = self.root / "configs" / "site" / "voice_topics.en.yaml"
        self.site_ca = self.root / "configs" / "site" / "voice_topics.ca.yaml"
        (self.root / ".env").write_text(env_text, encoding="utf-8", newline="\n")
        prod = "configs/prod/voice_topics.yaml\nconfigs/prod/voice_topics.ca.yaml\n"
        last = "configs/\nconfigs/voice_topics.yaml\n" + (prod if last_listed else "")
        (self.root / "artifacts" / "last-deploy.files").write_text(
            last, encoding="utf-8", newline="\n")
        self.files = tmp_path / "files.txt"
        self.files.write_text("configs/\nconfigs/voice_topics.yaml\n"
                              + (prod if ships_prod else ""), encoding="utf-8", newline="\n")
        (self.root / "configs" / "voice_topics.yaml").write_text(_menu("dev-en-module-1"),
                                                                 encoding="utf-8")
        self.stubs = tmp_path / "stubs"
        self.stubs.mkdir()
        sudo = self.stubs / "sudo"
        sudo.write_text('#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n', encoding="ascii",
                        newline="\n")
        sudo.chmod(0o755)

    def run(self) -> subprocess.CompletedProcess:
        script = "\n".join([
            "set -uo pipefail",
            f"ROOT='{self.root.as_posix()}'",
            'cd "$ROOT"',
            f"FILES='{self.files.as_posix()}'",
            'ME="$(id -un)"',
            _blocks(),
        ])
        path = self.root.parent / "adopt.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        env = {**os.environ, "PATH": f"{self.stubs.as_posix()}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([self.bash, path.as_posix()], capture_output=True,
                              encoding="utf-8", errors="replace", env=env, timeout=60,
                              check=False)

    def retired(self, name: str) -> list[Path]:
        return list((self.root / "artifacts" / "superseded").rglob(name))


def test_a_real_menu_is_adopted_and_the_prune_still_retires_the_source(tmp_path):
    robot = _Robot(tmp_path)
    original_en = robot.prod_en.read_text(encoding="utf-8")

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert robot.site_en.read_text(encoding="utf-8") == original_en
    assert REAL_CA in robot.site_ca.read_text(encoding="utf-8")
    assert "adopted configs/prod/voice_topics.yaml as this robot's menu" in result.stdout
    assert "adopted configs/prod/voice_topics.ca.yaml as this robot's menu" in result.stdout
    # The prune ran after it, exactly as before: the sources are moved aside.
    assert not robot.prod_en.exists() and not robot.prod_ca.exists()
    assert robot.retired("voice_topics.yaml") and robot.retired("voice_topics.ca.yaml")


@pytest.mark.parametrize("placeholder", ["example-en-module-1", "demo-module-3",
                                         "dev-en-module-1", "'example-x'"])
def test_a_placeholder_menu_is_not_adopted(tmp_path, placeholder):
    robot = _Robot(tmp_path, en=placeholder)

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert not robot.site_en.exists()
    assert "placeholder" in result.stdout
    assert robot.site_ca.exists()                 # the other language is judged alone
    assert not robot.prod_en.exists()             # ...and still retired by the prune


def test_an_existing_robot_menu_is_never_overwritten(tmp_path):
    robot = _Robot(tmp_path)
    robot.site_en.parent.mkdir(parents=True)
    robot.site_en.write_text("saved on the admin page\n", encoding="utf-8")

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert robot.site_en.read_text(encoding="utf-8") == "saved on the admin page\n"
    assert "adopted configs/prod/voice_topics.yaml" not in result.stdout
    assert robot.site_ca.exists()


def test_a_per_language_override_blocks_that_language_only(tmp_path):
    robot = _Robot(tmp_path, env_text="EMORI_VOICE_TOPICS_CA=configs/elsewhere.yaml\n")

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert not robot.site_ca.exists()
    assert robot.site_en.exists()


def test_the_global_override_blocks_both_and_an_empty_one_blocks_nothing(tmp_path):
    blocked = _Robot(tmp_path / "a", env_text="EMORI_VOICE_TOPICS=configs/elsewhere.yaml\n")
    blocked.run()
    assert not blocked.site_en.exists() and not blocked.site_ca.exists()

    empty = _Robot(tmp_path / "b", env_text="EMORI_VOICE_TOPICS_CA=\nEMORI_VOICE_TOPICS=\n")
    empty.run()
    assert empty.site_en.exists() and empty.site_ca.exists()


def test_a_tree_that_still_ships_the_menus_adopts_nothing_and_retires_nothing(tmp_path):
    robot = _Robot(tmp_path, ships_prod=True)

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert not robot.site_en.exists() and not robot.site_ca.exists()
    assert robot.prod_en.exists() and robot.prod_ca.exists()


def test_a_file_no_deploy_shipped_is_not_adopted(tmp_path):
    robot = _Robot(tmp_path, last_listed=False)

    robot.run()

    assert not robot.site_en.exists() and not robot.site_ca.exists()


def test_adoption_runs_before_the_prune_in_every_mode():
    text = _SCRIPT.read_text(encoding="utf-8")

    assert text.index("# 2b. Adopt") < text.index("# 3. Prune")
    # Before the full/quick split, so a quick deploy adopts too.
    assert text.index("adopt_menu configs/prod/voice_topics.ca.yaml") \
        < text.index('if [ "$MODE" = full ]')


@pytest.mark.parametrize("line", ["EMORI_VOICE_TOPICS_EN=configs/prod/voice_topics.yaml",
                                  'EMORI_VOICE_TOPICS_EN="configs/prod/voice_topics.yaml"',
                                  "EMORI_VOICE_TOPICS_EN=./configs/prod/voice_topics.yaml",
                                  "EMORI_VOICE_TOPICS=configs/prod/voice_topics.yaml"])
def test_a_file_an_override_names_is_kept_and_adopted(tmp_path, line):
    """The hostapp reads an override and nothing else: retiring the file it names would
    leave the robot on the built-in placeholder menu."""

    robot = _Robot(tmp_path, env_text=line + "\n")
    original = robot.prod_en.read_text(encoding="utf-8")

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert robot.prod_en.read_text(encoding="utf-8") == original     # still served
    assert not robot.retired("voice_topics.yaml")
    assert "kept configs/prod/voice_topics.yaml" in result.stdout
    # Adopted too, so removing the .env line later serves the same menu.
    assert robot.site_en.read_text(encoding="utf-8") == original


def test_an_absolute_override_inside_the_install_is_the_same_file(tmp_path):
    robot = _Robot(tmp_path)
    (robot.root / ".env").write_text(
        f"EMORI_VOICE_TOPICS_CA={robot.prod_ca.as_posix()}\n", encoding="utf-8", newline="\n")

    result = robot.run()

    assert result.returncode == 0, result.stderr
    assert robot.prod_ca.exists() and not robot.retired("voice_topics.ca.yaml")


@pytest.mark.parametrize("line", ['EMORI_VOICE_TOPICS_CA=""', "EMORI_VOICE_TOPICS_CA=''",
                                  "EMORI_VOICE_TOPICS_CA=   "])
def test_a_quoted_empty_override_is_no_override(tmp_path, line):
    robot = _Robot(tmp_path, env_text=line + "\n")

    robot.run()

    assert robot.site_ca.exists()                  # adopted
    assert not robot.prod_ca.exists()              # and retired, as without the line
