"""Speaker + microphone levels: daemon API, persisted state, wpctl multipliers.

Three levels exist on this robot and an operator with a phone must be able to
move all three without knowing that:

1. **hardware output** (0-100) — the daemon's ``/api/volume/set``, which also
   plays a test sound so the change is audible immediately;
2. **hardware capture** (0-100) — the daemon's ``/api/volume/microphone/set``;

   (on v2 there is no daemon: the same two routes are answered in this process
   by :mod:`.mixer`, the daemon's own ALSA ``VolumeControl``, and
   :mod:`.host_backend` decides which -- every call goes through
   :func:`_volume`);
3. **the hostapp's mode-scoped capture MULTIPLIER** (``wpctl``, 0..4.0) — one
   value while a roleplay session is running (far-field, ~1.4) and a much lower
   one while the robot is idle (~0.30).  See
   ``hostapp/mic_profile.py`` for why those two numbers cannot be one number.

Everything here is persisted through :mod:`emori_adapter.admin.state` and
**re-applied at startup**.  That is the whole point: the mixer is volatile, so
without re-applying the persisted level a reboot silently leaves the robot at
volume 0 and takes its voice away.

Two hard constraints, both from the field:

* **Never open PortAudio from this process.**  The hostapp owns capture, and a
  second process touching a wedged PortAudio stream SIGABRTs it.  Verification
  is the daemon API plus readycheck's audio-duplex gate, never a live meter
  here.
* **wpctl needs ``XDG_RUNTIME_DIR=/run/user/1000``**, which the systemd unit
  provides.  It is deliberately NOT set in code: a value invented here would
  mask a unit-file regression that also breaks the hostapp.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from emori_adapter.admin import host_backend, mixer, probes
from emori_adapter.admin.state import AdminState

log = logging.getLogger("emori.admin.audio")

# Mirrors hostapp/mic_profile.py:38-51 rather than importing it: that module
# lives behind ``emori_adapter.hostapp.__init__``, which imports the SSO signer
# and therefore PyJWT — an optional extra the admin process must not need in
# order to draw a slider. Keep these three numbers in step with mic_profile.py.
DEFAULT_SESSION_MULT = 1.4
DEFAULT_IDLE_MULT = 0.30
MAX_MULT = 4.0
_ENV_MULT = {"session": "EMORI_MIC_SESSION_VOL", "idle": "EMORI_MIC_IDLE_VOL"}

_WPCTL = "wpctl"
_SOURCE = "@DEFAULT_AUDIO_SOURCE@"

# The daemon comes up alongside us at boot and its volume backend needs the
# audio device; retry on this ladder (seconds after start) before giving up.
BOOT_RETRY_DELAYS_S = (0.0, 5.0, 10.0, 20.0, 30.0, 60.0)


def clamp_percent(value: Any) -> Optional[int]:
    """0-100 int, or ``None`` when the body is not a usable level."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > 100:
        return None
    return number


