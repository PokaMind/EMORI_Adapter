"""Readycheck's ``clock`` gate: this robot's clock, as Pokamind judges what it signs.

A CM4 has no RTC battery. After a power-off its clock is wrong until NTP syncs, and
Pokamind refuses a control-plane assertion (whoami) whose iat is more than
2 s in its future or whose exp (iat + 45) is more than 2 s in its past; a learner login
tolerates 60 s ahead and 600 s behind. A robot whose NTP is blocked therefore fails every
signed call, which looks like a Pokamind or token problem. The gate measures the skew
against the Date header of the pokamind reachability probe -- one-second resolution, so
it only FAILs when the whole uncertainty range is outside the window.
"""

from __future__ import annotations

import email.utils

import pytest

from emori_adapter.hostapp.readycheck import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    ClockProbe,
    CommandResult,
    HttpResult,
    SystemProbe,
    collect,
    evaluate,
    exit_code,
    gate_clock,
    measure_skew,
)
from tests.test_readycheck import _by_name, _FakeProbe, _healthy

# Pokamind's clock in these tests: a whole second, as a Date header can only say.
SERVER = 1_791_000_000
DATE = email.utils.formatdate(SERVER, usegmt=True)


def _clock(ntp: str = "yes", skew: float | None = 0.2, uncertainty: float = 0.6):
    return _healthy(clock=ClockProbe(ntp=ntp, skew_s=skew, uncertainty_s=uncertainty))


def _gate(evidence):
    return _by_name(evaluate(evidence))["clock"]


# --- measuring against a one-second Date header -----------------------------------------


def test_the_skew_is_the_middle_of_what_the_date_header_allows():
    # The server wrote "SERVER" at some instant in [SERVER, SERVER + 1); this machine
    # sent at +10.2 and heard back at +10.8 on its own clock: 10 s ahead.
    skew, uncertainty = measure_skew(DATE, SERVER + 10.2, SERVER + 10.8)

    assert skew == pytest.approx(10.0)
    assert uncertainty == pytest.approx(0.8)  # half the round trip + half a second


def test_a_robot_behind_reads_negative():
    skew, _ = measure_skew(DATE, SERVER - 3600.0, SERVER - 3599.0)

    assert skew == pytest.approx(-3600.0)


@pytest.mark.parametrize("header, sent, received", [
    ("", SERVER, SERVER + 1),
    ("not a date", SERVER, SERVER + 1),
    ("Mon, 12 Oct 2026 10:00:00", SERVER, SERVER + 1),  # no zone: cannot be compared
    (DATE, 0.0, SERVER),                                 # never sent
    (DATE, SERVER + 1, SERVER),                          # heard before it was sent
])
def test_nothing_is_measured_from_a_missing_or_impossible_reading(header, sent, received):
    assert measure_skew(header, sent, received) is None


# --- the gate ---------------------------------------------------------------------------


def test_a_synchronized_robot_inside_the_window_passes():
    gate = _gate(_clock())

    assert (gate.status, gate.code) == (PASS, "clock_ntp_synchronized")


def test_a_robot_more_than_two_seconds_ahead_fails_with_the_ntp_fix():
    gate = _gate(_clock(skew=5.0, uncertainty=0.6))

    assert (gate.status, gate.code) == (FAIL, "clock_ahead_of_pokamind")
    assert "5.0 s ahead" in gate.hint
    assert "sudo timedatectl set-ntp true" in gate.hint
    assert "UDP 123" in gate.hint
    assert "learner logins" not in gate.hint  # 5 s ahead still logs learners in
    assert exit_code(evaluate(_clock(skew=5.0))) == 1


def test_a_robot_that_booted_with_yesterdays_clock_fails_both_planes():
    gate = _gate(_clock(ntp="no", skew=-86400.0, uncertainty=0.7))

    assert (gate.status, gate.code) == (FAIL, "clock_behind_pokamind")
    assert "behind" in gate.hint and "learner logins too" in gate.hint


