"""The auto-apply watcher: restarts the hostapp unattended, and only when that is safe.

Pinned here, because each is a promise to a site:

* nothing restarts while a session or an add-on mode runs, nor before the robot has
  been idle for the whole threshold (600 s by default) -- and any busy reading starts
  the count again;
* one restart once it has, and never more than one per 30 minutes;
* only Pokamind-derived or generated reasons (``scope_changed``,
  ``dashboard_password``) and the ones an add-on names: a change a person saved waits
  for the Apply button;
* ``EMORI_SITE_AUTO_APPLY_IDLE_S=0`` switches it off.
"""

from __future__ import annotations

import asyncio

import pytest

from emori_adapter.admin import site_apply
from test_admin_service_fakes import http_ok, http_status

# What the hostapp's /api/admin/summary answers: an idle add-on reports the
# mode "idle", and "idle" is the hostapp's own fail-closed verdict.
IDLE = {"lang": "ca", "allowed_langs": ["ca"], "mic_profile": None,
        "session_active": False, "addon_mode": "idle", "idle": True}
BUSY = dict(IDLE, session_active=True, idle=False)
ADDON_MODE = dict(IDLE, addon_mode="running", idle=False)
# No session and the add-on idle, but a badge lease is held (or the add-on is
# mid-way through something): only the hostapp's own verdict knows.
LEASED = dict(IDLE, idle=False)


def _site(reasons, *, dash_differs=None):
    keys = []
    if dash_differs is not None:
        keys.append({"key": "EMORI_DASH_PASSWORD", "owner": "admin", "value": None,
                     "set": True, "source": "admin", "secret": True, "editable": False,
                     "restart": "hostapp", "running_differs": dash_differs, "problem": None})
    return {"keys": keys, "restart_pending": bool(reasons), "restart_reasons": list(reasons),
            "invalid_running": {}, "scope": {}}


class Rig:
    """A hostapp that answers the two routes, a restart that records, a clock."""

    def __init__(self, *, reasons=("scope_changed",), summary=None, dash_differs=None,
                 idle_s=600.0, restart_ok=True, auto_reasons=None):
        self.now = 1000.0
        self.site = _site(reasons, dash_differs=dash_differs)
        self.summary = dict(IDLE if summary is None else summary)
        self.site_up = True
        self.summary_up = True
        self.restarts = 0
        self.restart_ok = restart_ok
        self.calls: list[tuple[str, str]] = []
        self.watcher = site_apply.SiteApplyWatcher(
            fetch=self.fetch, restart=self.restart, clock=lambda: self.now, idle_s=idle_s,
            **({} if auto_reasons is None else {"auto_reasons": auto_reasons}))

    def fetch(self, method, path):
        self.calls.append((method, path))
        if path == "/api/admin/site":
            return http_ok(self.site) if self.site_up else http_status(503, {})
        if path == "/api/admin/summary":
            return http_ok(self.summary) if self.summary_up else http_status(502, {})
        raise AssertionError(path)

    def restart(self):
        self.restarts += 1
        if self.restart_ok:
            # A new hostapp process starts with nothing pending.
            self.site = _site(())
            return {"ok": True, "unit": "emori-hostapp.service", "action": "restart"}
        return {"ok": False, "error": "exit_1"}

    def run_for(self, seconds, step=60.0):
        decisions = []
        end = self.now + seconds
        while self.now < end:
            decisions.append(self.watcher.tick())
            self.now += step
        return decisions


def test_no_restart_before_the_whole_idle_threshold_then_exactly_one():
    rig = Rig()

    first = rig.run_for(600)              # ticks at 0, 60, ... 540 s of idle
    assert rig.restarts == 0 and set(first) == {"waiting_idle"}

    assert rig.watcher.tick() == "applied"   # 600 s of continuous idle
    assert rig.restarts == 1
    assert set(rig.run_for(3600)) == {"nothing_pending"}  # the new process has none
    assert rig.restarts == 1


