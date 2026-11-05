"""``POST /api/skill`` — the ROS graph's only route to the robot.

The point of this endpoint is that the ros4hri skill facade does NOT POST the daemon:
it POSTs the hostapp, so the single-writer rule holds and every trap RobotPlayer
closes (generation guards, the busy interlock, drop_if_busy, push_turn) stays closed.
An intake like that is only safe if it is closed by default and stays narrow, so these
pin the four independent gates -- flag, secret, loopback, rate limit -- one at a time,
plus the two "the robot is not yours right now" answers.

The mapping from a skill request to motion, the pure ``skill_to_intents``, is tested
in tests/test_cue_mapping.py; nothing here re-tests a motion shape.
"""

from __future__ import annotations

from collections import Counter

import pytest

KEY = "shared-secret-for-tests"
HEADERS = {"X-Emori-Skill-Key": KEY}


@pytest.fixture(autouse=True)
def ros_owns_motion(monkeypatch):
    """Every test below asks "what does the intake do for a LEGITIMATE facade call".

    That means EMORI_ROS_MOTION_OWNER=ros: on the shipped default the answer is 409 for
    everything, which is its own test (`test_the_default_owner_refuses...`) rather than
    a reason for the other twenty to be testing the same refusal.
    """

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "ros")


class _Driver:
    """The hostapp surface skill_api is allowed to touch, and nothing else."""

    def __init__(self, *, owner: bool = True, reserved: bool = False,
                 accept: bool = True) -> None:
        self.legacy_motion_enabled = owner
        self.reserved = reserved
        self.accept = accept
        self.batches: list[list] = []
        self.dropped_if_busy: list[bool] = []
        # The real LiveDriver's ledger is a collections.Counter, and skill_api writes
        # to it on every answer; the rehearsal reads it off /api/status.
        self.counters: Counter = Counter()
        self.liveness_beats = 0

    def note_skill_accepted(self) -> None:
        self.liveness_beats += 1

    def head_reserved(self) -> bool:
        return self.reserved

    def submit_legacy_motion(self, intents, *, drop_if_busy: bool = False,
                             guard=None) -> bool:
        self.batches.append(list(intents))
        self.dropped_if_busy.append(drop_if_busy)
        return self.accept


def _app(driver, *, enabled: bool = True, key: str = KEY, interval_s: float = 0.0,
         clock=None):
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    app = FastAPI()
    kwargs = {"clock": clock} if clock is not None else {}
    mounted = skill_api.mount(app, driver=driver, enabled=enabled, key=key,
                              interval_s=interval_s, **kwargs)
    return app, mounted


def _client(app, *, host: str = "127.0.0.1"):
    from starlette.testclient import TestClient

    return TestClient(app, client=(host, 41000))


def _post(client, body: dict, *, headers=HEADERS):
    return client.post("/api/skill", json=body, headers=headers)


EXPRESSION = {"skill": "set_expression", "label": "happy", "confidence": 0.8}


# --- the switch -------------------------------------------------------------------

def test_not_mounted_when_the_flag_is_off(monkeypatch):
    monkeypatch.setenv("EMORI_SKILL_API", "0")
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    app = FastAPI()
    assert skill_api.mount(app, driver=_Driver()) is False
    assert _post(_client(app), EXPRESSION).status_code == 404


def test_not_mounted_when_the_flag_is_on_but_the_key_is_unset(monkeypatch):
    """A secret-less "enabled" is the one configuration that must never exist."""

    monkeypatch.setenv("EMORI_SKILL_API", "1")
    monkeypatch.delenv("EMORI_SKILL_KEY", raising=False)
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    app = FastAPI()
    assert skill_api.mount(app, driver=_Driver()) is False
    assert _post(_client(app), EXPRESSION).status_code == 404


def test_mounted_when_both_halves_are_set(monkeypatch):
    monkeypatch.setenv("EMORI_SKILL_API", "1")
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    driver = _Driver()
    app = FastAPI()
    assert skill_api.mount(app, driver=driver) is True
    assert _post(_client(app), EXPRESSION).status_code == 200


