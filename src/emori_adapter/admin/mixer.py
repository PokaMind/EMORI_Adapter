"""Speaker/mic hardware volume without the reachy-mini daemon: its Linux ``VolumeControl``.

On v2 nothing answers ``/api/volume/*`` (see
:mod:`emori_adapter.admin.host_backend`), so this module does what the daemon's
``routers/volume_control_linux.py`` does, and answers in its shapes
(``{"volume", "platform", "device"}``), so :mod:`emori_adapter.admin.audio` and the
phone page stay exactly as they are.

Which of the daemon's two paths, and why: ``VolumeControlLinux`` uses pulsectl
when it can open a PulseAudio connection and ALSA's ``amixer`` otherwise.  As
deployed (a system service with no ``XDG_RUNTIME_DIR``) it took the ALSA path --
the robot answers ``{"volume": 82, "platform": "Linux", "device": "Reachy Mini
Audio"}``, and ``Reachy Mini Audio`` is the bracketed CARD name of
``aplay -l``, not a PipeWire sink description; the PipeWire sink read 0.65 at
the same moment.  So the hardware level an operator has always moved is the ALSA
mixer, and that is what this moves:

* the card: the first ``aplay -l`` card whose name contains ``reachy mini
  audio`` or ``respeaker``, else the default card;
* the controls: from ``amixer -c N scontents``, those with ``pvolume``
  (speaker) / ``cvolume`` (mic), in order;
* once per process, every index-1 control set to 100 % (the daemon does this
  when it builds its ``VolumeControl``, so the index-0 control is the level);
* get: the first control ``amixer sget <ctl>,0`` answers, its ``Left:`` percent;
  set: the first control ``amixer sset <ctl>,0 N%`` accepts.

The test sound.  The daemon plays ``impatient1.wav`` (its own asset) through its
media server -- which EMORI's v1 robots run ``--no-media``, so there it was a
silent no-op, and the v2 shim's ``POST /api/media/play_sound`` is inert too.
Here it is really played, with ``pw-play`` (the robot's PipeWire session, the same
tool the spoken readycheck and the language toggle use; ``EMORI_PLAY_TARGET``
picks the sink), from the daemon's asset when it is on disk, else from a short
tone written once to a private temp file.  Setting the speaker plays it too, as
the daemon's ``/api/volume/set`` does -- except on the boot re-apply, which must
not chirp at whoever is near the robot when it powers up.

Never PortAudio: the hostapp owns capture (see :mod:`emori_adapter.admin.audio`).
"""

from __future__ import annotations

import glob
import importlib.util
import logging
import math
import os
import platform
import re
import struct
import tempfile
import threading
import wave
from dataclasses import dataclass
from typing import Any, Callable, Optional

from emori_adapter.admin import probes

log = logging.getLogger("emori.admin.mixer")

SOUND_CARD_NAMES = ("reachy mini audio", "respeaker")
# The daemon's AUDIO_COMMAND_TIMEOUT.
AUDIO_COMMAND_TIMEOUT_S = 2.0
PLAY_TIMEOUT_S = 10.0

OUTPUT = "output"
INPUT = "input"
_CAPABILITY = {OUTPUT: "pvolume", INPUT: "cvolume"}

_CARD_RE = re.compile(r"card\s+(\d+):\s+[^[]+\[([^\]]+)\]")
_CONTROL_RE = re.compile(r"Simple mixer control '([^']+)',(\d+)")

TEST_SOUND = "impatient1.wav"
# Where the daemon's asset lives on a robot whose emori venv does not carry the
# reachy_mini package: the stock daemon venv (still on disk under v2, only
# disabled) and the v2 native RoboStack environment.
_ASSET_GLOBS = (
    "/venvs/mini_daemon/lib/python3*/site-packages/reachy_mini/assets/" + TEST_SOUND,
    "/opt/emori/ros2/.pixi/envs/default/lib/python3*/site-packages/reachy_mini/assets/"
    + TEST_SOUND,
)

BUSY_SOUND = {"status": "busy",
              "message": "Audio device is currently in use, test sound was skipped."}
PLAYED_SOUND = {"status": "ok", "message": "Test sound played"}


