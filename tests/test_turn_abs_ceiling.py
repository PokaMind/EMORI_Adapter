"""Background speech can neither wedge a turn nor keep a session alive."""

from __future__ import annotations

from emori_adapter.hostapp.watchdog import ConversationWatchdog


def test_abs_ceiling_fires_despite_continuous_activity():
    wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0, abs_ceiling_s=40.0)
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    for t in range(1, 40):
        wd.on_activity(now=float(t))
        assert wd.due(now=float(t)) is None  # stall clock keeps refreshing
    wd.on_activity(now=40.0)
    assert wd.due(now=40.0) == "force_end"  # absolute ceiling wins regardless


def test_abs_ceiling_zero_disables_backstop():
    wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0, abs_ceiling_s=0.0)
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    wd.on_activity(now=100.0)
    assert wd.due(now=100.0) is None


def test_stall_clock_unchanged_below_abs_ceiling():
    wd = ConversationWatchdog(open_ceiling_s=15.0, reprompt_after_s=10.0, abs_ceiling_s=40.0)
    wd.on_armed(now=0.0)
    wd.on_user_speaking(now=0.0)
    assert wd.due(now=10.0) == "reprompt"
    assert wd.due(now=15.0) == "force_end"


def test_vad_activity_no_longer_touches_walkaway_clock():
    from emori_adapter.hostapp.driver import LiveDriver
    from emori_adapter.robot.client import DryRunRobotClient

    driver = LiveDriver(DryRunRobotClient())
    driver.last_activity = 123.0
    driver.on_user_speaking_activity({})
    assert driver.last_activity == 123.0  # ambient VAD is not presence
