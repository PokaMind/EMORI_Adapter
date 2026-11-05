"""Logging for the two shapes this codebase runs in: a terminal, and a unit.

:func:`setup_logging` is for a HUMAN at a console (``emori-run``, the replay
harness): rich draws the columns.

:func:`setup_journal_logging` is for a systemd service whose only reader is
``journalctl`` -- both of the deployed units, ``emori-hostapp`` and
``emori-admin`` -- and it deliberately does NOT use rich.  Two reasons, both
measured on the appliance: journald already stamps every line with a timestamp,
the unit and the PID, so rich's own time column is duplicated noise; and
``RichHandler`` wraps at an ASSUMED terminal width and pads a level column into
every continuation line, which mangles exactly the long diagnostic lines
(bundle labels, safe error codes, module ids) a screenless robot is debugged
from.  Keeping the two apart also means the service format can never regress
the CLI's, or the other way round.
"""

from __future__ import annotations

import logging
import os

from rich.logging import RichHandler

# The service's verbosity knob.  Named EMORI_* like every other deployment
# switch so it can live in /opt/emori/.env with the rest of them.
LOG_LEVEL_ENV = "EMORI_LOG_LEVEL"
# The separate, deliberately awkward opt-in for third-party WIRE logging (and,
# with it, uvicorn's access log).  See :data:`OWN_LOGGER_ROOTS` for why raising
# EMORI_LOG_LEVEL must not do this, and :func:`access_log_enabled` for the other
# half of what it governs.
LOG_WIRE_ENV = "EMORI_LOG_WIRE"
# INFO by default because the diagnostics an appliance is operated from are
# INFO: "bundle: reconciled <old> -> <new>" style lines.  Without a
# configured root logger, Python's lastResort handler emits WARNING and above only
# and every one of those lines is dropped.
DEFAULT_SERVICE_LEVEL = logging.INFO
# journald knows the time, the unit and the PID; this carries only what it
# cannot know.  One line per record, no wrapping, no padding.
JOURNAL_FORMAT = "%(levelname)s %(name)s: %(message)s"

# ---------------------------------------------------------------------------
# What EMORI_LOG_LEVEL is allowed to make louder
# ---------------------------------------------------------------------------
# The handler is on ROOT, so it sees EVERY logger in the process, not just this
# codebase's.  That is the point (uvicorn aside, nothing else would be logged at
# all) and it is also the hazard, because raising the level raises it for
# libraries that log their INPUT:
#
# * ``websockets`` logs its handshake as "> GET <path-with-query>", then every frame
#   as "< TEXT {...}" / "> BINARY ...", i.e. the leading characters of each frame.
# * ``httpx`` logs the full outbound URL at INFO -- and, in emori-admin, ``POST /wifi/connect?ssid=...&
#   password=...``, the site's Wi-Fi key.
# * ``piper.voice`` logs ``text=%s, phonemes=%s`` at DEBUG: every string the
#   robot synthesises, verbatim.
#
# Components that handle such data are built to keep exactly that out of the logs; a verbosity knob must not be able to undo it,
# least of all on an appliance whose journal is world-readable and persists
# across boots.
#
# So the boundary is an ALLOWLIST, not a list of offenders to keep chasing:
# below WARNING, only THIS codebase's loggers are written down.  A new
# dependency, or a new logger inside an existing one, is mute by default and
# nobody has to notice it.  WARNING and above still passes from anybody --
# a library's error and its traceback are exactly what an operator needs.
OWN_LOGGER_ROOTS = ("emori", "emori_adapter")
# Loggers pinned at WARNING as well as filtered.  Redundant for SAFETY (the
# filter above is the boundary) and kept for two other reasons: `setLevel` makes
# `logger.debug(...)` return before it builds a LogRecord, which on a CM4 matters
# on per-frame and per-request paths; and the list is the written record of a
# sweep.  That sweep grepped the INSTALLED robot venv -- piper, vosk,
# websockets, httpx, httpcore, python_multipart, PIL, numpy,
# uvicorn, cv2, fastapi, starlette, anyio, pydantic, dotenv, jwt, cryptography,
# yaml, typer, rich, h11, wsproto, charset_normalizer -- for
# ``logging.getLogger`` and for every ``.debug(``/``.info(`` call site.  What it
# found: piper emits synthesized TEXT (voice.py:286); websockets/httpx/httpcore
# emit request wire; PIL, python_multipart, charset_normalizer and dotenv emit
# only shape (chunk ids, byte offsets, encoding guesses); vosk logs through the
# ROOT logger but only in ``vosk.transcriber``, a CLI module nothing here
# imports, and its native Kaldi log is already off (speech_api sets
# ``SetLogLevel(-1)``); numpy, cv2, fastapi, starlette, anyio,
# pydantic, jwt, cryptography, yaml, typer, h11 and wsproto have no
# logging at all.
_NOISY_LIBRARIES = (
    # piper is clamped to ERROR, not WARNING, by _clamp_libraries: its
    # phoneme_ids.py:190 logs the offending PHONEME at WARNING for every glyph
    # outside the voice's map, and the allowlist filter deliberately passes
    # WARNING+ from any library so a real library failure is never hidden.  A
    # phoneme is a fragment of what the robot is about to say.
    "piper",
    "httpx",
    "httpcore",
    "websockets",
    "websockets.client",
    "websockets.server",
    "python_multipart",
    "multipart",
    "PIL",
    "charset_normalizer",
)
# Values that mean "yes" for the wire knob.  Anything else -- unset, "0", "",
# "false", a typo -- keeps the clamp, which is the safe direction.
_WIRE_ENABLED = {"1", "true", "yes", "on"}


