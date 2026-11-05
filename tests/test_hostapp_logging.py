"""The hostapp's journal: root logging, the level knob, and no double-emission.

The hostapp process must call ``setup_logging``.  uvicorn configures only its own
loggers, so without it the ROOT logger keeps no handler and Python's ``lastResort``
emits WARNING and above -- a journal of WARNING lines and not one INFO record, which
means every INFO diagnostic in this codebase (an add-on's reconcile line
among them) is invisible on the appliance it was written for.

Both fixtures here are CALLABLES invoked from the test BODY rather than plain
fixtures, and for the same reason: pytest re-installs its own root log handler
and re-assigns ``sys.stderr`` at the start of every test PHASE, i.e. after
fixture setup has finished.  Anything these did during setup would be silently
undone before the first assertion, and the tests would be measuring pytest's
logging instead of the hostapp's.
"""

from __future__ import annotations

import io
import logging
import logging.config
import os
import sys

import pytest

from emori_adapter.logging_setup import (
    DEFAULT_SERVICE_LEVEL,
    JOURNAL_FORMAT,
    LOG_LEVEL_ENV,
    LOG_WIRE_ENV,
    OWN_LOGGER_ROOTS,
    _NOISY_LIBRARIES,
    setup_journal_logging,
)

# Every logger this module -- or uvicorn's own dictConfig -- can touch.  Named
# so a test may configure global logging honestly and still hand the interpreter
# back exactly as it found it.
_TOUCHED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    *_NOISY_LIBRARIES,
    "piper.voice",
    "emori_adapter.logging_setup",
    "emori.hostapp",
    "emori.hostapp.extensions",
    "emori.admin",
)


@pytest.fixture
def bare_root(monkeypatch):
    """Give the body a root logger as bare as the appliance's, then restore it."""

    saved: list[tuple[logging.Logger, list, int, bool]] = []
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    monkeypatch.delenv(LOG_WIRE_ENV, raising=False)

    def clear() -> logging.Logger:
        root = logging.getLogger()
        if not saved:
            for logger in (root, *(logging.getLogger(n) for n in _TOUCHED_LOGGERS)):
                saved.append(
                    (logger, list(logger.handlers), logger.level, logger.propagate)
                )
        root.handlers.clear()
        return root

    try:
        yield clear
    finally:
        for logger, handlers, level, propagate in saved:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate


@pytest.fixture
def journald(monkeypatch):
    """systemd hands a unit's stderr to journald; this stands in for that pipe."""

    def capture() -> io.StringIO:
        stream = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stream)
        return stream

    return capture


def _run_hostapp_main(monkeypatch) -> dict:
    """``emori-hostapp``'s entry point, stopped just short of serving.

    The REAL ``main`` and not a re-implementation of it: the defect was
    precisely that this function never configured logging.  Returns the kwargs
    it would have handed uvicorn.
    """

    import uvicorn

    from emori_adapter.hostapp import server

    served: dict = {}
    monkeypatch.setattr(server, "_build_app", lambda: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: served.update(kw))
    server.main()
    return served


def _run_admin_main(monkeypatch) -> dict:
    """``emori-admin``'s entry point, likewise.

    A deployed unit of its own (deploy/robot/emori-admin.service) that had no
    root handler either -- and the one that must stay readable when everything
    else on the robot is broken.
    """

    import uvicorn

    from emori_adapter.admin import server as admin_server

    served: dict = {}
    monkeypatch.setattr(admin_server, "create_app", lambda: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: served.update(kw))
    # main() applies emori-admin's settings to the process environment (and removes the
    # credentials that service never holds): a copy, so no other test sees the change.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    admin_server.main()
    return served


def test_the_hostapp_entry_point_puts_emori_info_into_the_journal(
    bare_root, journald, monkeypatch
):
    bare_root()
    journal = journald()

    _run_hostapp_main(monkeypatch)
    # An INFO line from a hostapp sub-logger, as an add-on's self-heal writes one.
    logging.getLogger("emori.hostapp.extensions").info(
        "add-on: reconciled dev-2026-07/d028b8ab -> dev-2026-07/51061f2c"
    )

    assert (
        "add-on: reconciled dev-2026-07/d028b8ab -> dev-2026-07/51061f2c"
        in journal.getvalue()
    )