class MixerError(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


@dataclass(frozen=True)
class Card:
    id: Optional[int]   # None = ALSA's default card
    name: str


DEFAULT_CARD = Card(None, "Default")

_lock = threading.Lock()
_card: Optional[Card] = None
_initialized: set = set()
_play_lock = threading.Lock()
_tone_path: Optional[str] = None


def reset() -> None:
    """Forget the cached card (tests; and nothing else needs it)."""

    global _card, _tone_path
    with _lock:
        _card = None
        _initialized.clear()
        stale, _tone_path = _tone_path, None
    if stale:
        try:
            os.unlink(stale)
        except OSError:
            pass


# --------------------------------------------------------------------------
# card + controls
# --------------------------------------------------------------------------
def parse_cards(text: str) -> list[Card]:
    cards: dict[int, Card] = {}
    for line in (text or "").splitlines():
        match = _CARD_RE.search(line)
        if match:
            number = int(match.group(1))
            cards.setdefault(number, Card(number, match.group(2)))
    return list(cards.values())


def _not_started(cmd: probes.Cmd) -> MixerError:
    """aplay/amixer did not run at all: ``alsa_missing`` ONLY when it is not installed.

    readycheck FAILs ``alsa_missing`` with "install alsa-utils"; a tool that is there
    but could not be started (``os_error``: a permission, a full process table) is
    not that, and must not send anyone to apt.
    """

    if cmd.error == "missing":
        return MixerError("alsa_missing")
    return MixerError("mixer_failed")


def find_card() -> Card:
    """The robot's sound card, as the daemon picks it.  Cached once FOUND only:
    a USB card that enumerates after this service started must still be seen."""

    global _card
    with _lock:
        if _card is not None:
            return _card
    listing = probes.run(["aplay", "-l"], timeout_s=AUDIO_COMMAND_TIMEOUT_S)
    if not listing.ran:
        raise _not_started(listing)
    for card in parse_cards(listing.stdout):
        if any(name in card.name.lower() for name in SOUND_CARD_NAMES):
            with _lock:
                _card = card
            return card
    return DEFAULT_CARD


def _amixer(card: Card, *args: str) -> list[str]:
    if card.id is None:
        return ["amixer", *args]
    return ["amixer", "-c", str(card.id), *args]


def parse_controls(text: str, capability: str) -> list[str]:
    """Control names (in order) whose ``scontents`` block mentions ``capability``."""

    controls: list[str] = []
    current: Optional[str] = None
    for line in (text or "").splitlines():
        match = _CONTROL_RE.match(line)
        if match:
            current = match.group(1)
            continue
        if current and capability in line and current not in controls:
            controls.append(current)
    return controls


def controls(card: Card, direction: str) -> list[str]:
    result = probes.run(_amixer(card, "scontents"), timeout_s=AUDIO_COMMAND_TIMEOUT_S)
    if not result.ran:
        raise _not_started(result)
    if not result.ok:
        return []
    return parse_controls(result.stdout, _CAPABILITY[direction])


def _first_ok(card: Card, names: list[str], *args_after: str,
              index: int = 0, verb: str = "sset") -> Optional[probes.Cmd]:
    """``amixer sset A,0 .. || amixer sset B,0 ..`` without a shell."""

    for name in names:
        result = probes.run(_amixer(card, verb, "%s,%d" % (name, index), *args_after),
                            timeout_s=AUDIO_COMMAND_TIMEOUT_S)
        if result.ok:
            return result
    return None


def _initialize(card: Card) -> None:
    """Index-1 controls to 100 %, once per card per process (the daemon's
    ``_initialize_device``).  Best-effort, like the daemon's."""

    with _lock:
        if card.id in _initialized:
            return
        _initialized.add(card.id)
    for direction in (INPUT, OUTPUT):
        try:
            names = controls(card, direction)
        except MixerError:
            return
        if _first_ok(card, names, "100%", index=1) is None and names:
            log.info("could not set the %s index-1 mixer controls of %s to 100%%",
                     direction, card.name)


def parse_percent(text: str) -> int:
    """The daemon's parse: the first ``[NN%]`` on a ``Left:`` line (``Mono:`` as
    a fallback, which the daemon never read).  -1 when there is none."""

    lines = (text or "").splitlines()
    for marker in ("Left:", "Mono:"):
        for line in lines:
            if marker in line and "[" in line:
                for part in line.split("["):
                    if "%" in part:
                        try:
                            return int(part.split("%")[0])
                        except ValueError:
                            return -1
    return -1


def get_volume(direction: str) -> tuple[int, Card]:
    card = find_card()
    _initialize(card)
    result = _first_ok(card, controls(card, direction), verb="sget")
    return (parse_percent(result.stdout) if result is not None else -1), card


def set_volume(direction: str, volume: int) -> tuple[bool, Card]:
    card = find_card()
    _initialize(card)
    level = "%d%%" % max(0, min(100, int(volume)))
    return _first_ok(card, controls(card, direction), level) is not None, card


# --------------------------------------------------------------------------
# test sound
# --------------------------------------------------------------------------
def daemon_test_sound() -> Optional[str]:
    """The daemon's own ``impatient1.wav``, wherever a reachy_mini package lives."""

    try:
        spec = importlib.util.find_spec("reachy_mini")
    except (ImportError, ValueError):
        spec = None
    for base in list(getattr(spec, "submodule_search_locations", None) or []):
        candidate = os.path.join(base, "assets", TEST_SOUND)
        if os.path.isfile(candidate):
            return candidate
    for pattern in _ASSET_GLOBS:
        for hit in sorted(glob.glob(pattern)):
            if os.path.isfile(hit):
                return hit
    return None


def _tone() -> Optional[str]:
    """A 0.3 s 880 Hz tone, written once to a private temp file."""

    global _tone_path
    with _lock:
        if _tone_path and os.path.isfile(_tone_path):
            return _tone_path
    rate, seconds, freq = 44100, 0.3, 880.0
    count, fade = int(rate * seconds), int(rate * 0.02)
    frames = bytearray()
    for i in range(count):
        envelope = min(1.0, i / fade, (count - 1 - i) / fade)
        frames += struct.pack("<h", int(0.35 * 32767 * envelope
                                        * math.sin(2 * math.pi * freq * i / rate)))
    try:
        fd, path = tempfile.mkstemp(prefix="emori-admin-tone-", suffix=".wav")
        with os.fdopen(fd, "wb") as handle:
            with wave.open(handle, "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(bytes(frames))
    except OSError:
        return None
    with _lock:
        _tone_path = path
    return path


def sound_file() -> Optional[str]:
    return daemon_test_sound() or _tone()


def play_argv(path: str) -> list[str]:
    argv = ["pw-play"]
    target = os.environ.get("EMORI_PLAY_TARGET", "").strip()
    if target:
        argv.append("--target=%s" % target)
    argv.append(path)
    return argv


def play_test_sound() -> probes.Http:
    """Play it now (bounded).  A sound already playing -> the daemon's ``busy`` answer."""

    path = sound_file()
    if not path:
        return _http(500, None, error="test_sound_missing")
    if not _play_lock.acquire(blocking=False):
        return _http(200, dict(BUSY_SOUND))
    try:
        result = probes.run(play_argv(path), timeout_s=PLAY_TIMEOUT_S)
    finally:
        _play_lock.release()
    if result.ok:
        return _http(200, dict(PLAYED_SOUND))
    if result.error == "missing":
        return _http(503, None, error="pw_play_missing")
    log.warning("test sound failed (%s)", result.error or "pw-play error")
    return _http(500, None, error="test_sound_failed")


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="admin-chime", daemon=True).start()


# Tests replace this to play inline.
_spawn: Callable[[Callable[[], None]], None] = _start_thread


def _chime() -> None:
    """The sound ``/api/volume/set`` plays after a level change, off the request thread."""

    def play() -> None:
        outcome = play_test_sound()
        if not outcome.ok:
            log.info("the confirmation sound after a speaker change did not play (%s)",
                     outcome.error)

    _spawn(play)


# --------------------------------------------------------------------------
# the daemon's routes, answered here
# --------------------------------------------------------------------------
def _http(status_code: int, payload: Any = None, *, error: str = "") -> probes.Http:
    ok = status_code < 400
    return probes.Http(ok=ok, reached=True, status=status_code, payload=payload,
                       content_type="application/json",
                       error="" if ok else (error or "http_%d" % status_code))


def _volume_body(volume: int, card: Card) -> dict:
    return {"volume": int(volume), "platform": platform.system(), "device": card.name}


def _requested(json_body: Any) -> Optional[int]:
    value = json_body.get("volume") if isinstance(json_body, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        return None
    return value


def request(method: str, path: str, *, json_body: Any = None,
            chime: bool = True) -> probes.Http:
    """Answer one daemon ``/api/volume/*`` call as the daemon would.  Never raises."""

    route = (method.upper(), path)
    try:
        if route in (("GET", "/api/volume/current"),
                     ("GET", "/api/volume/microphone/current")):
            direction = OUTPUT if path == "/api/volume/current" else INPUT
            volume, card = get_volume(direction)
            if volume < 0:
                return _http(500, {"detail": "Failed to get volume"}, error="mixer_failed")
            return _http(200, _volume_body(volume, card))
        if route in (("POST", "/api/volume/set"), ("POST", "/api/volume/microphone/set")):
            volume = _requested(json_body)
            if volume is None:
                return _http(422, {"detail": "volume must be an integer 0-100"})
            direction = OUTPUT if path == "/api/volume/set" else INPUT
            applied, card = set_volume(direction, volume)
            if not applied:
                return _http(500, {"detail": "Failed to set volume"}, error="mixer_failed")
            if direction == OUTPUT and chime:
                _chime()
            return _http(200, _volume_body(volume, card))
        if route == ("POST", "/api/volume/test-sound"):
            return play_test_sound()
    except MixerError as e:
        return _http(503, None, error=e.slug)
    except Exception as e:  # noqa: BLE001 - the recovery tool answers, it does not raise
        log.error("%s %s crashed on the host backend (%s)", method.upper(), path,
                  type(e).__name__)
        return _http(500, None, error="host_error")
    return _http(404, {"detail": "Not Found"})