def test_behind_by_less_than_an_assertion_lifetime_still_works_but_warns():
    """45 s of assertion lifetime absorb a robot that is behind: nothing is refused
    yet, but a synchronized clock does not sit ten seconds off."""

    gate = _gate(_clock(ntp="yes", skew=-10.0, uncertainty=0.6))

    assert (gate.status, gate.code) == (WARN, "clock_skew")
    assert "10.0 s behind" in gate.hint


def test_a_fail_needs_the_whole_uncertainty_range_outside_the_window():
    # 2.5 s ahead, +/- 0.6: it may be 1.9 s, which Pokamind accepts.
    unproven = _gate(_clock(ntp="no", skew=2.5, uncertainty=0.6))
    assert (unproven.status, unproven.code) == (WARN, "clock_skew")
    # ...and NTP's word settles an uncertain reading near the edge.
    assert _gate(_clock(ntp="yes", skew=1.8, uncertainty=0.6)).status == PASS


def test_unsynchronized_but_inside_the_window_is_a_warning():
    gate = _gate(_clock(ntp="no", skew=0.3, uncertainty=0.6))

    assert (gate.status, gate.code) == (WARN, "ntp_unsynchronized")
    assert "inside the limits" in gate.hint
    assert "sudo timedatectl set-ntp true" in gate.hint
    assert exit_code(evaluate(_clock(ntp="no", skew=0.3))) == 0


def test_unsynchronized_and_unmeasured_still_warns():
    gate = _gate(_clock(ntp="no", skew=None))

    assert (gate.status, gate.code) == (WARN, "ntp_unsynchronized")
    assert "pokamind_unreachable" in gate.hint  # a far-off clock breaks HTTPS too


def test_without_timedatectl_a_measurement_inside_the_window_passes():
    assert _gate(_clock(ntp="", skew=0.4)).code == "clock_within_tolerance"


def test_nothing_known_is_a_skip_never_a_pass():
    gate = _gate(_clock(ntp="", skew=None))

    assert (gate.status, gate.code) == (SKIP, "clock_unprobed")
    # ...and a record that predates the gate is exactly that.
    assert gate_clock(_healthy(clock=ClockProbe())).status == SKIP


# --- collection: the reachability probe IS the reference --------------------------------


class _ClockProbe(_FakeProbe):
    def __init__(self, environ, *, ntp="yes", **kwargs):
        super().__init__(environ, **kwargs)
        self._ntp = ntp
        self.ntp_asked = 0

    def ntp_synchronized(self) -> str:
        self.ntp_asked += 1
        return self._ntp


def test_collect_measures_against_the_pokamind_probes_date_header():
    answer = HttpResult(True, 200, None, "text/html", date=DATE,
                        sent_at=SERVER - 29.9, received_at=SERVER - 29.3)
    probe = _ClockProbe({"POKAMIND_BASE": "https://api.example.test"}, ntp="no",
                        answers={"https://api.example.test/": answer})

    clock = collect(probe).clock

    assert probe.ntp_asked == 1
    assert clock.ntp == "no"
    assert clock.skew_s == pytest.approx(-30.1)
    assert clock.uncertainty_s == pytest.approx(0.8)


def test_collect_without_an_answer_or_an_ntp_reader_measures_nothing():
    clock = collect(_FakeProbe({"POKAMIND_BASE": "https://api.example.test"})).clock

    assert clock == ClockProbe()


@pytest.mark.parametrize("result, expected", [
    (CommandResult(True, 0, "yes\n"), "yes"),
    (CommandResult(True, 0, "no\n"), "no"),
    (CommandResult(True, 1, "yes\n"), ""),        # timedatectl failed
    (CommandResult(True, 0, "maybe\n"), ""),
    (CommandResult(False), ""),                   # no timedatectl (a dev box)
])
def test_the_real_probe_reads_timedatectl(monkeypatch, result, expected):
    probe = SystemProbe()
    seen = []

    def command(argv, *, timeout_s=None):
        seen.append(argv)
        return result

    monkeypatch.setattr(probe, "command", command)

    assert probe.ntp_synchronized() == expected
    assert seen == [["timedatectl", "show", "-p", "NTPSynchronized", "--value"]]