def test_the_journal_format_is_plain_and_never_wraps_a_long_diagnostic(
    bare_root, journald, monkeypatch
):
    """No RichHandler here.

    rich wraps at an ASSUMED terminal width and pads a level column into every
    continuation line; a hostapp under systemd has no terminal at all, and the
    lines worth reading (bundle labels, module ids, safe error codes) are long.
    journald already stamps the time, the unit and the PID, so the format adds
    only what journald cannot know.
    """

    from rich.logging import RichHandler

    root = bare_root()
    journal = journald()
    message = "session cues " + "x" * 400

    _run_hostapp_main(monkeypatch)
    logging.getLogger("emori.hostapp").warning(message)

    assert [handler for handler in root.handlers if isinstance(handler, RichHandler)] == []
    assert root.handlers and root.handlers[0].formatter._fmt == JOURNAL_FORMAT
    lines = [line for line in journal.getvalue().splitlines() if "session cues" in line]
    assert lines == [f"WARNING emori.hostapp: {message}"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", logging.INFO),               # unset: the operating level, not silence
        ("INFO", logging.INFO),
        ("  warning ", logging.WARNING),  # case and stray spaces are an operator's
        ("DEBUG", logging.DEBUG),
        ("ERROR", logging.ERROR),
        # Junk degrades to the default rather than to "no logging at all", which
        # is the failure this whole file exists to prevent.
        ("chatty", logging.INFO),
        ("20", logging.INFO),
    ],
)
def test_the_level_knob_is_honoured_and_junk_degrades_to_info(
    bare_root, journald, monkeypatch, raw, expected
):
    root = bare_root()
    journal = journald()
    monkeypatch.setenv(LOG_LEVEL_ENV, raw)

    _run_hostapp_main(monkeypatch)
    logging.getLogger("emori.hostapp").debug("a debug diagnostic")

    assert root.level == expected
    assert ("a debug diagnostic" in journal.getvalue()) is (expected <= logging.DEBUG)


def test_an_unusable_level_names_itself_on_the_only_surface_that_exists(
    bare_root, journald, monkeypatch
):
    bare_root()
    journal = journald()
    monkeypatch.setenv(LOG_LEVEL_ENV, "chatty")

    _run_hostapp_main(monkeypatch)

    assert "EMORI_LOG_LEVEL='chatty'" in journal.getvalue()
    assert "using INFO" in journal.getvalue()


def test_third_party_wire_loggers_are_muted_at_every_level(
    bare_root, journald, monkeypatch
):
    """Noise at INFO; SESSION CONTENT at DEBUG -- so the clamp is unconditional.

    The root handler captures every logger in the process, and ``websockets``
    logs URLs and frame contents. An operator raising EMORI_LOG_LEVEL is asking about
    EMORI, and a robot's journal is world-readable and survives reboots, so DEBUG must
    not be a way to write session content to disk.
    """

    for level in ("", "INFO", "DEBUG"):
        bare_root()
        journald()
        for name in _NOISY_LIBRARIES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        monkeypatch.setenv(LOG_LEVEL_ENV, level)

        _run_hostapp_main(monkeypatch)

        assert logging.getLogger("emori.hostapp").getEffectiveLevel() == (
            logging.DEBUG if level == "DEBUG" else logging.INFO
        )
        for name in _NOISY_LIBRARIES:
            # piper is the one clamped harder than the rest: the allowlist
            # filter passes WARNING+ from any library so real failures surface,
            # and piper's WARNING carries a phoneme of the sentence being
            # spoken.  Its ERROR (a voice that will not load) still gets through.
            expected = logging.ERROR if name == "piper" else logging.WARNING
            assert logging.getLogger(name).level == expected, (level, name)


def test_the_wire_is_its_own_opt_in_and_never_the_level_knob(
    bare_root, journald, monkeypatch
):
    """A wire trace still has to be POSSIBLE -- behind a knob whose name and
    documentation say what it does, not as a side effect of asking for DEBUG."""

    bare_root()
    journald()
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.NOTSET)
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
    monkeypatch.setenv(LOG_WIRE_ENV, "1")

    _run_hostapp_main(monkeypatch)

    assert [logging.getLogger(n).level for n in _NOISY_LIBRARIES] == [
        logging.NOTSET
    ] * len(_NOISY_LIBRARIES)

    # ...and anything that is not an explicit yes keeps the clamp, because the
    # safe direction for a typo is "muted".
    for value in ("0", "", "false", "maybe"):
        bare_root()
        for name in _NOISY_LIBRARIES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        monkeypatch.setenv(LOG_WIRE_ENV, value)

        _run_hostapp_main(monkeypatch)

        assert logging.getLogger("websockets").level == logging.WARNING, value