def test_clearing_the_flag_at_runtime_revokes_the_intake(monkeypatch):
    """An operator who turns it off has revoked it, not merely stopped mounting it."""

    monkeypatch.setenv("EMORI_SKILL_API", "1")
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    driver = _Driver()
    app = FastAPI()
    assert skill_api.mount(app, driver=driver) is True
    client = _client(app)

    monkeypatch.setenv("EMORI_SKILL_API", "0")
    r = _post(client, EXPRESSION)
    assert r.status_code == 403 and r.json()["error_code"] == "skill_api_disabled"
    assert driver.batches == []


# --- who may call it --------------------------------------------------------------

def test_a_missing_or_wrong_key_is_refused():
    driver = _Driver()
    app, _ = _app(driver)
    client = _client(app)

    assert _post(client, EXPRESSION, headers={}).status_code == 403
    r = _post(client, EXPRESSION, headers={"X-Emori-Skill-Key": "not-the-key"})
    assert r.status_code == 403 and r.json()["error_code"] == "bad_key"
    assert driver.batches == []


@pytest.mark.parametrize("body", [b"not json at all", b"[]", b"\"a string\""])
def test_a_malformed_body_from_off_box_is_still_answered_as_off_box(body):
    """Auth runs BEFORE the body is parsed.

    A validated body parameter is parsed first, so a scanner posting junk would get a
    422 -- which distinguishes a robot that HAS a motion intake from one that does not,
    to a caller that passed neither the loopback check nor the key.
    """

    driver = _Driver()
    app, _ = _app(driver)
    r = _client(app, host="192.0.2.44").post("/api/skill", content=body)
    assert r.status_code == 403 and r.json()["error_code"] == "not_loopback"
    assert driver.batches == []


def test_a_malformed_body_from_loopback_is_a_400():
    driver = _Driver()
    app, _ = _app(driver)
    r = _client(app).post("/api/skill", content=b"{not json", headers=HEADERS)
    assert r.status_code == 400 and r.json()["error_code"] == "bad_skill"
    assert driver.batches == []


def test_a_non_loopback_client_is_refused():
    """The ROS container is host-networked; anything off-box is not the facade."""

    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app, host="192.0.2.44"), EXPRESSION)
    assert r.status_code == 403 and r.json()["error_code"] == "not_loopback"
    assert driver.batches == []


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_every_loopback_form_is_accepted(host):
    driver = _Driver()
    app, _ = _app(driver)
    assert _post(_client(app, host=host), EXPRESSION).status_code == 200


# --- what it accepts --------------------------------------------------------------

def test_an_unknown_skill_is_a_400():
    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), {"skill": "say", "text": "hello"})
    assert r.status_code == 400 and r.json()["error_code"] == "bad_skill"
    assert driver.batches == []


@pytest.mark.parametrize("body", [
    {"skill": "look_at", "yaw_deg": 90.0},              # outside -30..30
    {"skill": "look_at", "pitch_deg": -80.0},           # outside -25..25
    {"skill": "look_at", "duration": 9.0},              # outside 0.2..1.5
    {"skill": "set_expression", "label": "smug"},       # not an expression label
    {"skill": "set_expression", "label": "happy", "confidence": 4.0},
    {"skill": "replay_motion", "name": "backflip"},     # not in the repertoire
])
def test_out_of_range_params_are_refused_not_clamped(body):
    """A caller outside the bounds is a bug on the other side, not a quieter motion."""

    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), body)
    assert r.status_code == 400
    assert r.json()["error_code"] in ("out_of_range", "bad_skill")
    assert driver.batches == []


