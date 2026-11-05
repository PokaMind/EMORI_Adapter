"""The add-on seam: ``hostapp/extensions.py`` and the hostapp built with no add-on.

The kiosk runs complete with nothing installed. These tests pin the defaults every hook
falls back to, the discovery rules (one add-on, ``EMORI_ADDONS=off``, a broken add-on never
takes the kiosk down), and what the hostapp does when no add-on is there to answer.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from emori_adapter.hostapp import extensions, server
from emori_adapter.hostapp.driver import SessionInfo
from emori_adapter.hostapp.extensions import (
    BADGE_OK,
    BADGE_PENDING,
    BADGE_REFUSED,
    HostExtension,
    claim_timeout_s,
    contributed,
    extension_class,
    load_extension,
)
from emori_adapter.hostapp.gestures import GestureDirection
from emori_adapter.robot.client import DryRunRobotClient


@pytest.fixture(autouse=True)
def _badges_on_the_sample_roster(sample_roster):
    """The badge tests scan ``EMORI:anna-001`` from a copy of the sample roster."""


def _entry(name: str, load):
    return SimpleNamespace(name=name, load=load)


def _discover_as(monkeypatch, *entries) -> None:
    """Make these the installed add-ons, with discovery switched on."""

    monkeypatch.delenv("EMORI_ADDONS", raising=False)
    monkeypatch.setattr(extensions.metadata, "entry_points",
                        lambda group=None: list(entries))


class _Addon(HostExtension):
    name = "fake"

    @classmethod
    def settings_registry(cls):
        return {"registry": True}

    @classmethod
    def echo(cls, *args, **kwargs):
        return args, kwargs

    @classmethod
    def broken(cls):
        raise RuntimeError("add-on fault")


# --- the defaults -------------------------------------------------------------------

def test_a_bare_extension_accepts_every_badge_and_never_holds_the_robot():
    ext = HostExtension()

    assert ext.claim_badge("EMORI:anna-001") == BADGE_OK
    assert ext.bind_session("s1") is True
    assert ext.session_opened("s1") is True
    assert ext.selection_reserved() is False
    assert ext.busy() is False
    assert ext.status() is None
    assert list(ext.hint_lines("en")) == []
    assert ext.admin_api() is None
    assert ext.on_head_turn(GestureDirection.LEFT) is None
    assert ext.session_ended("s1") is None
    assert ext.session_ended() is None


def test_the_badge_answers_are_three_distinct_words():
    assert len({BADGE_OK, BADGE_REFUSED, BADGE_PENDING}) == 3


def test_the_lifespan_hooks_are_awaitable_no_ops():
    ext = HostExtension()

    assert asyncio.run(ext.start()) is None
    assert asyncio.run(ext.shutdown()) is None


# --- discovery ----------------------------------------------------------------------

def test_no_installed_add_on_means_the_defaults(monkeypatch):
    _discover_as(monkeypatch)

    assert extension_class() is None
    assert type(load_extension()) is HostExtension
    assert contributed("settings_registry") is None


@pytest.mark.parametrize("value", ["off", "OFF", " off ", "0", "false"])
def test_addons_off_skips_discovery_entirely(monkeypatch, value):
    def explode(**_kwargs):
        raise AssertionError("discovery ran with EMORI_ADDONS=off")

    monkeypatch.setattr(extensions.metadata, "entry_points", explode)
    monkeypatch.setenv("EMORI_ADDONS", value)

    assert extension_class() is None
    assert type(load_extension()) is HostExtension
    assert contributed("settings_registry") is None


def test_an_installed_add_on_is_loaded_and_instantiated(monkeypatch):
    _discover_as(monkeypatch, _entry("fake", lambda: _Addon))

    assert extension_class() is _Addon
    assert isinstance(load_extension(), _Addon)


def test_only_the_first_add_on_by_name_loads(monkeypatch, caplog):
    class _Other(HostExtension):
        pass

    _discover_as(monkeypatch, _entry("zeta", lambda: _Other), _entry("alpha", lambda: _Addon))

    with caplog.at_level(logging.WARNING, logger="emori.hostapp.extensions"):
        assert extension_class() is _Addon

    assert "loading only alpha" in caplog.text


def test_an_add_on_whose_import_raises_leaves_the_defaults(monkeypatch, caplog):
    def broken_import():
        raise ImportError("missing native library")

    _discover_as(monkeypatch, _entry("fake", broken_import))

    with caplog.at_level(logging.WARNING, logger="emori.hostapp.extensions"):
        assert extension_class() is None
        loaded = load_extension()

    assert type(loaded) is HostExtension
    assert "failed to import" in caplog.text
    assert contributed("settings_registry") is None


def test_an_add_on_whose_constructor_raises_leaves_the_defaults(monkeypatch, caplog):
    class _BadInit(HostExtension):
        def __init__(self) -> None:
            raise RuntimeError("cannot start")

    _discover_as(monkeypatch, _entry("fake", lambda: _BadInit))

    with caplog.at_level(logging.WARNING, logger="emori.hostapp.extensions"):
        assert extension_class() is _BadInit
        loaded = load_extension()

    assert type(loaded) is HostExtension
    assert "failed to start" in caplog.text


def test_a_broken_discovery_environment_runs_the_kiosk_without_an_add_on(monkeypatch):
    monkeypatch.delenv("EMORI_ADDONS", raising=False)

    def explode(**_kwargs):
        raise OSError("metadata unreadable")

    monkeypatch.setattr(extensions.metadata, "entry_points", explode)

    assert extension_class() is None
    assert type(load_extension()) is HostExtension


# --- contributed --------------------------------------------------------------------

def test_contributed_calls_a_classmethod_the_add_on_publishes(monkeypatch):
    _discover_as(monkeypatch, _entry("fake", lambda: _Addon))

    assert contributed("settings_registry") == {"registry": True}
    assert contributed("echo", 1, key="v") == ((1,), {"key": "v"})


def test_contributed_is_none_for_a_hook_the_add_on_does_not_publish(monkeypatch):
    _discover_as(monkeypatch, _entry("fake", lambda: _Addon))

    assert contributed("readiness_gates") is None
    assert contributed("name") is None            # present but not callable


def test_contributed_swallows_a_hook_that_raises(monkeypatch, caplog):
    _discover_as(monkeypatch, _entry("fake", lambda: _Addon))

    with caplog.at_level(logging.WARNING, logger="emori.hostapp.extensions"):
        assert contributed("broken") is None

    assert "add-on hook broken failed" in caplog.text


# --- claim_timeout_s ----------------------------------------------------------------

def test_the_claim_window_defaults_to_sixty_seconds(monkeypatch):
    monkeypatch.delenv("EMORI_TRAINING_NAV_TIMEOUT_S", raising=False)

    assert claim_timeout_s() == 60.0


@pytest.mark.parametrize("raw,expected", [
    ("10", 10.0), ("90", 90.0), ("600", 600.0), ("12.5", 12.5), (" 45 ", 45.0),
])
def test_the_claim_window_is_deployment_tunable(monkeypatch, raw, expected):
    monkeypatch.setenv("EMORI_TRAINING_NAV_TIMEOUT_S", raw)

    assert claim_timeout_s() == expected


@pytest.mark.parametrize("raw", ["9.9", "0", "-5", "601", "1e9", "soon", "", "nan", "inf"])
def test_a_claim_window_outside_ten_to_six_hundred_keeps_the_default(monkeypatch, raw):
    monkeypatch.setenv("EMORI_TRAINING_NAV_TIMEOUT_S", raw)

    assert claim_timeout_s() == 60.0


# --- the hostapp with no add-on -----------------------------------------------------

def _app(monkeypatch, *, extension=None, gesture: bool = False, lang_toggle: str = ""):
    if lang_toggle:
        monkeypatch.setenv("EMORI_LANG_TOGGLE", lang_toggle)
    else:
        monkeypatch.delenv("EMORI_LANG_TOGGLE", raising=False)
    monkeypatch.setattr(server, "DEMO", True)
    overrides = {
        "robot_client": DryRunRobotClient(),
        "fiware_factory": None,
        "idle": False,
        "watchdog": False,
        "gesture": gesture,
        "tts": None,
        "stt": None,
    }
    if extension is not None:
        overrides["extension"] = extension
    return server._build_app(overrides=overrides)


def test_the_hostapp_without_an_add_on_carries_the_default_extension(monkeypatch):
    app = _app(monkeypatch)

    assert type(app.state.extension) is HostExtension


def test_a_badge_claim_is_accepted_and_training_starts_with_no_add_on(monkeypatch):
    app = _app(monkeypatch)

    with TestClient(app) as client:
        claim = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        assert claim.status_code == 202
        assert claim.json() == {"accepted": True}

        minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert minted.status_code == 200
        opened = client.get(minted.json()["session_url"])
        assert opened.status_code == 200

        assert app.state.driver.tracker.active is True


def test_a_badge_already_taken_into_a_session_is_refused_by_the_kiosk_itself(monkeypatch):
    app = _app(monkeypatch)

    with TestClient(app) as client:
        first = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        repeat = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        again = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})

    assert first.status_code == 202
    assert repeat.status_code == 202                 # idempotent until identify takes it
    assert again.status_code == 409
    assert again.json() == {"error_code": "qr_already_claimed"}


def test_an_unusable_decode_is_rejected_before_any_add_on_is_asked(monkeypatch):
    app = _app(monkeypatch)

    with TestClient(app) as client:
        response = client.post("/api/badge/claim", json={"payload": ""})

    assert response.status_code == 400
    assert response.json() == {"error_code": "invalid_qr"}


def test_a_head_turn_with_no_add_on_is_a_no_op(monkeypatch):
    app = _app(monkeypatch, gesture=True)
    driver = app.state.driver
    ticker = app.state.gesture_ticker
    assert callable(ticker.on_direction)

    for direction in (GestureDirection.LEFT, GestureDirection.RIGHT):
        ticker.on_direction(direction)

    assert driver.pop_cmd() is None
    assert driver.tracker.active is False
    assert driver.counters.get("lang_toggle", 0) == 0


def test_the_addon_status_route_is_503_with_no_add_on(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        response = client.get("/api/addon/status")

    assert response.status_code == 503
    assert response.json() == {"error_code": "addon_unavailable"}


def test_the_addon_hints_route_answers_no_lines_with_no_add_on(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        response = client.get("/api/addon/hints")

    assert response.status_code == 200
    assert response.json() == {"lines": []}


def test_an_idle_robot_with_no_add_on_is_idle(monkeypatch):
    app = _app(monkeypatch, gesture=True, lang_toggle="1")
    ticker = app.state.gesture_ticker

    assert ticker._toggle_armed() is True              # noqa: SLF001 - the idle gate


def test_a_training_session_makes_the_robot_not_idle(monkeypatch):
    app = _app(monkeypatch, gesture=True, lang_toggle="1")
    ticker = app.state.gesture_ticker

    app.state.driver.start_session(SessionInfo(session_id="s1", person_id="p1", lang="ca"))

    assert ticker._toggle_armed() is False             # noqa: SLF001


def test_an_add_on_that_raises_in_attach_leaves_training_available(monkeypatch):
    class _BadAttach(HostExtension):
        def attach(self, ctx) -> None:
            raise RuntimeError("cannot attach")

        def claim_badge(self, payload: str) -> str:
            return BADGE_REFUSED                       # would refuse, were it still in charge

    app = _app(monkeypatch, extension=_BadAttach())

    assert type(app.state.extension) is HostExtension
    with TestClient(app) as client:
        claim = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert minted.status_code == 200
        client.get(minted.json()["session_url"])

        assert app.state.driver.tracker.active is True
    assert claim.status_code == 202


def test_an_add_on_that_raises_in_start_leaves_training_available(monkeypatch):
    class _BadStart(HostExtension):
        async def start(self) -> None:
            raise RuntimeError("cannot start")

    app = _app(monkeypatch, extension=_BadStart())

    with TestClient(app) as client:                    # the lifespan runs start()
        claim = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})

    assert claim.status_code == 202


def test_an_add_on_that_refuses_a_badge_is_obeyed(monkeypatch):
    class _Refusing(HostExtension):
        def claim_badge(self, payload: str) -> str:
            return BADGE_REFUSED

    app = _app(monkeypatch, extension=_Refusing())

    with TestClient(app) as client:
        response = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})

    assert response.status_code == 409
    assert response.json() == {"error_code": "training_unavailable"}


def test_an_add_on_status_and_hints_are_served(monkeypatch):
    class _Reporting(HostExtension):
        def status(self):
            return {"mode": "idle", "enabled": {"x": True}}

        def hint_lines(self, lang: str):
            return [f"hint-{lang}"]

    app = _app(monkeypatch, extension=_Reporting())

    with TestClient(app) as client:
        status = client.get("/api/addon/status")
        hints = client.get("/api/addon/hints")

    assert status.status_code == 200
    assert status.json() == {"mode": "idle", "enabled": {"x": True}}
    lang = app.state.lang_state.current()
    assert hints.json() == {"lines": [f"hint-{lang}"]}


def test_a_busy_add_on_makes_the_robot_not_idle_and_a_raising_one_too(monkeypatch):
    class _Busy(HostExtension):
        def busy(self) -> bool:
            return True

    class _Unreadable(HostExtension):
        def busy(self) -> bool:
            raise RuntimeError("state unreadable")

    busy = _app(monkeypatch, extension=_Busy(), gesture=True, lang_toggle="1")
    assert busy.state.gesture_ticker._toggle_armed() is False        # noqa: SLF001

    unreadable = _app(monkeypatch, extension=_Unreadable(), gesture=True, lang_toggle="1")
    assert unreadable.state.gesture_ticker._toggle_armed() is False  # noqa: SLF001


def test_an_add_on_that_raises_in_status_or_hints_never_breaks_the_routes(monkeypatch):
    class _Raising(HostExtension):
        def status(self):
            raise RuntimeError("no")

        def hint_lines(self, lang: str):
            raise RuntimeError("no")

    with TestClient(_app(monkeypatch, extension=_Raising())) as client:
        assert client.get("/api/addon/status").status_code == 503
        assert client.get("/api/addon/hints").json() == {"lines": []}


# --- one badge at a time, with no add-on to enforce it --------------------------------

SECOND_BADGE = "EMORI:marc-002"


def test_a_second_card_cannot_end_a_badge_session_that_is_running(monkeypatch):
    app = _app(monkeypatch)
    with TestClient(app) as client:
        client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})
        assert client.get(minted.json()["session_url"]).status_code == 200

        refused = client.post("/api/badge/claim", json={"payload": SECOND_BADGE})

        assert refused.status_code == 409
        assert refused.json() == {"error_code": "training_unavailable"}
        assert app.state.driver.tracker.active is True


def test_a_second_card_waits_while_the_first_is_still_picking_a_topic(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        assert client.post("/api/badge/claim",
                           json={"payload": "EMORI:anna-001"}).status_code == 202

        refused = client.post("/api/badge/claim", json={"payload": SECOND_BADGE})

        assert refused.status_code == 409
        # The first card keeps its place: scanning it again still answers.
        assert client.post("/api/badge/claim",
                           json={"payload": "EMORI:anna-001"}).status_code == 202


def test_a_card_that_never_opened_its_session_does_not_refuse_the_next_one_forever(monkeypatch):
    with TestClient(_app(monkeypatch)) as client:
        client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})
        assert client.post("/api/identify",
                           json={"payload": "EMORI:anna-001"}).status_code == 200
        assert client.post("/api/badge/claim",
                           json={"payload": SECOND_BADGE}).status_code == 409

        later = server.time.time() + claim_timeout_s() + 1.0
        monkeypatch.setattr(server.time, "time", lambda: later)

        assert client.post("/api/badge/claim",
                           json={"payload": SECOND_BADGE}).status_code == 202


# --- a broken add-on never takes the kiosk down ---------------------------------------

class _Raising(HostExtension):
    name = "raising"

    def __init__(self, *broken: str) -> None:
        self._broken = broken

    def _maybe(self, hook: str) -> None:
        if hook in self._broken:
            raise RuntimeError(f"{hook} fault")

    def register_routes(self, app) -> None:
        self._maybe("register_routes")

    def claim_badge(self, payload: str) -> str:
        self._maybe("claim_badge")
        return BADGE_OK

    def bind_session(self, session_id: str) -> bool:
        self._maybe("bind_session")
        return True

    def session_opened(self, session_id: str) -> bool:
        self._maybe("session_opened")
        return True

    def session_ended(self, session_id=None) -> None:
        self._maybe("session_ended")


def test_an_add_on_whose_routes_raise_still_boots_the_kiosk(monkeypatch):
    with TestClient(_app(monkeypatch, extension=_Raising("register_routes"))) as client:
        assert client.get("/api/addon/hints").status_code == 200


def test_a_raising_badge_claim_refuses_the_badge(monkeypatch):
    with TestClient(_app(monkeypatch, extension=_Raising("claim_badge"))) as client:
        answer = client.post("/api/badge/claim", json={"payload": "EMORI:anna-001"})

    assert answer.status_code == 409
    assert answer.json() == {"error_code": "training_unavailable"}


def test_a_login_that_cannot_be_signed_refuses_the_badge_and_holds_nothing(monkeypatch):
    class _Records(HostExtension):
        ended: list = []

        def session_ended(self, session_id=None) -> None:
            self.ended.append(session_id)

    def _broken_minter(_token):
        def mint(**_claims):
            raise ValueError("cannot sign")
        return mint

    extension = _Records()
    app = _app(monkeypatch, extension=extension)
    monkeypatch.setattr(server, "DEMO", False)
    monkeypatch.setattr(server, "ROBOT_TOKEN", object())
    monkeypatch.setattr(server, "_robot_sso_minter", _broken_minter)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post("/api/identify", json={"payload": "EMORI:anna-001"})

        assert answer.status_code == 409
        assert answer.json() == {"error_code": "training_unavailable"}
        assert len(extension.ended) == 1
        # The same card, and any other, may try again at once.
        assert client.post("/api/badge/claim",
                           json={"payload": "EMORI:anna-001"}).status_code == 202


@pytest.mark.parametrize("answer", [None, 1, "yes"])
def test_a_session_hook_accepts_only_with_true(monkeypatch, answer):
    class _Vague(HostExtension):
        def bind_session(self, session_id: str):
            return answer

    with TestClient(_app(monkeypatch, extension=_Vague())) as client:
        minted = client.post("/api/identify", json={"payload": "EMORI:anna-001"})

    assert minted.status_code == 409
    assert minted.json() == {"error_code": "training_unavailable"}


def test_a_mint_is_held_against_another_card_from_the_moment_it_exists(monkeypatch):
    held_seen = []
    captured = {}

    class _Capturing(server._QrPriorityHandoff):
        def __init__(self, *args, session_held, **kwargs):
            captured["held"] = session_held
            super().__init__(*args, session_held=session_held, **kwargs)

    class _Watches(HostExtension):
        def bind_session(self, session_id: str) -> bool:
            held_seen.append(captured["held"]())
            return True

    monkeypatch.setattr(server, "_QrPriorityHandoff", _Capturing)
    with TestClient(_app(monkeypatch, extension=_Watches())) as client:
        assert client.post("/api/identify",
                           json={"payload": "EMORI:anna-001"}).status_code == 200

    assert held_seen == [True]


def test_a_raising_bind_refuses_the_session_and_frees_the_kiosk(monkeypatch):
    with TestClient(_app(monkeypatch, extension=_Raising("bind_session"))) as client:
        answer = client.post("/api/identify", json={"payload": "EMORI:anna-001"})

        assert answer.status_code == 409
        # Nothing was left held: the claim is released and another card is welcome.
        assert client.post("/api/badge/claim",
                           json={"payload": "EMORI:anna-001"}).status_code == 202


def test_a_raising_session_end_still_sends_the_page_home(monkeypatch):
    app = _app(monkeypatch, extension=_Raising("session_ended"))
    with TestClient(app) as client:
        driver = app.state.driver
        driver.start_session(SessionInfo(session_id="s1", person_id="p", lang="en"))

        with driver._lock:
            driver._finalize_locked(None)

        assert driver.tracker.active is False
        assert client.get("/api/pending-cmd").json()["cmd"] == "home"


def test_an_add_on_built_for_another_api_is_not_loaded(monkeypatch, caplog):
    class _Old(HostExtension):
        api_version = extensions.API_VERSION + 1

    _discover_as(monkeypatch, _entry("old", lambda: _Old))

    with caplog.at_level(logging.WARNING):
        assert type(load_extension()) is HostExtension
    assert "running without it" in caplog.text