def test_a_session_or_an_add_on_mode_holds_it_and_starts_the_count_again():
    rig = Rig()
    rig.run_for(540)
    rig.summary = dict(BUSY)
    assert rig.watcher.tick() == "busy"
    rig.now += 60
    rig.summary = dict(ADDON_MODE)
    assert rig.watcher.tick() == "busy"
    rig.now += 60
    rig.summary = dict(IDLE)

    again = rig.run_for(600)
    assert rig.restarts == 0 and set(again) == {"waiting_idle"}
    assert rig.watcher.tick() == "applied" and rig.restarts == 1


def test_an_unanswered_question_is_never_idle():
    rig = Rig()
    rig.run_for(540)
    rig.summary_up = False
    assert rig.watcher.tick() == "hostapp_unreachable"
    rig.now += 60
    rig.summary_up = True
    rig.site_up = False
    assert rig.watcher.tick() == "hostapp_unreachable"
    rig.now += 60
    rig.site_up = True
    assert set(rig.run_for(600)) == {"waiting_idle"} and rig.restarts == 0


@pytest.mark.parametrize("summary", [{}, {"session_active": False, "addon_mode": None},
                                     dict(IDLE, idle=None), dict(IDLE, idle="true"), LEASED])
def test_a_summary_that_does_not_say_idle_is_busy(summary):
    rig = Rig(summary=summary)

    assert set(rig.run_for(1200)) == {"busy"} and rig.restarts == 0


def test_an_idle_add_on_reporting_its_idle_mode_is_idle():
    """An add-on's own state on an idle robot is the mode "idle" -- never null. A
    robot that reports it is applied once the threshold has passed."""

    rig = Rig(summary=dict(IDLE))

    rig.run_for(660)
    assert rig.restarts == 1


def test_at_most_one_restart_per_thirty_minutes():
    rig = Rig()
    rig.run_for(600)
    assert rig.watcher.tick() == "applied"
    # Pokamind changes something again right away.
    rig.site = _site(["scope_changed"])
    decisions = rig.run_for(1800 - 60)

    assert rig.restarts == 1
    assert "rate_limited" in decisions
    rig.run_for(60 * 20)
    assert rig.restarts == 2


def test_a_failed_restart_still_counts_against_the_cap():
    rig = Rig(restart_ok=False)
    rig.run_for(600)

    assert rig.watcher.tick() == "restart_failed"
    rig.run_for(1700)
    assert rig.restarts == 1


@pytest.mark.parametrize("reasons", [["settings_saved"],
                                     ["scope_changed", "settings_saved"], ["something_new"]])
def test_a_change_a_person_saved_waits_for_the_apply_button(reasons):
    rig = Rig(reasons=reasons)

    decisions = rig.run_for(7200)

    assert rig.restarts == 0 and set(decisions) == {"manual_reasons"}
    # It does not even ask whether the robot is idle.
    assert ("GET", "/api/admin/summary") not in rig.calls


def test_a_reason_the_add_on_names_is_applied_unattended_and_others_still_wait():
    named = Rig(reasons=("addon_change",), auto_reasons=frozenset({"addon_change"}))
    named.run_for(600)
    assert named.watcher.tick() == "applied" and named.restarts == 1

    unnamed = Rig(reasons=("addon_change",))
    assert set(unnamed.run_for(1800)) == {"manual_reasons"} and unnamed.restarts == 0

    mixed = Rig(reasons=("addon_change", "settings_saved"),
                auto_reasons=frozenset({"addon_change"}))
    assert set(mixed.run_for(1800)) == {"manual_reasons"} and mixed.restarts == 0


def test_a_pending_restart_without_a_reason_is_never_applied_unattended():
    rig = Rig(reasons=())
    rig.site["restart_pending"] = True

    assert set(rig.run_for(1800)) == {"manual_reasons"} and rig.restarts == 0


def test_nothing_pending_restarts_nothing():
    rig = Rig(reasons=())

    assert set(rig.run_for(7200)) == {"nothing_pending"} and rig.restarts == 0