def test_a_valid_set_expression_reaches_the_player_within_the_cap():
    from emori_adapter.behavior.cues import SKILL_MAX_S

    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), EXPRESSION)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["dropped"] is False
    assert body["submitted"] == len(driver.batches[0]) >= 1
    assert sum(i.duration for i in driver.batches[0]) <= SKILL_MAX_S + 1e-6
    # Never in front of the greeting: a chatty node must not starve the shared queue.
    assert driver.dropped_if_busy == [True]


def test_a_valid_look_at_reaches_the_player():
    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), {"skill": "look_at", "yaw_deg": 12.0, "pitch_deg": -8.0,
                             "duration": 0.5})
    assert r.status_code == 200 and r.json()["submitted"] == 1
    intent = driver.batches[0][0]
    assert intent.duration == pytest.approx(0.5)


def test_a_valid_replay_motion_reaches_the_player_within_the_cap():
    from emori_adapter.behavior.cues import SKILL_MAX_S

    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), {"skill": "replay_motion", "name": "nod", "intensity": 0.5})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sum(i.duration for i in driver.batches[0]) <= SKILL_MAX_S + 1e-6


# --- when the robot is not available ----------------------------------------------

def test_a_robot_owned_by_an_add_on_mode_answers_409():
    driver = _Driver(owner=False)
    app, _ = _app(driver)
    r = _post(_client(app), EXPRESSION)
    assert r.status_code == 409 and r.json()["error_code"] == "not_owner"
    assert driver.batches == []


def test_a_reserved_head_answers_423():
    """Push-turn selection is reading the head's pose; motion would wipe its baseline."""

    driver = _Driver(reserved=True)
    app, _ = _app(driver)
    r = _post(_client(app), EXPRESSION)
    assert r.status_code == 423 and r.json()["error_code"] == "head_reserved"
    assert driver.batches == []


def test_a_dropped_submit_is_reported_honestly():
    driver = _Driver(accept=False)
    app, _ = _app(driver)
    body = _post(_client(app), EXPRESSION).json()
    assert body == {"ok": True, "submitted": 0, "dropped": True}


# --- who OWNS the robot -----------------------------------------------------------

@pytest.mark.parametrize("owner", ["hostapp", "none", "", "typo"])
def test_the_default_owner_refuses_an_otherwise_perfect_request(monkeypatch, owner):
    """The knob that documents which lane may move the robot has to decide it.

    ``hostapp`` promises "the facade does NOT POST -- zero behaviour change", and
    ``none`` promises nothing moves. An authenticated loopback caller that moved the
    head anyway would make both sentences false, and an unrecognised value is a typo,
    which must read as the shipped default rather than as permission.
    """

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", owner)
    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), EXPRESSION)

    assert r.status_code == 409 and r.json()["error_code"] == "not_owner"
    assert driver.batches == []


def test_the_owner_is_re_read_per_request(monkeypatch):
    """Enforcement lives at the request, not at mount: revoking must take effect."""

    driver = _Driver()
    app, _ = _app(driver)
    client = _client(app)
    assert _post(client, EXPRESSION).status_code == 200

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "none")
    assert _post(client, EXPRESSION).status_code == 409
    assert len(driver.batches) == 1


# --- the robot's own limits, not the defaults -------------------------------------

def test_the_intake_plans_against_the_rigs_cue_config(monkeypatch):
    """EMORI_CUE_Z_MM is bisected on the robot because head z is unverified on hardware.

    The one lane driven from OUTSIDE the hostapp must not be the one that ignores it.
    """

    from emori_adapter.behavior import CueConfig

    monkeypatch.setenv("EMORI_CUE_Z_MM", "2.0")
    driver = _Driver()
    driver.cue_config = CueConfig.from_env()
    app, _ = _app(driver)

    assert _post(_client(app), {"skill": "set_expression", "label": "scared"}).status_code == 200
    zs = [abs(i.head.z) for i in driver.batches[0] if i.head is not None]
    assert zs and max(zs) <= 2.0 + 1e-9


# --- the rate limit ---------------------------------------------------------------