def test_piper_never_writes_the_robots_own_speech_into_the_journal(
    bare_root, journald, monkeypatch
):
    """The leak this boundary was widened for.

    ``piper/voice.py:286`` is ``_LOGGER.debug("text=%s, phonemes=%s", text,
    sentence_phonemes)`` on the logger ``piper.voice``, and ``speech/tts.py``
    imports ``PiperVoice`` IN THIS PROCESS -- so with a root handler installed,
    EMORI_LOG_LEVEL=DEBUG printed every string the robot synthesises,
    verbatim, into a journal that is
    world-readable and survives reboots.
    """

    bare_root()
    journal = journald()
    logging.getLogger("piper.voice").setLevel(logging.NOTSET)
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")

    _run_hostapp_main(monkeypatch)
    # The real call site's format string and a real thing the robot says.
    logging.getLogger("piper.voice").debug(
        "text=%s, phonemes=%s",
        "Segons el protocol, has d'avisar la supervisora abans de contenir ningu.",
        ["s", "@", "ɣ"],
    )

    assert "supervisora" not in journal.getvalue()
    assert journal.getvalue() == ""


def test_only_this_codebase_gets_louder_when_the_level_is_raised(
    bare_root, journald, monkeypatch
):
    """The boundary is an ALLOWLIST, not a list of offenders to keep chasing.

    A denylist is only ever as current as the last audit of the dependency tree:
    a sweep of the installed robot venv found ``piper`` emitting
    synthesized text at DEBUG, which nothing on this side had noticed.  So below
    WARNING the journal takes records from ``emori.*`` / ``emori_adapter.*`` and
    from nothing else, and a library nobody has audited yet -- or added next
    year -- is mute without an edit here.
    """

    bare_root()
    journal = journald()
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")

    _run_hostapp_main(monkeypatch)
    # A logger no list in this codebase mentions, at a level the operator asked
    # for, saying the kind of thing a library says about its input.
    unaudited = logging.getLogger("some_future_tts_backend.client")
    unaudited.setLevel(logging.NOTSET)
    unaudited.info("POST /v1/speak text='Hola, tinc mal de cap des de dilluns'")
    unaudited.debug("frame 0: 'em van dir que se sent insegura'")
    logging.getLogger("emori.hostapp").debug("mode=mode_a stage=start")

    assert "mal de cap" not in journal.getvalue()
    assert "insegura" not in journal.getvalue()
    assert "mode=mode_a stage=start" in journal.getvalue()


def test_a_librarys_errors_and_tracebacks_are_never_hidden(
    bare_root, journald, monkeypatch
):
    """The allowlist bounds VERBOSITY, not visibility of failure.

    A robot that cannot reach its broker, or whose ONNX session blew up, has to
    say so on the only surface it has -- so WARNING and above passes from
    anybody, including from the loggers that are pinned.
    """

    bare_root()
    journal = journald()

    _run_hostapp_main(monkeypatch)
    logging.getLogger("httpx").warning("connect timeout to orion.example.local:1026")
    try:
        raise RuntimeError("onnxruntime session is gone")
    except RuntimeError:
        logging.getLogger("piper.voice").exception("synthesis failed")

    assert "connect timeout to orion.example.local:1026" in journal.getvalue()
    assert "synthesis failed" in journal.getvalue()
    assert "RuntimeError: onnxruntime session is gone" in journal.getvalue()


def test_the_wire_knob_also_opens_the_libraries_the_filter_holds_back(
    bare_root, journald, monkeypatch
):
    """One knob, both mechanisms: clamping the logger and filtering the handler
    have to agree, or EMORI_LOG_WIRE=1 would half-work and a dev triage session
    would silently miss the frames it was turned on for."""

    bare_root()
    journal = journald()
    logging.getLogger("piper.voice").setLevel(logging.NOTSET)
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
    monkeypatch.setenv(LOG_WIRE_ENV, "1")

    _run_hostapp_main(monkeypatch)
    logging.getLogger("piper.voice").debug("text=%s", "hola")

    assert "text=hola" in journal.getvalue()


def test_the_allowlist_covers_every_logger_name_this_codebase_actually_uses():
    """A boundary drawn on names is only as good as the names being right.

    Both prefixes are load-bearing and neither is a prefix of the other:
    ``emori.*`` is what the modules pass to ``getLogger`` explicitly, and
    ``emori_adapter.*`` is what ``__name__`` produces in the few that do not.
    """

    from emori_adapter.logging_setup import _is_own_logger

    assert set(OWN_LOGGER_ROOTS) == {"emori", "emori_adapter"}
    for name in ("emori.hostapp.extensions.addon", "emori.admin.probes",
                 "emori.fiware", "emori", "emori_adapter.logging_setup"):
        assert _is_own_logger(name), name
    # ...and a package that merely STARTS with the letters is not us.
    for name in ("emoriX.client", "emori_sdk.wire", "piper.voice", "httpx", "root"):
        assert not _is_own_logger(name), name


