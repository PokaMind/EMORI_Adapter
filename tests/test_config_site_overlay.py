"""``config`` runs the settings layer at import, and changes nothing where it has no say.

A dev box or a robot in service with explicit ``.env`` values keeps exactly the
environment it had.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

_REPO = Path(__file__).resolve().parents[1]

# Everything the settings layer reads, cleared so the host's environment cannot leak into
# the child process.
_SITE_ENV = (
    "EMORI_HARDWARE_PROFILE", "EMORI_SITE_AUTO", "EMORI_SITE_SOURCES", "POKAMIND_BASE",
    "POKAMIND_ROBOT_TOKEN", "EMORI_POKAMIND_TOKEN_FILE", "EMORI_COURSE_ID", "EMORI_LANG",
    "EMORI_PATHWAYS", "EMORI_ROBOT_NAME", "EMORI_CUE_AMP", "EMORI_CUE_Z_MM",
)


def _import_config(extra: dict) -> dict:
    env = {key: value for key, value in os.environ.items() if key not in _SITE_ENV}
    env.update({"PYTHONPATH": str(_REPO / "src"), "EMORI_ADDONS": "off"})
    env.update(extra)
    code = ("import json, os\n"
            "before = dict(os.environ)\n"
            "import emori_adapter.config as config\n"
            "after = dict(os.environ)\n"
            "print(json.dumps({'same': before == after,"
            " 'changed': sorted(k for k in set(before) | set(after)"
            " if before.get(k) != after.get(k)),"
            " 'sources': config.Settings().site_sources}))\n")

    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                            text=True, timeout=120, check=False, cwd=str(_REPO))

    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_config_with_no_profile_changes_nothing():
    """A fresh interpreter: the environment after ``import config`` is the one before."""

    out = _import_config({"EMORI_CUE_AMP": "1.2", "EMORI_CUE_Z_MM": "12"})

    assert out["same"], out["changed"]
    assert out["sources"]["EMORI_CUE_AMP"] == "env"
    assert "EMORI_SITE_SOURCES" not in out["changed"]


def test_an_installed_robot_gets_its_hardware_values_at_import():
    out = _import_config({"EMORI_HARDWARE_PROFILE": "reachy-mini-wireless",
                          "EMORI_CUE_Z_MM": "12"})

    assert out["sources"]["EMORI_CUE_Z_MM"] == "env"
    assert out["sources"]["EMORI_CUE_AMP"] == "hardware"
    assert out["changed"] and "EMORI_CUE_AMP" in out["changed"]