def clamp_mult(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number < 0.0 or number > MAX_MULT:  # NaN-safe
        return None
    return round(number, 3)


# --------------------------------------------------------------------------
# the daemon's volume API (routers/volume.py — body is {"volume": 0..100}),
# wherever it runs
# --------------------------------------------------------------------------
def _volume(method: str, path: str, *, json_body: Any = None,
            chime: bool = True) -> probes.Http:
    """One daemon ``/api/volume/*`` call, answered by whichever backend is in force.

    Same rule as ``network._wifi``: a route-missing 404 from ``:8000`` teaches
    the selector that this is a daemon-less robot, and the call is answered by
    the host at once.  ``chime`` only exists on the host backend (the daemon
    always tries its test sound after a speaker set -- a no-op under the
    ``--no-media`` v1 deploy).
    """

    if host_backend.current() == host_backend.HOST:
        return mixer.request(method, path, json_body=json_body, chime=chime)
    result = probes.daemon(method, path, json_body=json_body)
    if (host_backend.route_missing(result)
            and host_backend.note_route_missing(path).backend == host_backend.HOST):
        return mixer.request(method, path, json_body=json_body, chime=chime)
    return result


def speaker_read() -> probes.Http:
    """The raw speaker-level read (readycheck's deep check wants the failure too)."""

    return _volume("GET", "/api/volume/current")


def mic_read() -> probes.Http:
    return _volume("GET", "/api/volume/microphone/current")


def level_of(result: probes.Http) -> Optional[int]:
    payload = result.payload if isinstance(result.payload, dict) else {}
    return clamp_percent(payload.get("volume")) if result.ok else None


def speaker_get() -> Optional[int]:
    return level_of(speaker_read())


def mic_get() -> Optional[int]:
    return level_of(mic_read())


def speaker_set(volume: int, *, chime: bool = True) -> probes.Http:
    # The daemon plays a short test sound on this route (volume.py:104-110), so
    # the operator hears the new level without a second tap. Deliberate.
    # ``chime=False`` is the boot re-apply: the host backend really plays it,
    # and a robot must not chirp at whoever is near it when it powers up.
    return _volume("POST", "/api/volume/set", json_body={"volume": int(volume)},
                   chime=chime)


def mic_set(volume: int) -> probes.Http:
    return _volume("POST", "/api/volume/microphone/set",
                   json_body={"volume": int(volume)})


def test_sound() -> probes.Http:
    return _volume("POST", "/api/volume/test-sound")


# --------------------------------------------------------------------------
# mic multipliers (state overrides env overrides the measured default)
# --------------------------------------------------------------------------
def _env_mult(profile: str) -> Optional[float]:
    raw = (os.environ.get(_ENV_MULT[profile], "") or "").strip()
    if not raw:
        return None
    try:
        return clamp_mult(float(raw))
    except (TypeError, ValueError):
        return None


def _resolve_mult(persisted: dict, profile: str, default: float) -> float:
    for candidate in (persisted.get("mic_%s_mult" % profile), _env_mult(profile)):
        if candidate is not None:
            return float(candidate)
    return default


def mic_multipliers(state: AdminState) -> dict[str, float]:
    """The two multipliers the hostapp will actually use, resolved in order.

    Same precedence B wires into ``mic_profile._level``: persisted operator
    override, then the env knob, then the measured default.  Resolved here too
    so the phone shows the number that is in force, not the one it last wrote.
    """

    persisted = state.get()
    return {"session": _resolve_mult(persisted, "session", DEFAULT_SESSION_MULT),
            "idle": _resolve_mult(persisted, "idle", DEFAULT_IDLE_MULT)}


def applied_profile() -> Optional[str]:
    """Which mic profile the hostapp reports as APPLIED (``/api/status``).

    ``None`` means the hostapp is down, or wpctl never took there — in both
    cases the immediate apply below has nothing to aim at and must not guess.
    """

    status = probes.hostapp_status()
    if not status:
        return None
    value = status.get("mic_profile")
    return value if value in _ENV_MULT else None


def apply_mult_now(state: AdminState) -> dict:
    """Push the multiplier for the CURRENTLY applied profile straight to wpctl.

    So the slider is felt in the room instead of at the next profile switch.
    Safe to do behind the hostapp's back: ``MicProfile`` caches the profile
    NAME, not the level, so it will not fight us — and when it does next switch
    profiles it reads the same persisted state we just wrote (B's change to
    ``mic_profile._level``), so the two never disagree.

    Best-effort by construction: no wpctl (dev box), no hostapp, or no applied
    profile all mean "the value is persisted and takes effect on the next
    switch", which is still correct, just not instant.
    """

    profile = applied_profile()
    if profile is None:
        return {"applied": False, "reason": "no_applied_profile"}
    volume = "%.2f" % mic_multipliers(state)[profile]
    result = probes.run([_WPCTL, "set-volume", _SOURCE, volume])
    if not result.ok:
        return {"applied": False, "reason": result.error or "wpctl_failed",
                "profile": profile}
    return {"applied": True, "profile": profile, "volume": float(volume)}


# --------------------------------------------------------------------------
# the composed GET /api/audio payload
# --------------------------------------------------------------------------
def snapshot(state: AdminState) -> dict:
    """``GET /api/audio``.  A level the backend could not read is ``None``, and its
    ``*_error`` says why in one word (``route_missing``, ``alsa_missing``, ...),
    so the Audio tab can say what to do instead of drawing an empty slider."""

    multipliers = mic_multipliers(state)
    speaker, mic = speaker_read(), mic_read()
    return {
        "ok": True,
        "speaker": level_of(speaker),
        "mic": level_of(mic),
        "speaker_error": host_backend.error_slug(speaker) or None,
        "mic_error": host_backend.error_slug(mic) or None,
        "applied_profile": applied_profile(),
        "mic_session_mult": multipliers["session"],
        "mic_idle_mult": multipliers["idle"],
        "persisted": state.get(),
    }


# --------------------------------------------------------------------------
# boot re-apply
# --------------------------------------------------------------------------
def reapply_persisted(state: AdminState) -> dict:
    """Push the persisted hardware levels back into the mixer.  Never raises.

    Through the same backend as every other volume call (the daemon on v1, this
    process's ALSA mixer on v2).  Returns ``{"pending": bool, ...}``:
    ``pending`` is True when something was persisted but the backend
    refused/was absent, which is the caller's cue to try again on the retry
    ladder (the daemon's audio backend -- or the USB card itself -- can take tens
    of seconds to come up on a cold boot).
    """

    persisted = state.get()
    out: dict[str, Any] = {"pending": False, "speaker": None, "mic": None}
    speaker = clamp_percent(persisted.get("speaker_volume"))
    mic = clamp_percent(persisted.get("mic_volume"))
    if speaker is None and mic is None:
        return out  # nothing was ever set from the phone; leave the mixer alone
    if speaker is not None:
        applied = speaker_set(speaker, chime=False).ok
        out["speaker"] = speaker if applied else None
        out["pending"] = out["pending"] or not applied
    if mic is not None:
        applied = mic_set(mic).ok
        out["mic"] = mic if applied else None
        out["pending"] = out["pending"] or not applied
    if not out["pending"]:
        log.warning("persisted audio re-applied at startup (speaker=%s mic=%s)",
                    out["speaker"], out["mic"])
    return out