def test_the_real_http_probe_records_the_date_and_when_it_asked(monkeypatch):
    import httpx

    class _Response:
        status_code = 200
        headers = {"content-type": "text/html", "date": DATE}

        def json(self):
            raise ValueError

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response())

    result = SystemProbe().http("https://api.example.test/")

    assert result.date == DATE
    assert 0 < result.sent_at <= result.received_at


def test_a_clock_slightly_ahead_only_needs_the_2_s_of_the_control_plane():
    """With no add-on needing a stricter clock, 1.4 s ahead is inside the window."""

    assert _gate(_healthy(clock=ClockProbe(ntp="yes", skew_s=1.4, uncertainty_s=0.6))
                 ).status == PASS
    assert _healthy().clock_strict_reason == ""


# --- a Date that is not Pokamind's clock NOW is never measured ---------------------------


def _collect_clock(ntp: str, **answer):
    """Pokamind's Date says SERVER, but this robot asked 30 s later on a synchronized
    clock: a cache kept the answer (Age 30), or a redirect took it elsewhere."""

    reply = HttpResult(True, 200, None, "text/html", date=DATE,
                       sent_at=SERVER + 30.2, received_at=SERVER + 30.8, **answer)
    probe = _ClockProbe({"POKAMIND_BASE": "https://api.example.test"}, ntp=ntp,
                        answers={"https://api.example.test/": reply})
    return collect(probe).clock


@pytest.mark.parametrize("answer, why", [
    ({"age": "30"}, "came from a cache (Age: 30 s)"),
    ({"age": "soon"}, "unreadable Age header"),
    ({"redirected": True}, "came through a redirect"),
])
def test_a_cached_or_redirected_answer_is_never_measured(answer, why):
    clock = _collect_clock("yes", **answer)

    assert clock.skew_s is None
    assert why in clock.unmeasurable
    # A synchronized robot is not failed for a cache's age: NTP's word stands.
    gate = gate_clock(_healthy(clock=clock))
    assert (gate.status, gate.code) == (PASS, "clock_ntp_synchronized")


def test_unmeasurable_without_ntps_word_is_said_so():
    unsynchronized = gate_clock(_healthy(clock=_collect_clock("no", age="30")))
    assert (unsynchronized.status, unsynchronized.code) == (WARN, "clock_unmeasurable")
    assert "Age: 30 s" in unsynchronized.hint and "sudo timedatectl set-ntp true" in \
        unsynchronized.hint
    assert exit_code([unsynchronized]) == 0

    unknown = gate_clock(_healthy(clock=_collect_clock("", redirected=True)))
    assert (unknown.status, unknown.code) == (SKIP, "clock_unmeasurable")
    assert "redirect" in unknown.hint


@pytest.mark.parametrize("age", ["", "0"])
def test_a_fresh_answer_is_measured_as_before(age):
    clock = _collect_clock("yes", age=age)

    assert clock.unmeasurable == ""
    assert clock.skew_s == pytest.approx(30.0)
    assert gate_clock(_healthy(clock=clock)).code == "clock_ahead_of_pokamind"


def test_the_real_http_probe_records_the_age_and_a_followed_redirect(monkeypatch):
    import httpx

    class _Response:
        status_code = 200
        headers = {"content-type": "text/html", "date": DATE, "age": "12"}
        history = [object()]

        def json(self):
            raise ValueError

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response())

    result = SystemProbe().http("https://api.example.test/")

    assert (result.age, result.redirected) == ("12", True)