def test_switched_off_with_zero(monkeypatch):
    monkeypatch.setenv(site_apply.ENV_IDLE_S, "0")
    rig = Rig(idle_s=None)

    assert rig.watcher.enabled is False
    assert set(rig.run_for(7200)) == {"disabled"} and rig.restarts == 0
    assert rig.calls == []
    # ... and its loop returns at once instead of polling.
    asyncio.run(asyncio.wait_for(rig.watcher.run(), timeout=2))


@pytest.mark.parametrize("raw, expected", [("", 600.0), ("120", 120.0), ("0", 0.0),
                                           ("  900 ", 900.0), ("soon", 600.0),
                                           ("-5", 600.0), ("nan", 600.0)])
def test_the_threshold_comes_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv(site_apply.ENV_IDLE_S, raw)

    assert site_apply.idle_threshold_s() == expected


def test_a_reset_dashboard_password_is_applied_once_the_hostapp_reads_it_back():
    rig = Rig(reasons=(), dash_differs=True)

    rig.run_for(600)
    assert rig.watcher.tick() == "applied" and rig.restarts == 1
    assert rig.watcher.status()["reasons"] == ["dashboard_password"]


def test_a_queued_reason_drops_once_the_hostapp_runs_the_new_value():
    rig = Rig(reasons=(), dash_differs=False)
    rig.watcher.queue("dashboard_password")

    assert rig.watcher.tick() == "nothing_pending"
    assert rig.watcher.queued() == []


def test_a_queued_reason_is_applied_even_by_a_hostapp_that_cannot_report_it():
    rig = Rig(reasons=())          # no EMORI_DASH_PASSWORD row at all (an older hostapp)
    rig.watcher.queue("dashboard_password")
    rig.watcher.queue("Not A Reason!")

    rig.run_for(600)
    assert rig.watcher.tick() == "applied"
    assert rig.watcher.queued() == []


def test_status_says_what_it_is_waiting_for():
    rig = Rig()
    rig.run_for(180)

    status = rig.watcher.status()
    assert status["enabled"] is True and status["idle_s"] == 600.0
    assert status["last_decision"] == "waiting_idle"
    assert status["reasons"] == ["scope_changed"]
    # Ticks at 0, 60 and 120 s; the clock now reads 180 s past the first idle one.
    assert status["idle_for_s"] == 180.0 and status["applied_ago_s"] is None


def test_the_restart_is_logged_with_its_reasons(caplog):
    rig = Rig(reasons=("scope_changed", "dashboard_password"))
    rig.run_for(600)

    with caplog.at_level("WARNING", logger="emori.admin.site_apply"):
        rig.watcher.tick()

    assert any("auto-applied scope_changed dashboard_password" in r.getMessage()
               for r in caplog.records)


def test_the_admin_service_runs_the_watcher_with_its_token_and_the_allowlisted_restart(
        monkeypatch, tmp_path):
    """emori-admin's own wiring: the hostapp calls carry the shared token, the restart is
    the allowlisted ``systemctl restart emori-hostapp.service``."""

    from emori_adapter.admin import server
    from test_admin_service_fakes import base_env, install

    base_env(monkeypatch, token="tok-123", state_file=str(tmp_path / "state.json"))
    run, http = install(monkeypatch)
    http.add("GET", "/api/admin/site", http_ok(_site(["scope_changed"])))
    http.add("GET", "/api/admin/summary", http_ok(IDLE))
    run.add(["sudo", "systemctl", "restart", "emori-hostapp.service"])
    clock = {"now": 0.0}
    captured = {}

    real = site_apply.SiteApplyWatcher

    def build(**kw):
        watcher = real(clock=lambda: clock["now"], idle_s=60.0, **kw)
        captured["watcher"] = watcher
        return watcher

    monkeypatch.setattr(site_apply, "SiteApplyWatcher", build)
    server.create_app(background=False)
    watcher = captured["watcher"]

    assert watcher.tick() == "waiting_idle"
    clock["now"] = 60.0
    assert watcher.tick() == "applied"
    assert run.argv_for("sudo") == [["sudo", "systemctl", "restart", "emori-hostapp.service"]]
    assert all(c["headers"] == {"X-EMORI-Admin-Token": "tok-123"} for c in http.calls)
