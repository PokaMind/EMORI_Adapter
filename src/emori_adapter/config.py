"""Runtime configuration, loaded from environment / .env with sane defaults.

We avoid pydantic-settings to keep deps light; a small dataclass + os.environ
+ python-dotenv is enough and explicit.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from emori_adapter import site_settings
# The walk lives in site_settings (emori-admin needs it without importing this module).
from emori_adapter.site_settings import find_install_dotenv


# The .env this process configures itself from ("" when there is none): the nearest
# one walking up from this package -- /opt/emori/.env on a robot -- however and from
# wherever the process was started. A RELATIVE path in it means "relative to the
# install" (robot_token.install_root), never to the working directory.
DOTENV_FILE = find_install_dotenv(os.path.dirname(os.path.abspath(__file__)))
if DOTENV_FILE:
    load_dotenv(DOTENV_FILE)

# The robot-local settings layer (site_settings): admin-saved values, then .env, then
# this robot's Pokamind record, then its hardware profile, written into os.environ so
# every reader below and elsewhere sees the effective value. It never raises; the guard
# is for an import-time surprise, so a broken layer can never stop a process.
try:
    site_settings.apply(os.environ, dotenv_file=DOTENV_FILE or None)
except Exception:  # noqa: BLE001
    import logging as _logging

    _logging.getLogger("emori.config").warning(
        "site_settings_apply_failed: the environment is used as loaded")


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_finite_float(value: str | None) -> float | None:
    """Parse an explicitly configured finite number without coercion."""

    if value is None or not value or value != value.strip():
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _get_finite_float(name: str, default: float) -> float:
    """The default for a missing, blank, padded or non-finite value."""

    parsed = _parse_finite_float(os.environ.get(name))
    return default if parsed is None else parsed


@dataclass
class Settings:
    # --- Reachy Mini daemon ---
    # Slice 1 (Windows local): http://127.0.0.1:8000
    # Slice 2 (from ROS 2 container): http://host.docker.internal:8000
    reachy_base_url: str = field(default_factory=lambda: _get("REACHY_BASE_URL", "http://127.0.0.1:8000"))
    reachy_timeout_s: float = field(default_factory=lambda: _get_float("REACHY_TIMEOUT_S", 5.0))

    # --- Learner identity (ROS4HRI person id) ---
    person_id: str = field(default_factory=lambda: _get("EMORI_PERSON_ID", "learner_01"))
    session_id: str = field(default_factory=lambda: _get("EMORI_SESSION_ID", "sim-session"))

    # --- Loop cadence ---
    cycle_period_s: float = field(default_factory=lambda: _get_float("EMORI_CYCLE_PERIOD_S", 2.0))

    # --- Pokamind live source: the hostapp relay (the producer is /api/cycle) ---
    pokamind_ws_url: str = field(
        default_factory=lambda: _get("POKAMIND_WS_URL", "ws://localhost:8080/ws/relay"))

    # --- FIWARE / NGSI-LD ---
    orion_url: str = field(default_factory=lambda: _get("ORION_URL", "http://localhost:1026"))
    ngsild_tenant: str = field(default_factory=lambda: _get("NGSILD_TENANT", "pokamind"))

    # --- On-robot management dashboard ---
    # The local SQLite mirror of the FIWARE feed (hostapp/metrics_store.py) plus the
    # shared management password that gates the dashboard pages. The salt is what
    # makes a person code stable across restarts WITHOUT the robot storing a name;
    # unset, a fallback is derived from EMORI_ROBOT_ID and a warning is logged.
    dash_db: str = field(default_factory=lambda: _get("EMORI_DASH_DB", "artifacts/dashboard.db"))
    dash_salt: str = field(default_factory=lambda: _get("EMORI_DASH_SALT", ""))
    dash_password: str = field(default_factory=lambda: _get("EMORI_DASH_PASSWORD", ""))
    heartbeat_s: float = field(default_factory=lambda: _get_float("EMORI_HEARTBEAT_S", 60.0))
    # Human-readable name in the dashboard header ("Lobby EMORI"). Cosmetic only --
    # EMORI_ROBOT_ID stays the identity; this is what a manager reads on a phone.
    robot_name: str = field(default_factory=lambda: _get("EMORI_ROBOT_NAME", ""))

    # --- Head-turn selection gesture (hostapp.gestures.DirectionalGestureDetector) ---
    directional_yaw_threshold_rad: float = field(
        default_factory=lambda: _get_finite_float("EMORI_DIRECTIONAL_YAW_THRESHOLD_RAD", 0.20)
    )
    directional_roll_threshold_rad: float = field(
        default_factory=lambda: _get_finite_float("EMORI_DIRECTIONAL_ROLL_THRESHOLD_RAD", 0.20)
    )
    directional_ambiguity_ratio: float = field(
        default_factory=lambda: _get_finite_float("EMORI_DIRECTIONAL_AMBIGUITY_RATIO", 1.40)
    )
    # Where each robot-local setting's value came from (site_settings): key -> env |
    # admin | calibration | calibration_fallback | pokamind | hardware | default.
    # Names and layers only, never a value.
    site_sources: dict = field(default_factory=site_settings.effective_sources, repr=False,
                               compare=False)


def load_settings() -> Settings:
    return Settings()
