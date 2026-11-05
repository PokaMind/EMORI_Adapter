"""Suite-wide isolation for the robot's own runtime state.

``LangState`` (hostapp/lang_state.py) persists the live kiosk language to
``EMORI_LANG_STATE_FILE``, defaulting to the REPO-RELATIVE ``artifacts/kiosk_lang.json``
so a deployment only has to keep one directory writable.  That default is right on the
appliance and poisonous under pytest: any test that switches the language (today
``test_identity``'s ``app.state.lang_state.set("en")``) writes the real file, and every
later test that builds a hostapp -- in that run and in every run afterwards -- silently
renders, speaks and mints SSO claims in the language a previous test left behind rather
than in its own ``EMORI_LANG``.  That is how a green suite turns red on its second
invocation, with the failure landing on innocent modules (readycheck, voice-menu
grammar) in test-ORDER-dependent combinations.

So every test starts from a state file that does not exist yet: ``current()`` falls back
to the deployment default the test itself set, and a test that wants a switched kiosk
performs the switch.  Tests that pin the persistence behaviour set the variable
themselves (``monkeypatch`` inside the test body overrides this fixture, and both are
undone at teardown).
"""

from __future__ import annotations

import itertools
import os
import sys
import types
from pathlib import Path

import pytest

# The suite tests the kiosk alone, whatever else is installed in this environment.
os.environ["EMORI_ADDONS"] = "off"

# Some third-party wheels ship a top-level ``tests`` package into site-packages
# (ultralytics 8.x does), and a REGULAR package anywhere on sys.path beats this
# repo's namespace ``tests/`` dir regardless of path order (PEP 420) -- silently
# redirecting ``from tests.test_live_contract import ...`` to someone else's test
# suite and killing collection. Pre-register OUR directory as the ``tests``
# package before any test module imports, so intra-suite imports always resolve
# here whatever the environment has swallowed.
_here = str(Path(__file__).resolve().parent)
if getattr(sys.modules.get("tests"), "__path__", None) != [_here]:
    _pkg = types.ModuleType("tests")
    _pkg.__path__ = [_here]
    sys.modules["tests"] = _pkg

# lang_state._STATE_FILE_ENV, spelled out rather than imported: conftest is loaded for
# every test, including runs collected without the hostapp extras installed.
_LANG_STATE_FILE_ENV = "EMORI_LANG_STATE_FILE"

_counter = itertools.count()

# A two-person roster for tests that scan a badge (configs/roster.example.yaml ships
# nobody). Tests use a COPY (``sample_roster``): People-tab code rewrites its file.
SAMPLE_ROSTER = Path(_here) / "fixtures" / "roster.sample.yaml"


@pytest.fixture
def sample_roster(tmp_path_factory, monkeypatch):
    """Point ``EMORI_ROSTER`` at a fresh copy of the sample roster; returns that path."""

    path = tmp_path_factory.mktemp("roster") / "roster.yaml"
    path.write_bytes(SAMPLE_ROSTER.read_bytes())
    monkeypatch.setenv("EMORI_ROSTER", str(path))
    return path


@pytest.fixture(scope="session")
def _kiosk_lang_state_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("kiosk-lang")


@pytest.fixture(autouse=True)
def _isolated_kiosk_language_state(monkeypatch, _kiosk_lang_state_dir):
    """Point every test's kiosk language state at its own (absent) file."""

    path = _kiosk_lang_state_dir / f"kiosk_lang-{next(_counter)}.json"
    monkeypatch.setenv(_LANG_STATE_FILE_ENV, str(path))


@pytest.fixture(autouse=True)
def _no_inherited_robot_token(monkeypatch, tmp_path_factory):
    """A test never runs with THIS machine's Pokamind robot token.

    The token resolves from ``EMORI_POKAMIND_TOKEN_FILE`` (default
    ``secrets/pokamind_robot_token`` under the CWD) and then ``POKAMIND_ROBOT_TOKEN``,
    and ``emori_adapter.config`` loads a developer's ``.env`` into ``os.environ`` at
    import. Either would silently switch every SSO mint in the suite from the legacy
    secret the tests assert on to EdDSA -- so each test starts from a token file
    that does not exist, no env token, and an unbound process cache. A test about
    the token sets one itself (``monkeypatch.setenv`` inside the test wins).
    """

    path = tmp_path_factory.getbasetemp() / "no-robot-token" / f"token-{next(_counter)}"
    monkeypatch.setenv("EMORI_POKAMIND_TOKEN_FILE", str(path))
    monkeypatch.delenv("POKAMIND_ROBOT_TOKEN", raising=False)
    try:
        from emori_adapter import robot_token
    except Exception:  # pragma: no cover - collected without the package importable
        robot_token = None
    if robot_token is not None:
        robot_token.reset_configured()
        server = sys.modules.get("emori_adapter.hostapp.server")
        if server is not None and hasattr(server, "ROBOT_TOKEN_LOAD"):
            # The hostapp binds its token at import, like SECRET; a module imported
            # while some earlier test had one configured must not keep it.
            monkeypatch.setattr(server, "ROBOT_TOKEN_LOAD", robot_token.TokenLoad())
            monkeypatch.setattr(server, "ROBOT_TOKEN", None)
    yield
    if robot_token is not None:
        robot_token.reset_configured()