def test_excess_requests_are_refused_rather_than_queued():
    now = {"t": 100.0}
    driver = _Driver()
    app, _ = _app(driver, interval_s=0.35, clock=lambda: now["t"])
    client = _client(app)

    assert _post(client, EXPRESSION).status_code == 200
    now["t"] += 0.10
    r = _post(client, EXPRESSION)
    assert r.status_code == 429 and r.json()["error_code"] == "rate_limited"
    assert len(driver.batches) == 1                 # nothing queued behind the first

    now["t"] += 0.30                                # past the interval
    assert _post(client, EXPRESSION).status_code == 200
    assert len(driver.batches) == 2


def test_one_ros_event_is_a_burst_of_DIFFERENT_skills_and_all_of_it_lands():
    """A single engagement cycle issues set_expression + replay_motion + look_at.

    They are microseconds apart, so a single shared slot would refuse the actual
    MOTION in favour of whichever request the node published first -- i.e. the
    recovery behaviour would never reach the robot while the graph logged success.
    """

    now = {"t": 100.0}
    driver = _Driver()
    app, _ = _app(driver, interval_s=0.35, clock=lambda: now["t"])
    client = _client(app)

    assert _post(client, EXPRESSION).status_code == 200
    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 200
    assert _post(client, {"skill": "look_at", "yaw_deg": 0.0, "pitch_deg": 6.0}).status_code == 200
    assert len(driver.batches) == 3

    # ...while a node repeating the SAME skill is still refused: that is the queue
    # starvation the limit exists for.
    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 429


# --- the hostapp's own mount point ------------------------------------------------

class _AppPlayer:
    busy = False

    def __init__(self) -> None:
        self.batches: list[list] = []

    def submit(self, intents, *, drop_if_busy: bool = False, guard=None) -> bool:
        self.batches.append(list(intents))
        return True


def _hostapp(monkeypatch, player):
    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    return server._build_app(overrides={
        "watchdog": False, "idle": False, "gesture": False,
        "player": player, "robot_client": DryRunRobotClient(),
    })


def test_the_shipped_hostapp_has_no_skill_route(monkeypatch):
    """The default posture: a robot nobody opted in has no intake to find."""

    monkeypatch.delenv("EMORI_SKILL_API", raising=False)
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    app = _hostapp(monkeypatch, _AppPlayer())
    assert _post(_client(app), EXPRESSION).status_code == 404


def test_the_hostapp_funnels_an_enabled_skill_into_the_one_queue(monkeypatch):
    """The whole point: a skill request lands in RobotPlayer, not on the daemon."""

    monkeypatch.setenv("EMORI_SKILL_API", "1")
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    player = _AppPlayer()
    app = _hostapp(monkeypatch, player)

    r = _post(_client(app), {"skill": "replay_motion", "name": "nod", "intensity": 0.5})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(player.batches) == 1

    # An add-on mode taking the robot closes it, through the same ownership gate the
    # hostapp's own lanes go through.
    app.state.driver.set_legacy_motion_enabled(False)
    r = _post(_client(app), {"skill": "replay_motion", "name": "nod"})
    assert r.status_code == 409 and r.json()["error_code"] == "not_owner"
    assert len(player.batches) == 1


def test_a_refused_request_never_spends_the_budget():
    """A 400 is not an accepted request, so it must not cost the caller its slot."""

    now = {"t": 100.0}
    driver = _Driver()
    app, _ = _app(driver, interval_s=0.35, clock=lambda: now["t"])
    client = _client(app)

    assert _post(client, {"skill": "nope"}).status_code == 400
    assert _post(client, EXPRESSION).status_code == 200


# --- the hostapp-side ledger ------------------------------------------------------
# The facade keeps its own counters, but they say what it POSTED. A stale key, a
# container that is not loopback, an owner knob left on `hostapp`: all three read as
# "posted, nothing moved" on the ROS side and as total silence on this one, which is
# exactly the pair of numbers the sim rehearsal has to compare.