def wire_logging_requested() -> bool:
    """Whether the operator asked for third-party WIRE logging.

    The knob that opens third-party wire logging is also the knob that opens uvicorn's
    access log (see :func:`access_log_enabled`): both write down the same class
    of thing -- a URL somebody's words were carried in -- so they share one
    documented, deliberately awkward opt-in rather than two.
    """

    return os.environ.get(LOG_WIRE_ENV, "").strip().lower() in _WIRE_ENABLED


def access_log_enabled() -> bool:
    """Whether uvicorn may log one line per HTTP request.  Default: no.

    uvicorn's access log formats ``get_path_with_query_string(scope)`` -- the
    path AND the raw query -- and prints it on stdout, which under systemd is
    journald.  On this appliance that is ``GET /api/tts?lang=ca&text=<the exact
    sentence the robot is about to say>`` and ``POST /api/stt?lang=en&grammar=
    <the menu words>``: the robot's speech, written to a world-readable journal
    that survives reboots, from a component that has no idea it is doing it.

    It is also what makes the journal unreadable.  The kiosk polls
    ``/api/addon/hints`` continuously, so a screenless robot being debugged
    over SSH produces dozens of access lines a minute and the one INFO record
    that matters scrolls away.

    Errors are unaffected: uvicorn's own ``uvicorn.error`` logger keeps its
    handler and its level, so startup, shutdown and every unhandled-exception
    traceback still reach the journal.  Only the per-request line goes.
    """

    return wire_logging_requested()


def _is_own_logger(name: str) -> bool:
    """Is ``name`` one of THIS codebase's loggers?  (``emori.*``/``emori_adapter.*``.)"""

    return any(name == root or name.startswith(root + ".") for root in OWN_LOGGER_ROOTS)


class OwnCodeFilter(logging.Filter):
    """Below WARNING, pass only this codebase's records.  See :data:`OWN_LOGGER_ROOTS`.

    On the HANDLER rather than on each logger, because that is what makes it a
    boundary instead of a list: a dependency added next year, or a logger added
    inside one already installed, is silent here without anybody editing this
    file.
    """

    def __init__(self, *, third_party: bool = False) -> None:
        super().__init__()
        # Snapshotted at install time, not read per record: a service reads its
        # env once, and a filter on a hot path should not.
        self.third_party = third_party

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return self.third_party or _is_own_logger(record.name)


def _clamp_noisy_libraries() -> None:
    """Pin the measured-verbose third-party loggers at WARNING.  See above."""

    if wire_logging_requested():
        return
    for name in _NOISY_LIBRARIES:
        # ERROR for piper: see the note beside it in _NOISY_LIBRARIES -- its
        # WARNING carries a phoneme of the sentence, and WARNING is exactly the
        # level the filter lets through from any library.  A piper ERROR (a
        # voice that will not load) still reaches the journal.
        level = logging.ERROR if name == "piper" else logging.WARNING
        logging.getLogger(name).setLevel(level)


def setup_logging(level: str = "INFO") -> None:
    handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
    # Same boundary as the service: `emori-run --log-level DEBUG` on a dev box is
    # a human asking about EMORI, not about a media socket's frames or
    # about what Piper was handed to say.
    handler.addFilter(OwnCodeFilter(third_party=wire_logging_requested()))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[handler],
    )
    _clamp_noisy_libraries()


def _resolve_level(value: object) -> int | None:
    """A level NAME (any case, any surrounding space) -> its number, else None."""

    if not isinstance(value, str):
        return None
    name = value.strip().upper()
    if not name:
        return None
    level = logging.getLevelName(name)
    # getLevelName answers the string "Level BANANA" for anything it does not
    # know, so the type IS the validity check.
    return level if isinstance(level, int) else None


def setup_journal_logging(level: str = "") -> bool:
    """Configure ROOT logging for a service process.  Never raises.

    Returns whether this call installed the root handler, which is False both
    when root was already configured and when configuring it failed.

    Root, not ``emori_adapter``: uvicorn configures only its own loggers and
    sets ``propagate`` False on them, so without a root handler nothing else in
    the process is logged at all -- and setting a level on one package would
    still leave the records with nowhere to go.

    An already-configured root is left completely alone.  Adding a second
    handler is how one journal line becomes two, and removing an embedder's (or
    pytest's) handler is how their logs disappear; the appliance this exists for
    has neither, which is precisely why its INFO was invisible.
    """

    try:
        requested = level or os.environ.get(LOG_LEVEL_ENV, "")
        resolved = _resolve_level(requested)
        root = logging.getLogger()
        if root.handlers:
            return False
        # stderr: systemd routes a unit's stderr straight into the journal, and
        # stdout is where uvicorn's own access log goes.
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(JOURNAL_FORMAT))
        # Unconditional, DEBUG included: see OWN_LOGGER_ROOTS.  Raising the
        # adapter's verbosity must never also open third-party wire logging or
        # Piper's input, because those carry URLs, frame contents and every sentence
        # the robot speaks.
        handler.addFilter(OwnCodeFilter(third_party=wire_logging_requested()))
        root.addHandler(handler)
        root.setLevel(DEFAULT_SERVICE_LEVEL if resolved is None else resolved)
        _clamp_noisy_libraries()
        if resolved is None and isinstance(requested, str) and requested.strip():
            # Said AFTER the handler exists, so the operator who set the knob
            # can read on the appliance's only surface why it did nothing.
            logging.getLogger(__name__).warning(
                "%s=%r is not a logging level; using %s",
                LOG_LEVEL_ENV,
                requested,
                logging.getLevelName(DEFAULT_SERVICE_LEVEL),
            )
        return True
    except Exception:  # noqa: BLE001 - logging never keeps a service from starting
        return False
