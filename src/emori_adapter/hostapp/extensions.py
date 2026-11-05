"""Optional hostapp add-ons: the one seam between the training kiosk and anything extra.

The kiosk runs complete with no add-on installed. A separately installed package can add
modes that share the robot with training sessions by publishing a :class:`HostExtension`
under the ``emori_adapter.extensions`` entry-point group::

    [project.entry-points."emori_adapter.extensions"]
    my_addon = "my_addon.extension:MyExtension"

At most one add-on is loaded (the first entry point by name); ``EMORI_ADDONS=off`` skips
discovery. A broken add-on never takes the kiosk down: it is logged and the
:class:`HostExtension` defaults apply, which accept every badge and never hold the robot.

Entry-point modules are imported by the hostapp, the admin service and the readiness
check alike, so they must stay light: import media or network stacks inside ``attach``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Mapping, Optional, Sequence

log = logging.getLogger("emori.hostapp.extensions")

ENTRY_POINT_GROUP = "emori_adapter.extensions"

# Bumped when a hook changes incompatibly. An add-on declares the version it was built for
# as ``api_version`` on its class; a mismatch leaves it unloaded rather than half-working.
API_VERSION = 1

# What HostExtension.claim_badge answers.
BADGE_OK = "ok"
BADGE_REFUSED = "refused"
BADGE_PENDING = "pending"

# The default scan-to-/session window, overridable by EMORI_TRAINING_NAV_TIMEOUT_S.
_DEFAULT_CLAIM_TIMEOUT_S = 60.0


def claim_timeout_s() -> float:
    """The badge-scan-to-/session deadline in seconds.

    Deployment-tunable (``EMORI_TRAINING_NAV_TIMEOUT_S``) because slow Piper prompts, Vosk
    retries or a cold SSO redirect can exceed the 60-second default on a real robot.
    Values outside 10-600 seconds (or non-numeric ones) keep the default; NaN fails the
    range check.
    """

    raw = os.environ.get("EMORI_TRAINING_NAV_TIMEOUT_S")
    if raw is None:
        return _DEFAULT_CLAIM_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CLAIM_TIMEOUT_S
    if not (10.0 <= value <= 600.0):
        return _DEFAULT_CLAIM_TIMEOUT_S
    return value


@dataclass(frozen=True)
class ExtensionContext:
    """The live hostapp objects an add-on may use, handed over once by ``attach``."""

    driver: Any
    gesture_ticker: Any
    lang_state: Any
    # Raise the session capture gain while an add-on mode holds the microphone.
    mic_claim: Callable[[bool], None]
    # Is a training session really exchanging turns, or only navigated to?
    session_alive: Callable[[], bool]
    # The _build_app test overrides, for add-on specific fakes.
    overrides: Mapping[str, Any]
    # Set when a ROS process hosts the app (its node name and a status publisher); else None.
    ros: Any = None


class HostExtension:
    """An add-on's hooks, each defaulting to "no add-on here".

    ``attach`` runs once while the app is built; every other hook may run on any thread.
    """

    name = ""
    api_version = API_VERSION

    def attach(self, ctx: ExtensionContext) -> None:
        """Receive the hostapp objects, before any other hook runs."""

    async def start(self) -> None:
        """Lifespan startup, on the event loop. A failure leaves training available."""

    async def shutdown(self) -> None:
        """Lifespan teardown, before the hostapp's own workers stop."""

    def on_head_turn(self, direction: Any) -> None:
        """A signed head turn (``gestures.GestureDirection``) while no session runs."""

    def on_head_back(self) -> bool:
        """The head was pushed back while no session runs. True only if that ended a mode."""
        return False

    def selection_reserved(self) -> bool:
        """True while a badge scan owns the head, so the gesture detector stands down."""
        return False

    def busy(self) -> bool:
        """True while a mode is running: no language switch or service restart underneath."""
        return False

    def claim_badge(self, payload: str) -> str:
        """May this decoded badge start a training session? ``BADGE_OK`` / ``_REFUSED`` / ``_PENDING``."""
        return BADGE_OK

    def bind_session(self, session_id: str) -> bool:
        """A session was minted for a claimed badge. False refuses it."""
        return True

    def session_opened(self, session_id: str) -> bool:
        """The kiosk opened that session's page. False refuses it."""
        return True

    def session_ended(self, session_id: Optional[str] = None) -> None:
        """A training session ended, or its mint expired unopened."""

    def status(self) -> Optional[dict]:
        """Content-free state: ``{"mode": str, "enabled": {name: bool}, ...}``. None: nothing to show."""
        return None

    def hint_lines(self, lang: str) -> Sequence[str]:
        """Short lines for the landing page, in ``lang``."""
        return ()

    def register_routes(self, app: Any) -> None:
        """Add HTTP routes to the hostapp."""

    def admin_api(self) -> Any:
        """An object the hostapp's admin routes delegate add-on actions to, or None."""
        return None


def addons_switched_off(value: Optional[str]) -> bool:
    """Does this value of ``EMORI_ADDONS`` skip discovery?"""

    return (value or "").strip().lower() in ("off", "0", "false")


def _discover() -> list[metadata.EntryPoint]:
    """Installed add-on entry points, in a stable order. EMORI_ADDONS=off finds none."""

    if addons_switched_off(os.environ.get("EMORI_ADDONS")):
        return []
    try:
        found = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - a broken environment still runs the kiosk
        log.warning("add-on discovery failed; running without add-ons", exc_info=True)
        return []
    return sorted(found, key=lambda entry: entry.name)


def extension_class() -> Optional[type]:
    """The installed add-on's class (imported, not instantiated), or None.

    Separate from :func:`load_extension` for the processes that never attach one but do
    read what it contributes: the settings overlay, the admin service, the readiness check.
    """

    entries = _discover()
    if not entries:
        return None
    if len(entries) > 1:
        log.warning("%d add-ons installed; loading only %s", len(entries), entries[0].name)
    try:
        cls = entries[0].load()
    except Exception:  # noqa: BLE001 - training must remain available
        log.warning("add-on %s failed to import; running without it", entries[0].name,
                    exc_info=True)
        return None
    if getattr(cls, "api_version", None) != API_VERSION:
        log.warning("add-on %s is built for API %r, this core is %d; running without it",
                    entries[0].name, getattr(cls, "api_version", None), API_VERSION)
        return None
    return cls


def load_extension() -> HostExtension:
    """A fresh instance of the installed add-on, or the no-op defaults."""

    cls = extension_class()
    if cls is None:
        return HostExtension()
    try:
        return cls()
    except Exception:  # noqa: BLE001 - training must remain available
        log.warning("add-on %s failed to start; running without it",
                    getattr(cls, "__name__", cls), exc_info=True)
        return HostExtension()


def contributed(hook: str, *args: Any, **kwargs: Any) -> Any:
    """Call a classmethod the add-on may publish (``settings_registry``, ``readiness_gates``, ...).

    Answers None with no add-on, no such hook, or a hook that raises. Static on purpose:
    the callers run before, or in processes without, a live hostapp.
    """

    cls = extension_class()
    method = getattr(cls, hook, None) if cls is not None else None
    if not callable(method):
        return None
    try:
        return method(*args, **kwargs)
    except Exception:  # noqa: BLE001 - an add-on fault must not stop the caller
        log.warning("add-on hook %s failed", hook, exc_info=True)
        return None