CALM_SETTLE = {"skill": "replay_motion", "name": "calm_settle", "intensity": 0.4}


def test_an_accepted_request_counts_itself_and_its_intents():
    driver = _Driver()
    app, _ = _app(driver)
    r = _post(_client(app), EXPRESSION)

    assert r.status_code == 200
    n = len(driver.batches[0])
    assert driver.counters["skill_accepted"] == 1
    assert driver.counters["skill_intents_submitted"] == n >= 1
    assert driver.counters["skill_dropped"] == 0


def test_an_accepted_request_is_the_owner_fallbacks_liveness_beat():
    """``note_skill_accepted`` is the ONLY evidence the hostapp gets that the ROS lane
    is still moving the robot; a refusal or a drop is not that evidence."""

    driver = _Driver()
    app, _ = _app(driver)
    client = _client(app)
    assert _post(client, EXPRESSION).status_code == 200
    assert driver.liveness_beats == 1

    driver.accept = False
    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 200
    assert driver.liveness_beats == 1          # a drop is not a beat


def test_a_dropped_submit_is_counted_as_dropped_not_accepted():
    driver = _Driver(accept=False)
    app, _ = _app(driver)
    assert _post(_client(app), EXPRESSION).json()["dropped"] is True
    assert driver.counters["skill_dropped"] == 1
    assert driver.counters["skill_accepted"] == 0
    assert driver.counters["skill_intents_submitted"] == 0


def test_a_driver_without_a_ledger_is_still_served():
    """An injected double (or a driver mid-teardown) must not be able to 500 a request."""

    class _NoLedger(_Driver):
        counters = None

    driver = _NoLedger()
    app, _ = _app(driver)
    assert _post(_client(app), EXPRESSION).status_code == 200


@pytest.mark.parametrize("kwargs, body, headers, host, code", [
    ({}, EXPRESSION, {}, "127.0.0.1", "bad_key"),
    ({}, EXPRESSION, HEADERS, "192.0.2.44", "not_loopback"),
    ({}, {"skill": "nope"}, HEADERS, "127.0.0.1", "bad_skill"),
    ({}, {"skill": "look_at", "yaw_deg": 90.0}, HEADERS, "127.0.0.1", "out_of_range"),
    ({"owner": False}, EXPRESSION, HEADERS, "127.0.0.1", "not_owner"),
    ({"reserved": True}, EXPRESSION, HEADERS, "127.0.0.1", "head_reserved"),
])
def test_every_refusal_names_itself_on_the_ledger(kwargs, body, headers, host, code):
    driver = _Driver(**kwargs)
    app, _ = _app(driver)
    _post(_client(app, host=host), body, headers=headers)

    assert driver.counters["skill_refused_%s" % code] == 1
    assert driver.counters["skill_accepted"] == 0


def test_the_owner_refusal_is_counted_too(monkeypatch):
    """The commonest misconfiguration of all: the knob says hostapp on one side only."""

    monkeypatch.setenv("EMORI_ROS_MOTION_OWNER", "hostapp")
    driver = _Driver()
    app, _ = _app(driver)
    assert _post(_client(app), EXPRESSION).status_code == 409
    assert driver.counters["skill_refused_not_owner"] == 1


def test_the_rate_limit_is_counted():
    now = {"t": 100.0}
    driver = _Driver()
    app, _ = _app(driver, interval_s=0.35, clock=lambda: now["t"])
    client = _client(app)

    assert _post(client, EXPRESSION).status_code == 200
    assert _post(client, EXPRESSION).status_code == 429
    assert driver.counters["skill_refused_rate_limited"] == 1
    assert driver.counters["skill_accepted"] == 1


