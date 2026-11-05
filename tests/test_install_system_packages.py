"""``deploy/robot/install.sh`` installs every system tool its own steps run.

A brand-new robot has only what its OS image ships. install.sh is ``set -euo pipefail``,
so a step that calls a tool nobody installed ends the whole install with exit 127 --
before step 9 has installed a single unit. A fresh Debian trixie box shows it:
``scripts/get_voice_models.sh`` unpacks the Vosk models with ``unzip``, which a minimal
image does not have.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_INSTALL = _REPO / "deploy" / "robot" / "install.sh"
_MODELS = _REPO / "scripts" / "get_voice_models.sh"


def _apt_packages() -> set[str]:
    """The package names of install.sh's ``apt-get install`` (step 1)."""

    text = _INSTALL.read_text(encoding="utf-8")
    match = re.search(r"apt-get install -y -qq --no-upgrade \\\n(.*?)\n\n", text, re.S)
    assert match, "install.sh no longer has its step-1 apt-get install block"
    words = match.group(1).replace("\\\n", " ").split()
    return {word for word in words if not word.startswith("-")}


def test_the_model_downloader_runs_from_install_sh():
    assert "bash scripts/get_voice_models.sh" in _INSTALL.read_text(encoding="utf-8")


def test_unzip_is_installed_because_the_model_downloader_needs_it():
    downloader = _MODELS.read_text(encoding="utf-8")
    assert re.search(r"^\s*unzip ", downloader, re.M), "get_voice_models.sh stopped using unzip"
    assert "unzip" in _apt_packages()


def test_a_redeploy_never_upgrades_installed_packages():
    # A robot in service keeps its Chromium and camera stack across a code deploy: a
    # plain `apt-get install` upgrades every listed package the refreshed lists have newer.
    text = _INSTALL.read_text(encoding="utf-8")
    installs = re.findall(r"apt-get install [^\n]*", text)
    step1 = [line for line in installs if "-qq" in line]
    assert step1 and all("--no-upgrade" in line for line in step1)


def test_the_apt_block_still_parses():
    packages = _apt_packages()
    # The ones the robot cannot work without, so the parser above is known to read the
    # real list and not an empty or truncated one.
    assert {"v4l2loopback-dkms", "chromium", "xvfb", "unzip"} <= packages
