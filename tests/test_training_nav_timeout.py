"""Training-nav timeout: env-tunable badge scan-to-/session deadline."""

from __future__ import annotations

import pytest

from emori_adapter.hostapp import extensions
from emori_adapter.hostapp.extensions import HostExtension


def test_default_is_sixty_seconds(monkeypatch):
    monkeypatch.delenv("EMORI_TRAINING_NAV_TIMEOUT_S", raising=False)
    assert extensions.claim_timeout_s() == 60.0


def test_valid_override_is_used(monkeypatch):
    monkeypatch.setenv("EMORI_TRAINING_NAV_TIMEOUT_S", "180")
    assert extensions.claim_timeout_s() == 180.0


@pytest.mark.parametrize(
    "bad", ["", "abc", "-5", "0", "9.9", "601", "9999", "nan", "inf", " 60 x"]
)
def test_invalid_override_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("EMORI_TRAINING_NAV_TIMEOUT_S", bad)
    assert extensions.claim_timeout_s() == 60.0


class _NavFakeExtension(HostExtension):
    """A minimal add-on for a _build_app wiring assertion."""


def _build_nav_app(monkeypatch, capture):
    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    real_cls = server._QrPriorityHandoff

    def _spy(runtime, *, ttl_s=None, **kwargs):
        capture["ttl_s"] = ttl_s
        return real_cls(runtime, ttl_s=ttl_s, **kwargs)

    monkeypatch.setattr(server, "DEMO", True)
    monkeypatch.setattr(server, "_QrPriorityHandoff", _spy)
    return server._build_app(
        overrides={
            "robot_client": DryRunRobotClient(),
            "player": None,
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "extension": _NavFakeExtension(),
            "tts": None,
            "stt": None,
        }
    )


def test_build_app_wires_handoff_ttl_to_nav_deadline_plus_grace(monkeypatch):
    """The three coupled badge windows share one source: the handoff fingerprint
    TTL must equal the (overridden) lease deadline + 5 s grace, so decoupling
    them is caught, not just the pure timeout function."""

    monkeypatch.setenv("EMORI_TRAINING_NAV_TIMEOUT_S", "180")
    capture: dict[str, float] = {}
    _build_nav_app(monkeypatch, capture)
    assert capture["ttl_s"] == 185.0
    assert capture["ttl_s"] == extensions.claim_timeout_s() + 5.0


def test_build_app_handoff_ttl_uses_default_grace(monkeypatch):
    monkeypatch.delenv("EMORI_TRAINING_NAV_TIMEOUT_S", raising=False)
    capture: dict[str, float] = {}
    _build_nav_app(monkeypatch, capture)
    assert capture["ttl_s"] == 65.0
