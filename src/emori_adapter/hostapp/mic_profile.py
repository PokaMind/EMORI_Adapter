"""Mode-scoped capture gain for the ONE microphone this robot has.

Two states share that single source and want incompatible levels (both measured
on the robot):

* the roleplay TRAINING session hears a person STANDING a metre or two away. At the
  idle 0.30 that far-field speech never survives the pipeline -- the session's
  voice-activity detector scored ``prob 0.003`` and the transcription came back empty --
  while ~1.4 carries the same voice through intact;
* an idle robot wants a low gain, so room noise does not read as speech. At 1.0 the
  room's own ambient tone does.

So the gain cannot be one number baked into the deploy: it follows the mode. This
helper is the only thing that moves it, and every level is env-tunable because only
the physical room can settle them -- or, on a robot nobody can SSH into, tunable from
the phone superadmin, whose persisted value (``admin/state.py``) OUTRANKS the env for
the same reason ``lang_state`` does: the only knob a human at the site can actually
reach is that slider, and a setting they watched take effect must survive the reboot
that a deploy-time env value would otherwise win back.

Deliberately BEST-EFFORT. It shells out to ``wpctl`` (PipeWire), which is absent on a
dev box and can refuse on a robot whose audio stack is still coming up; neither may keep
the hostapp from starting or kill a session, so a failure degrades to "the gain stays
where it is" and says so once in the log.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger("emori.hostapp.mic_profile")

SESSION_PROFILE = "session"
IDLE_PROFILE = "idle"

# profile -> (env override, measured default). See the module docstring for WHY
# these two numbers are what they are.
_PROFILES: dict[str, tuple[str, float]] = {
    SESSION_PROFILE: ("EMORI_MIC_SESSION_VOL", 1.4),
    IDLE_PROFILE: ("EMORI_MIC_IDLE_VOL", 0.30),
}
# profile -> the field the phone superadmin persists it under (admin/state.py).
_STATE_FIELDS: dict[str, str] = {
    SESSION_PROFILE: "mic_session_mult",
    IDLE_PROFILE: "mic_idle_mult",
}

_WPCTL = "wpctl"
_SOURCE = "@DEFAULT_AUDIO_SOURCE@"
# wpctl answers immediately when PipeWire is up; this bound only exists so a wedged
# audio stack cannot block the /session request thread that is starting a session.
_TIMEOUT_S = 5.0
# wpctl takes a raw multiplier, so >1 is over-amplification. 4.0 is far past anything
# a real robot wants and still finite: a fat-fingered EMORI_MIC_SESSION_VOL=140 (percent
# mistaken for a multiplier) must not deafen the mic for the whole demo.
_MAX_VOL = 4.0


def _persisted(profile: str) -> Optional[float]:
    """The phone superadmin's saved multiplier for ``profile``, or None. Never raises.

    Read PER CALL rather than cached: the writer is a different process
    (``emori-admin``), profile switches happen a handful of times per session, and a
    fresh read is what lets a slider moved on the phone take effect at the next
    transition with no cross-process invalidation protocol and no hostapp restart.

    ``read_state`` already returns ``{}`` for a missing or corrupt file, and the import
    is guarded too: a partial deploy that is missing the admin package must degrade to
    the env defaults, never leave the mic stuck where the previous mode wanted it.
    """

    try:
        from emori_adapter.admin.state import read_state

        raw = read_state().get(_STATE_FIELDS[profile])
        if raw is None:
            return None
        value = float(raw)
    except Exception:  # noqa: BLE001 - the gain must still move if any of that fails
        return None
    # read_state validates the range already; clamping again is what keeps the promise
    # local to this module, where _MAX_VOL is defined and could change alone.
    clamped = min(_MAX_VOL, max(0.0, value))
    if clamped != value:
        log.warning("persisted %s=%s is outside 0..%s; using %s",
                    _STATE_FIELDS[profile], value, _MAX_VOL, clamped)
    return clamped


def _level(profile: str) -> float:
    """The gain for ``profile``. Never raises.

    Precedence, highest first: the phone superadmin's persisted override, the env
    override, the measured default (see the module docstring for why the persisted
    value outranks the deployment's own env).
    """

    env_name, default = _PROFILES[profile]
    persisted = _persisted(profile)
    if persisted is not None:
        return persisted
    raw = (os.environ.get(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        # Loud, like the other on-robot knobs (server._stt_gate_json): on a screenless
        # appliance a silent revert to the default is indistinguishable from a retune
        # that took.
        log.warning("%s=%r is not a number; using %s", env_name, raw, default)
        return default
    clamped = min(_MAX_VOL, max(0.0, value))
    if clamped != value:
        log.warning("%s=%s is outside 0..%s; using %s", env_name, value, _MAX_VOL, clamped)
    return clamped


class MicProfile:
    """Hold the shared capture source at the level the current mode needs.

    ``run`` is injectable purely so tests never shell out; production passes nothing.
    """

    def __init__(self, *, run: Optional[Callable[..., object]] = None) -> None:
        self._run = run if run is not None else subprocess.run
        # Told of every profile asked for, whether or not the gain moved: the mic gate follows
        # the mode, and a robot without wpctl still has modes.
        self.listener: Optional[Callable[[str], None]] = None
        # Session start (request thread), robot-side finalize (watchdog thread) and
        # boot (lifespan) all call in. The wpctl call is made INSIDE the lock on
        # purpose: two interleaved transitions would otherwise race and leave the
        # source at the loser's level while this object believed the winner's.
        # ``_TIMEOUT_S`` is what bounds the wait for the other caller.
        self._lock = threading.Lock()
        self._profile: Optional[str] = None
        self._warned = False

    @property
    def profile(self) -> Optional[str]:
        """The profile currently APPLIED, or None if nothing has taken effect yet."""

        with self._lock:
            return self._profile

    def set_profile(self, profile: str) -> bool:
        """Move the capture gain to ``profile``. True only when wpctl actually ran ok."""

        if profile not in _PROFILES:
            # A caller bug, but this sits on the session-start path: refuse the unknown
            # name and leave the gain alone rather than raise into a live session.
            log.warning("unknown mic profile %r; leaving capture gain unchanged", profile)
            return False
        if self.listener is not None:
            try:
                self.listener(profile)
            except Exception:  # noqa: BLE001 - the gain must still move
                log.debug("mic profile listener failed", exc_info=True)
        with self._lock:
            if self._profile == profile:
                return False  # idempotent: already there, nothing to shell out for
            volume = f"{_level(profile):.2f}"
            if not self._apply(volume):
                # Deliberately NOT latched. After a failure the source is at an unknown
                # level, so the next request for this same profile must try again
                # instead of being skipped as an already-applied no-op.
                return False
            self._profile = profile
        # WARNING, not INFO: under uvicorn the hostapp's root logger filters INFO, so
        # an INFO line here would be missing from journalctl exactly when someone is
        # asking why the robot cannot hear. It fires at most twice per session.
        log.warning("mic profile %s (capture gain %s)", profile, volume)
        return True

    def _apply(self, volume: str) -> bool:
        argv = [_WPCTL, "set-volume", _SOURCE, volume]
        try:
            result = self._run(
                argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
            )
        except FileNotFoundError:
            self._warn("wpctl is not installed; capture gain left as-is")
            return False
        except (OSError, subprocess.SubprocessError) as e:  # incl. TimeoutExpired
            self._warn(f"wpctl failed ({type(e).__name__}); capture gain left as-is")
            return False
        code = getattr(result, "returncode", 0)
        if code:
            self._warn(f"wpctl exited {code}; capture gain left as-is")
            return False
        return True

    def _warn(self, message: str) -> None:
        """Say it once at WARNING, then stay quiet.

        The hostapp's root logger filters INFO, so an INFO-level note here would be
        invisible in journalctl exactly when someone is asking why the robot cannot
        hear. But a robot without wpctl would repeat it on every session transition,
        so only the first one is loud.
        """

        if self._warned:
            log.debug("mic profile: %s", message)
            return
        self._warned = True
        log.warning("mic profile: %s", message)
