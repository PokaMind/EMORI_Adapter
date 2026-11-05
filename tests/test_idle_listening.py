"""IdleTicker while the learner has the floor: stillness or an attentive breath, never a scan.

The 18-degree ``look_around`` read as a robot that stopped listening and was the one
robot motion running beside an open microphone. While the conversation
watchdog says the turn is ARMED or OPEN the idle tick holds the head on the person
and only breathes; outside a user turn the scan is exactly what it always was.
"""

from __future__ import annotations

import time
from collections import Counter
from types import SimpleNamespace

from emori_adapter.behavior import repertoire as R
from emori_adapter.hostapp.driver import IdleTicker
from emori_adapter.hostapp.watchdog import ConversationWatchdog


class _FakeDriver:
    def __init__(self) -> None:
        self.counters: Counter = Counter()
        self.player = SimpleNamespace(busy=False)
        self.tracker = SimpleNamespace(active=True)
        self.motions: list[list] = []
        self.last_activity = 0.0
        self.watchdog = ConversationWatchdog()

    def submit_legacy_motion(self, intents, *, drop_if_busy=False) -> bool:
        self.motions.append(list(intents))
        return True


def test_idle_scans_outside_a_user_turn():
    driver = _FakeDriver()
    idle = IdleTicker(driver, idle_after_s=0.0)
    idle._tick()  # noqa: SLF001
    idle._tick()  # noqa: SLF001
    assert len(driver.motions) == 2
    assert all(batch[0].reason == "idle: stay alive" for batch in driver.motions)
    # The scan sweeps left/centre/right over consecutive ticks.
    assert any(abs(batch[0].head.yaw) > 10 for batch in driver.motions)


def test_idle_breathes_attentively_while_the_turn_is_armed_or_open():
    driver = _FakeDriver()
    idle = IdleTicker(driver, idle_after_s=0.0)
    now = time.monotonic()
    driver.watchdog.on_armed(now=now)
    idle._tick()  # noqa: SLF001
    driver.watchdog.on_user_speaking(now=now)
    idle._tick()  # noqa: SLF001
    assert len(driver.motions) == 2
    for batch in driver.motions:
        intent = batch[0]
        assert intent.reason == "idle: listening"
        assert intent.head.yaw == 0.0 and abs(intent.head.pitch) <= 3.0
        assert intent.duration >= 1.0
    # Two consecutive breaths alternate so the head is never frozen.
    assert driver.motions[0][0].head.pitch != driver.motions[1][0].head.pitch


def test_awaiting_the_reply_breathes_and_the_scan_returns_once_the_ai_speaks():
    """A reply can take up to 18 s, and a robot that spends it on an 18-degree scan
    looks away the moment the learner finishes. Submitted is still their turn."""

    driver = _FakeDriver()
    idle = IdleTicker(driver, idle_after_s=0.0)
    now = time.monotonic()
    driver.watchdog.on_user_speaking(now=now)
    driver.watchdog.on_submitted(now=now)
    idle._tick()  # noqa: SLF001
    assert driver.motions[0][0].reason == "idle: listening"
    driver.watchdog.on_ai_utterance(now=now)
    idle._tick()  # noqa: SLF001
    assert driver.motions[1][0].reason == "idle: stay alive"


def test_still_while_listening_holds_the_body_while_the_learner_has_the_floor():
    """Every servo move is 4-8 kHz noise at the head's own mic, as loud as a learner at
    80 cm: while the turn is ARMED or OPEN nothing idle moves."""

    driver = _FakeDriver()
    idle = IdleTicker(driver, idle_after_s=0.0, still_while_listening=True)
    now = time.monotonic()
    driver.watchdog.on_armed(now=now)
    idle._tick()  # noqa: SLF001
    driver.watchdog.on_user_speaking(now=now)
    idle._tick()  # noqa: SLF001
    assert driver.motions == []
    assert driver.counters["idle_held_listening"] == 2
    assert driver.counters["idle_ticks"] == 0
    # ...and it is the floor, not the session, that holds it: submitted breathes and
    # outside a turn the scan is back.
    driver.watchdog.on_submitted(now=now)
    idle._tick()  # noqa: SLF001
    driver.watchdog.on_ai_utterance(now=now)
    idle._tick()  # noqa: SLF001
    assert [batch[0].reason for batch in driver.motions] == ["idle: listening",
                                                             "idle: stay alive"]


def test_a_driver_without_a_watchdog_keeps_the_legacy_scan():
    driver = _FakeDriver()
    del driver.watchdog
    idle = IdleTicker(driver, idle_after_s=0.0)
    idle._tick()  # noqa: SLF001
    assert driver.motions[0][0].reason == "idle: stay alive"


def test_attentive_breath_is_small_and_on_axis():
    for seq in range(4):
        intent = R.attentive_breath(seq)
        assert intent.kind == "goto" and intent.head.yaw == 0.0
        assert abs(intent.head.pitch) <= 3.0 and abs(intent.head.z) <= 1.0
        assert intent.antennas is not None