def test_a_root_logger_someone_else_configured_is_never_clobbered(
    bare_root, journald, monkeypatch
):
    """An embedder (or pytest itself) owning root is left completely alone:
    adding a handler is how one journal line becomes two, and removing theirs is
    how their logs disappear."""

    root = bare_root()
    journal = journald()
    theirs = logging.StreamHandler(io.StringIO())
    root.addHandler(theirs)
    root.setLevel(logging.ERROR)

    assert setup_journal_logging() is False

    assert root.handlers == [theirs]
    assert root.level == logging.ERROR
    assert journal.getvalue() == ""


def test_configuring_logging_never_raises_at_startup(bare_root, monkeypatch):
    """A service that cannot log still has to run."""

    root = bare_root()

    def unusable(*args, **kwargs):
        raise OSError("no stream for you")

    monkeypatch.setattr(logging, "StreamHandler", unusable)

    assert setup_journal_logging() is False
    assert root.handlers == []


def test_uvicorns_own_lines_are_not_double_logged(bare_root, journald, monkeypatch):
    """Asserted against uvicorn's ACTUAL config, then against its actual effect.

    uvicorn.run applies ``LOGGING_CONFIG`` through dictConfig after this process
    has configured root.  Two things have to hold for the journal to stay
    readable: that config must not carry a ``root`` key (dictConfig would
    replace the handler installed above), and uvicorn's own loggers must not
    propagate into root (every startup and access line would appear twice).
    """

    from uvicorn.config import LOGGING_CONFIG

    assert "root" not in LOGGING_CONFIG
    assert LOGGING_CONFIG["disable_existing_loggers"] is False
    assert LOGGING_CONFIG["loggers"]["uvicorn"]["propagate"] is False
    assert LOGGING_CONFIG["loggers"]["uvicorn.access"]["propagate"] is False
    # uvicorn.error carries no handler of its own: its records reach uvicorn's
    # handler by propagation and stop there, at uvicorn's propagate=False.
    assert "handlers" not in LOGGING_CONFIG["loggers"]["uvicorn.error"]

    root = bare_root()
    journal = journald()
    _run_hostapp_main(monkeypatch)
    # Both handler sets now write to this same stream, which is exactly the
    # condition under which a double-emission is visible.
    logging.config.dictConfig(LOGGING_CONFIG)

    logging.getLogger("uvicorn.error").info("Application startup complete.")
    logging.getLogger("emori.hostapp").info("hub ready")

    assert journal.getvalue().count("Application startup complete.") == 1
    # ...and the root handler this process installed survived uvicorn's config.
    assert journal.getvalue().count("hub ready") == 1
    assert root.level == DEFAULT_SERVICE_LEVEL


# ---------------------------------------------------------------------------
# uvicorn's access log: the largest content channel either service had
# ---------------------------------------------------------------------------
def test_both_services_ship_with_the_http_access_log_off(bare_root, journald,
                                                         monkeypatch):
    """It printed the robot's own speech, one line per request.

    uvicorn's access formatter renders ``get_path_with_query_string(scope)`` --
    path AND raw query -- onto stdout, which under systemd is journald.  On this
    appliance that is ``GET /api/tts?lang=ca&text=<the exact sentence the robot
    is about to say>`` and ``POST /api/stt?lang=en&grammar=<the menu words>``.
    It also makes the journal unreadable: the kiosk polls /api/addon/status
    continuously, so the journal runs at dozens of access lines a minute around
    the one INFO record being looked for.
    """

    bare_root()
    journald()

    assert _run_hostapp_main(monkeypatch)["access_log"] is False
    assert _run_admin_main(monkeypatch)["access_log"] is False


def test_the_access_log_comes_back_only_under_the_wire_knob(bare_root, journald,
                                                            monkeypatch):
    """Same opt-in as third-party wire logging, because it is the same kind of thing:
    a URL somebody's words were carried in.  Documented never-for-a-deployed-robot."""

    bare_root()
    journald()
    monkeypatch.setenv(LOG_WIRE_ENV, "1")

    assert _run_hostapp_main(monkeypatch)["access_log"] is True
    assert _run_admin_main(monkeypatch)["access_log"] is True

    # ...and raising the ORDINARY verbosity knob is not a way to turn it on.
    monkeypatch.delenv(LOG_WIRE_ENV)
    monkeypatch.setenv(LOG_LEVEL_ENV, "DEBUG")
    assert _run_hostapp_main(monkeypatch)["access_log"] is False


