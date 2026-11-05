"""A guard-rejected blip is ambient sound: it must not re-arm the walk-away clock.

Measured live: a room nobody spoke in produced a rejected blip every ~7 s all
session, and if each one refreshed ``last_activity`` EMORI_WALKAWAY_S would never fire
and the conversation with an empty chair would go on until the abs ceiling. An
ACCEPTED turn still counts (it is a person speaking), and so does the AI's own speech.
"""

from __future__ import annotations

import time


def _driver():
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    return LiveDriver(DryRunRobotClient())


def test_a_rejected_turn_does_not_touch_the_activity_clock():
    driver = _driver()
    driver.last_activity = time.monotonic() - 100.0
    before = driver.last_activity
    driver.on_turn_rejected({})
    assert driver.last_activity == before
    assert driver.counters["turn_rejected"] == 1


def test_an_accepted_turn_still_touches_it():
    driver = _driver()
    driver.last_activity = time.monotonic() - 100.0
    before = driver.last_activity
    driver.on_turn_submitted({})
    assert driver.last_activity > before


def test_ambient_speaking_activity_still_does_not_touch_it():
    driver = _driver()
    driver.last_activity = time.monotonic() - 100.0
    before = driver.last_activity
    driver.on_user_speaking_activity({})
    assert driver.last_activity == before
