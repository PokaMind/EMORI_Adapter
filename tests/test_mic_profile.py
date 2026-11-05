"""Mode-scoped capture gain: one microphone, two irreconcilable levels.

The robot has a single capture source. The roleplay session hears a person standing a
metre or two away and needs ~1.4; an idle robot stays
at 0.30, so room noise does not read as speech (see mic_profile.py). These
tests pin the two levels, the "never crash the hostapp" posture, and the idempotency
that keeps a per-turn caller from shelling out on every event.

They also pin the third source of a level: the phone superadmin's persisted override
(``admin/state.py``), written by a DIFFERENT process and read per transition, which
outranks the deployment's own env.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from emori_adapter.hostapp.mic_profile import IDLE_PROFILE, SESSION_PROFILE, MicProfile


class _RecordingRunner:
    """Stands in for ``subprocess.run``; records argv, returns/raises to order."""

    def __init__(self, *, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode, "", "")


def _volumes(runner: _RecordingRunner) -> list[str]:
    return [argv[-1] for argv in runner.calls]


@pytest.fixture(autouse=True)
def _levels_from_the_defaults(monkeypatch, tmp_path):
    """A developer's own .env (loaded by any earlier test that imported the hostapp)
    must not decide what these tests measure; the tuning tests set them back.

    The persisted override file is pointed at tmp for the same reason, and one step
    further: the default path is ``artifacts/admin_state.json`` inside the repo, so a
    real slider setting on a developer's box would otherwise silently outrank every
    level asserted below."""
    monkeypatch.delenv("EMORI_MIC_SESSION_VOL", raising=False)
    monkeypatch.delenv("EMORI_MIC_IDLE_VOL", raising=False)
    monkeypatch.setenv("EMORI_ADMIN_STATE_FILE", str(tmp_path / "admin_state.json"))


def test_session_profile_applies_the_measured_far_field_gain():
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    assert mic.set_profile(SESSION_PROFILE) is True
    assert runner.calls == [["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", "1.40"]]
    assert mic.profile == SESSION_PROFILE


def test_idle_profile_applies_the_guard_calibrated_gain():
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    assert mic.set_profile(IDLE_PROFILE) is True
    assert _volumes(runner) == ["0.30"]


def test_levels_are_env_tunable(monkeypatch):
    """Only the robot can settle these numbers; a retune must need no code change."""
    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "1.75")
    monkeypatch.setenv("EMORI_MIC_IDLE_VOL", "0.45")
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)
    assert _volumes(runner) == ["1.75", "0.45"]


def test_env_is_read_per_transition_not_frozen_at_construction(monkeypatch):
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)
    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "1.10")

    mic.set_profile(SESSION_PROFILE)
    assert _volumes(runner) == ["1.10"]


def test_same_profile_twice_shells_out_once():
    """Session start / end can fire repeatedly; the no-op case must stay free."""
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    assert mic.set_profile(IDLE_PROFILE) is True
    assert mic.set_profile(IDLE_PROFILE) is False
    assert len(runner.calls) == 1


def test_switching_back_reapplies():
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(IDLE_PROFILE)
    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)
    assert _volumes(runner) == ["0.30", "1.40", "0.30"]


def test_missing_wpctl_never_raises_and_is_not_latched():
    """A dev box (or a robot without PipeWire tooling) must still start the hostapp."""
    runner = _RecordingRunner(raises=FileNotFoundError("wpctl"))
    mic = MicProfile(run=runner)

    assert mic.set_profile(SESSION_PROFILE) is False
    assert mic.profile is None
    # Not latched: the source is at an unknown level, so the same profile must be
    # retried rather than skipped as an already-applied no-op.
    assert mic.set_profile(SESSION_PROFILE) is False
    assert len(runner.calls) == 2


def test_wpctl_failure_modes_are_all_survivable():
    for failure in (
        OSError("no such device"),
        subprocess.TimeoutExpired(cmd="wpctl", timeout=5.0),
        subprocess.SubprocessError("boom"),
    ):
        runner = _RecordingRunner(raises=failure)
        mic = MicProfile(run=runner)
        assert mic.set_profile(SESSION_PROFILE) is False
        assert mic.profile is None


def test_nonzero_exit_is_not_remembered():
    runner = _RecordingRunner(returncode=3)
    mic = MicProfile(run=runner)

    assert mic.set_profile(IDLE_PROFILE) is False
    assert mic.profile is None


def test_recovery_after_a_failure_applies_the_level():
    runner = _RecordingRunner(raises=FileNotFoundError("wpctl"))
    mic = MicProfile(run=runner)
    assert mic.set_profile(SESSION_PROFILE) is False

    runner.raises = None
    assert mic.set_profile(SESSION_PROFILE) is True
    assert mic.profile == SESSION_PROFILE


def test_unknown_profile_leaves_the_gain_alone():
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    assert mic.set_profile("loud") is False
    assert runner.calls == []


def test_unparseable_level_falls_back_to_the_measured_default(monkeypatch):
    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "loud")
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    assert _volumes(runner) == ["1.40"]


def test_absurd_level_is_clamped(monkeypatch):
    """`140` is percent, not a multiplier: clamp instead of deafening the mic."""
    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "140")
    monkeypatch.setenv("EMORI_MIC_IDLE_VOL", "-2")
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)
    assert _volumes(runner) == ["4.00", "0.00"]


def test_default_runner_is_subprocess_run():
    """Nothing injected -> the real thing, so production wiring needs no argument."""
    assert MicProfile()._run is subprocess.run


@pytest.mark.parametrize("profile", [SESSION_PROFILE, IDLE_PROFILE])
def test_argv_targets_the_default_capture_source(profile):
    runner = _RecordingRunner()
    MicProfile(run=runner).set_profile(profile)
    assert runner.calls[0][:3] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@"]


# --- the phone superadmin's persisted override --------------------------------


def _persist(tmp_path, **fields) -> None:
    """Write the state file the way admin/state.py writes it (another process)."""

    (tmp_path / "admin_state.json").write_text(json.dumps(fields), encoding="utf-8")


def test_a_persisted_multiplier_outranks_the_env(monkeypatch, tmp_path):
    """The operator at the site has a phone, not a shell: the slider they watched
    take effect must outrank the value baked into the deploy."""

    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "1.75")
    monkeypatch.setenv("EMORI_MIC_IDLE_VOL", "0.45")
    _persist(tmp_path, mic_session_mult=2.2, mic_idle_mult=0.15)
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)

    assert _volumes(runner) == ["2.20", "0.15"]


def test_a_persisted_multiplier_outranks_the_measured_default(tmp_path):
    _persist(tmp_path, mic_session_mult=0.9)
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)

    # Only the field that was persisted moves; the other profile keeps its default.
    assert _volumes(runner) == ["0.90", "0.30"]


def test_each_profile_reads_only_its_own_field(tmp_path):
    _persist(tmp_path, mic_idle_mult=0.05)
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(IDLE_PROFILE)
    mic.set_profile(SESSION_PROFILE)

    assert _volumes(runner) == ["0.05", "1.40"]


def test_the_override_is_read_per_transition_not_frozen_at_construction(tmp_path):
    """The writer is a DIFFERENT process; a slider moved now must be felt at the
    next profile switch, with no restart and no invalidation protocol."""

    runner = _RecordingRunner()
    mic = MicProfile(run=runner)
    mic.set_profile(SESSION_PROFILE)

    _persist(tmp_path, mic_session_mult=2.0)
    mic.set_profile(IDLE_PROFILE)
    mic.set_profile(SESSION_PROFILE)

    assert _volumes(runner) == ["1.40", "0.30", "2.00"]


def test_no_state_file_leaves_the_env_in_charge(monkeypatch, tmp_path):
    monkeypatch.setenv("EMORI_MIC_SESSION_VOL", "1.75")
    runner = _RecordingRunner()

    MicProfile(run=runner).set_profile(SESSION_PROFILE)

    assert not (tmp_path / "admin_state.json").exists()
    assert _volumes(runner) == ["1.75"]


@pytest.mark.parametrize("body", ["{not json", "[]", '{"mic_session_mult": "loud"}',
                                  '{"mic_session_mult": 140}',
                                  '{"mic_session_mult": null}'])
def test_an_unusable_state_file_falls_back_instead_of_raising(tmp_path, body):
    """A power cut mid-write, a hand-edited file, or a percent typed as a
    multiplier: none of them may keep the capture gain from moving."""

    (tmp_path / "admin_state.json").write_text(body, encoding="utf-8")
    runner = _RecordingRunner()

    assert MicProfile(run=runner).set_profile(SESSION_PROFILE) is True
    assert _volumes(runner) == ["1.40"]


def test_an_override_that_slips_past_the_state_validator_is_still_clamped(monkeypatch):
    """Belt and braces: _MAX_VOL is defined HERE, so this module enforces it here
    too rather than trusting a validator in another package to keep agreeing."""

    from emori_adapter.admin import state

    monkeypatch.setattr(state, "read_state", lambda *a, **k: {"mic_session_mult": 9.0,
                                                              "mic_idle_mult": -3.0})
    runner = _RecordingRunner()
    mic = MicProfile(run=runner)

    mic.set_profile(SESSION_PROFILE)
    mic.set_profile(IDLE_PROFILE)

    assert _volumes(runner) == ["4.00", "0.00"]


def test_an_unavailable_admin_state_module_never_stops_the_gain_moving(monkeypatch):
    """A partial deploy must degrade to the env defaults, not wedge the mic."""

    from emori_adapter.admin import state

    def _boom(*a, **k):
        raise RuntimeError("admin package half-installed")

    monkeypatch.setattr(state, "read_state", _boom)
    runner = _RecordingRunner()

    assert MicProfile(run=runner).set_profile(SESSION_PROFILE) is True
    assert _volumes(runner) == ["1.40"]