def test_access_log_false_is_actually_how_the_installed_uvicorn_suppresses_it(
    bare_root, journald, monkeypatch
):
    """Verified against uvicorn (0.48.0) rather than assumed.

    ``Config.configure_logging`` answers ``access_log=False`` by clearing the
    ``uvicorn.access`` handlers AND setting ``propagate=False``, and the HTTP
    protocols read ``self.access_logger.hasHandlers()`` once per connection.
    Both halves matter here: without ``propagate=False`` the logger would find
    the ROOT handler this process now installs, ``hasHandlers()`` would answer
    True again, and the line would come back through the journal -- which is
    exactly the interaction that makes ``access_log=False`` the right lever and
    a ``log_config`` edit the wrong one.
    """

    import uvicorn

    root = bare_root()
    journal = journald()
    # The production ORDER, which is the whole point: this process installs the
    # root handler first, and uvicorn configures itself afterwards.
    _run_hostapp_main(monkeypatch)
    assert root.handlers, "the root handler has to be in place for this to mean anything"

    config = uvicorn.Config(object(), access_log=False)
    config.configure_logging()

    access = logging.getLogger("uvicorn.access")
    assert access.propagate is False
    assert access.hasHandlers() is False     # ...even with root carrying ours
    # ...while errors and tracebacks keep their route out.
    assert logging.getLogger("uvicorn.error").hasHandlers() is True

    # And the line the protocol would have emitted goes nowhere.
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.4:51234", "GET",
                "/api/tts?lang=ca&text=Bon%20dia,%20com%20et%20puc%20ajudar",
                "1.1", 200)
    assert journal.getvalue() == ""


# ---------------------------------------------------------------------------
# emori-admin: the other deployed unit
# ---------------------------------------------------------------------------
def test_the_admin_entry_point_puts_its_info_into_the_journal_too(
    bare_root, journald, monkeypatch
):
    """``emori-admin`` is a deployed unit (deploy/robot/emori-admin.service) and
    had no root handler either.  It is the RECOVERY tool -- reached for exactly
    when the daemon, the network or the hostapp is already broken -- so its
    diagnostics being invisible is the worst version of this defect."""

    bare_root()
    journal = journald()

    _run_admin_main(monkeypatch)
    logging.getLogger("emori.admin").info("AP rebrand: already_current")

    assert "AP rebrand: already_current" in journal.getvalue()


def test_the_admin_reads_its_level_knob_after_loading_dot_env(
    bare_root, journald, monkeypatch
):
    """Ordering, and it differs from the hostapp's on purpose.

    hostapp/server.py imports ``emori_adapter.config`` at MODULE scope, so its
    .env is loaded long before main() runs; emori-admin loads it inside main()
    (``site_settings.load_admin_environment``, which leaves out the credentials it
    never holds), so configuring logging first would read EMORI_LOG_LEVEL out of an
    environment that does not have it yet. The unit no longer hands it .env at all,
    so nothing else would put the knob there.
    """

    import dotenv

    import emori_adapter

    root = bare_root()
    journald()
    # The loader finds the install's .env walking up from the package; this box may
    # have none, so the first place it looks has one.
    planted = os.path.join(os.path.dirname(os.path.abspath(emori_adapter.__file__)), ".env")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile",
                        lambda path: path == planted or real_isfile(path))
    monkeypatch.setattr(
        dotenv, "dotenv_values",
        lambda path=None, *a, **kw: {LOG_LEVEL_ENV: "DEBUG"} if path == planted else {})

    _run_admin_main(monkeypatch)

    assert root.level == logging.DEBUG


def test_making_the_admin_visible_does_not_publish_the_sites_wifi_password(
    bare_root, journald, monkeypatch
):
    """Why the clamp is not optional in THIS process.

    ``network.connect`` hands the daemon ``POST /wifi/connect?ssid=...&
    password=...`` -- the site's key, in a query string -- and httpx logs
    ``request.url`` in full at INFO, which is the DEFAULT level.  So installing a
    root handler here without the boundary would have written a customer's Wi-Fi
    password into a world-readable journal on the first join, with nobody asking
    for anything unusual.  probes.py already refuses to let exception text out
    for the same reason; this closes the other end of it.
    """

    bare_root()
    journal = journald()
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    _run_admin_main(monkeypatch)
    # httpx/_client.py:1025, verbatim, with the URL network.connect builds.
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"', "POST",
        "http://127.0.0.1:8000/wifi/connect?ssid=Guest&password=example-pass",
        "HTTP/1.1", 200, "OK")

    assert journal.getvalue() == ""
    assert "example-pass" not in journal.getvalue()