def test_a_revoked_intake_is_counted(monkeypatch):
    monkeypatch.setenv("EMORI_SKILL_API", "1")
    monkeypatch.setenv("EMORI_SKILL_KEY", KEY)
    from fastapi import FastAPI

    from emori_adapter.hostapp import skill_api

    driver = _Driver()
    app = FastAPI()
    assert skill_api.mount(app, driver=driver) is True
    monkeypatch.setenv("EMORI_SKILL_API", "0")

    assert _post(_client(app), EXPRESSION).status_code == 403
    assert driver.counters["skill_refused_skill_api_disabled"] == 1


# --- safety outranks expression ---------------------------------------------------

def test_a_de_escalation_settle_is_never_dropped_behind_expression():
    """The hostapp's own lane submits its calm settle with drop_if_busy=False when it
    preempts a live cue. With owner=ros the ROS graph is the ONLY thing that can
    produce that settle, so the intake has to apply the same rule -- otherwise an
    agitated learner's de-escalation is the one motion most likely to be dropped,
    behind the very expression it is meant to interrupt.
    """

    driver = _Driver()
    app, _ = _app(driver)
    client = _client(app)

    assert _post(client, CALM_SETTLE).status_code == 200
    assert driver.dropped_if_busy == [False]

    # ...and nothing else gains that privilege.
    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 200
    assert _post(client, EXPRESSION).status_code == 200
    assert driver.dropped_if_busy == [False, True, True]


def test_a_settle_that_arrives_while_the_queue_is_full_still_reaches_the_player():
    class _BusyDriver(_Driver):
        def submit_legacy_motion(self, intents, *, drop_if_busy: bool = False,
                                 guard=None) -> bool:
            if drop_if_busy:                 # a full 12-slot queue refuses these
                return False
            return super().submit_legacy_motion(intents, drop_if_busy=drop_if_busy,
                                                guard=guard)

    driver = _BusyDriver()
    app, _ = _app(driver)
    client = _client(app)

    assert _post(client, EXPRESSION).json()["dropped"] is True
    r = _post(client, CALM_SETTLE)
    assert r.status_code == 200 and r.json()["dropped"] is False
    assert len(driver.batches) == 1
    assert driver.counters["skill_dropped"] == 1 and driver.counters["skill_accepted"] == 1


def test_the_settle_exemption_survives_the_normalisation_skill_to_intents_applies():
    """`skill_to_intents` strips and lower-cases the motion name before it builds the
    settle, so a body spelling it ``" Calm_Settle "`` IS a de-escalation as far as the
    player is concerned. The intake must recognise it as one too, or that request is
    built as a settle and then submitted as if it were expression.
    """

    driver = _Driver()
    app, _ = _app(driver)
    client = _client(app)

    body = {"skill": "replay_motion", "name": " Calm_Settle ", "intensity": 0.4}
    assert _post(client, body).status_code == 200
    assert driver.dropped_if_busy == [False]


def test_a_de_escalation_settle_is_not_refused_by_the_per_kind_rate_limit():
    """A cue replay and the settle share the ``replay_motion`` kind. If the settle had to
    wait out that kind's interval it would be answered 429 exactly when an escalating
    cycle lands right after a sentence onset -- and with owner=ros nothing else can
    produce it. The exemption applies to the 429 gate as well as to drop_if_busy; every
    other replay still spends the budget as before.
    """

    driver = _Driver()
    app, _ = _app(driver, interval_s=10.0)
    client = _client(app)

    assert _post(client, {"skill": "replay_motion", "name": "talking"}).status_code == 200
    r = _post(client, CALM_SETTLE)
    assert r.status_code == 200 and r.json()["dropped"] is False
    # The settle spent nothing, and a plain replay right after it is still rate-limited
    # by the talking replay that came first.
    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 429
    assert driver.counters["skill_accepted"] == 2


def test_a_stray_space_does_not_buy_a_second_rate_limit_budget():
    driver = _Driver()
    app, _ = _app(driver, interval_s=10.0)
    client = _client(app)

    assert _post(client, {"skill": "replay_motion", "name": "nod"}).status_code == 200
    assert _post(client, {"skill": " replay_motion", "name": "nod"}).status_code == 429
