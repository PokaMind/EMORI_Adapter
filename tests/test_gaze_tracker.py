"""Which face the robot locks on to, and what it takes to make it change its mind.

``reachy_mini_ros2/gaze/tracking.py`` is Pollen's tracker (Apache-2.0, vendored) with the two
things the requirement needs and upstream does not have: a CONFIDENCE gate, and a way
back out of a lock. Upstream acquires the largest box above a minimum area, ignores the
detector's score entirely, and then follows nearest-to-previous-centre for ever - so it
would happily aim nine servos at a 0.61-confidence smudge, and once locked on somebody
walking past behind a learner it would keep them for the rest of the session.

Every rule here is pure, so it is tested as arithmetic rather than on a robot.

Loaded by PATH: ``ros2_ws`` is not on ``sys.path`` and putting it there would need ROS.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

_MODULE = (Path(__file__).resolve().parents[1] / "ros2_ws" / "src" / "open" / "reachy_mini_ros2"
           / "reachy_mini_ros2" / "gaze" / "tracking.py")
_MODULE_NAME = "reachy_mini_gaze_tracking_under_test"


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


tracking = _load()

_W, _H = 320, 240


@dataclass(frozen=True)
class _Face:
    """The four fields the tracker reads off a detection, plus the score.

    Deliberately NOT `gaze.vision.Face`: that module imports onnxruntime, and none of
    the logic under test here needs a detector to exist.
    """

    bbox: tuple[float, float, float, float]
    right_eye: tuple[float, float]
    left_eye: tuple[float, float]
    nose: tuple[float, float]
    score: float = 0.9


def _face(cx: float, cy: float, size: float, score: float = 0.9) -> _Face:
    """A square face of `size` pixels centred on `(cx, cy)`, nose at the centre."""

    half = size / 2.0
    return _Face(
        bbox=(cx - half, cy - half, size, size),
        right_eye=(cx - size / 6.0, cy - size / 8.0),
        left_eye=(cx + size / 6.0, cy - size / 8.0),
        nose=(cx, cy),
        score=score,
    )


def _select(tracker, faces):
    return tracker.select(list(faces), _W, _H)


# --- 1. acquisition -------------------------------------------------------------

def test_acquisition_takes_the_largest_face_that_is_confident_enough():
    """"Closest" is largest-bbox, but only among faces the robot is willing to look at.
    A 0.6 detection is "this is a face"; 0.75 is "this is a face I will aim at"."""

    tracker = tracking.Tracker()
    big_but_unsure = _face(220.0, 120.0, 80.0, score=0.65)
    smaller_and_sure = _face(100.0, 120.0, 40.0, score=0.92)

    assert _select(tracker, [big_but_unsure, smaller_and_sure]) is smaller_and_sure


def test_a_frame_with_nothing_confident_enough_acquires_nothing():
    tracker = tracking.Tracker()
    assert _select(tracker, [_face(160.0, 120.0, 90.0, score=0.74)]) is None
    assert tracker.has_target is False


def test_a_face_too_small_to_be_a_person_is_refused():
    """`min_area_frac` 0.003 at 320x240 is 230 px^2 - about a 15 px face, roughly 3 m
    away. Below that the box is as likely to be a pattern on a wall."""

    tracker = tracking.Tracker()
    assert _select(tracker, [_face(160.0, 120.0, 14.0)]) is None
    assert _select(tracker, [_face(160.0, 120.0, 20.0)]) is not None


def test_the_detected_and_eligible_counts_are_reported():
    """Both are /diagnostics keys: "the detector sees three people, one of them well
    enough to look at" is a different situation from "the detector sees nobody"."""

    tracker = tracking.Tracker()
    _select(tracker, [_face(100.0, 120.0, 40.0, score=0.9),
                      _face(200.0, 120.0, 40.0, score=0.62),
                      _face(260.0, 120.0, 40.0, score=0.5)])
    assert (tracker.last_detected, tracker.last_eligible) == (3, 1)


# --- 2. retention ---------------------------------------------------------------

def test_a_locked_face_whose_confidence_decays_is_released_like_a_miss():
    """The gate applies to retention as well, and it releases through the SAME path a
    face that walked out of frame takes - which is the path the relax logic downstream
    is already written against."""

    tracker = tracking.Tracker(max_misses=20)
    assert _select(tracker, [_face(160.0, 120.0, 40.0, score=0.9)]) is not None

    faded = _face(160.0, 120.0, 40.0, score=0.5)
    for _ in range(20):
        assert _select(tracker, [faded]) is None
        assert tracker.has_target is True, "a dip must not drop the lock immediately"
    assert _select(tracker, [faded]) is None
    assert tracker.has_target is False


def test_twenty_one_empty_frames_drop_the_lock():
    """20 misses at 5 Hz is 4.0 s, which is `gaze.relax_s` by construction: the tracker
    lets go at the same moment the controller relaxes to its rest pose."""

    tracker = tracking.Tracker(max_misses=20)
    assert _select(tracker, [_face(160.0, 120.0, 40.0)]) is not None
    for _ in range(20):
        assert _select(tracker, []) is None
        assert tracker.has_target is True
    assert _select(tracker, []) is None
    assert tracker.has_target is False


def test_a_face_that_teleports_is_not_the_same_face():
    """`max_jump` is in normalised units, so it means the same thing at any resolution.
    A lock that followed an arbitrary jump would hand the robot to whoever walked in."""

    tracker = tracking.Tracker()
    assert _select(tracker, [_face(100.0, 120.0, 40.0)]) is not None
    assert _select(tracker, [_face(310.0, 120.0, 40.0)]) is None
    assert tracker.has_target is True, "one jump is a miss, not a release"


# --- 3. the challenger rule -----------------------------------------------------

def test_a_bigger_face_does_not_steal_the_lock_by_being_bigger_once():
    """Upstream follows nearest-to-previous-centre unconditionally. Keeping that is the
    point: a head that re-picked the largest box every tick would oscillate between two
    people standing side by side."""

    tracker = tracking.Tracker()
    locked = _face(100.0, 120.0, 40.0)
    challenger = _face(200.0, 120.0, 47.0)          # 2209 / 1600 = 1.38x
    assert _select(tracker, [locked]) is locked
    assert _select(tracker, [locked, challenger]) is locked


def test_a_challenger_that_is_bigger_for_a_full_second_takes_the_lock():
    """1.36x for 5 consecutive ticks at 5 Hz. Without this the component follows the
    first person it ever saw until they leave the frame for four seconds."""

    tracker = tracking.Tracker(switch_margin=1.35, switch_ticks=5)
    locked = _face(100.0, 120.0, 40.0)              # area 1600
    challenger = _face(200.0, 120.0, 47.0)          # area 2209, ratio 1.381

    assert _select(tracker, [locked]) is locked
    for tick in range(4):
        assert _select(tracker, [locked, challenger]) is locked, f"tick {tick}"
    assert _select(tracker, [locked, challenger]) is challenger
    assert tracker.switches == 1


def test_a_challenger_just_under_the_margin_never_takes_the_lock():
    """Hysteresis in the RATIO as well as the run length: two people at nearly the same
    distance must not make the head twitch between them for the whole session."""

    tracker = tracking.Tracker(switch_margin=1.35, switch_ticks=5)
    locked = _face(100.0, 120.0, 40.0)              # area 1600
    challenger = _face(200.0, 120.0, 46.3)          # area 2143.7, ratio 1.340

    assert _select(tracker, [locked]) is locked
    for _ in range(50):
        assert _select(tracker, [locked, challenger]) is locked
    assert tracker.switches == 0


def test_the_challenger_run_resets_when_the_challenge_lapses():
    """Five CONSECUTIVE ticks. A challenger that appears and disappears is somebody
    walking past, not somebody who came to talk."""

    tracker = tracking.Tracker(switch_margin=1.35, switch_ticks=5)
    locked = _face(100.0, 120.0, 40.0)
    challenger = _face(200.0, 120.0, 47.0)

    assert _select(tracker, [locked]) is locked
    for _ in range(4):
        assert _select(tracker, [locked, challenger]) is locked
    assert _select(tracker, [locked]) is locked            # the challenge lapses
    for _ in range(4):
        assert _select(tracker, [locked, challenger]) is locked
    assert tracker.switches == 0


def test_a_challenger_below_the_confidence_gate_is_not_a_challenger():
    tracker = tracking.Tracker(switch_margin=1.35, switch_ticks=5)
    locked = _face(100.0, 120.0, 40.0)
    challenger = _face(200.0, 120.0, 90.0, score=0.6)

    assert _select(tracker, [locked]) is locked
    for _ in range(20):
        assert _select(tracker, [locked, challenger]) is locked
    assert tracker.switches == 0


# --- 4. the adaptive centre filter ----------------------------------------------

def test_the_dead_zone_returns_the_previous_value_identically():
    """Identically, not approximately: the deadband downstream compares against the
    last ACCEPTED target, so a filter that drifted by 1e-9 a tick would eventually
    walk the head across the room."""

    smoother = tracking.AdaptiveCenterFilter()
    assert smoother.update((0.0, 0.0)) == (0.0, 0.0)
    assert smoother.update((0.01, 0.0)) == (0.0, 0.0)
    assert smoother.update((0.0, 0.015)) == (0.0, 0.0)


def test_a_fast_movement_selects_the_fast_alpha():
    """0.3 normally, 0.6 when the face moved more than 0.15 between raw samples: a
    person turning their head should not be smoothed into arriving a second late."""

    fast = tracking.AdaptiveCenterFilter()
    fast.update((0.0, 0.0))
    assert fast.update((0.5, 0.0)) == pytest.approx((0.3, 0.0))

    slow = tracking.AdaptiveCenterFilter()
    slow.update((0.0, 0.0))
    assert slow.update((0.1, 0.0)) == pytest.approx((0.03, 0.0))


def test_reset_forgets_everything():
    smoother = tracking.AdaptiveCenterFilter()
    smoother.update((0.4, 0.4))
    smoother.reset()
    assert smoother.update((-0.2, 0.1)) == (-0.2, 0.1)


# --- 5. the observation ---------------------------------------------------------

def test_the_observation_carries_the_confidence_and_the_normalised_centre():
    """The score has to survive this far: it is the ROI message's `c` field and the
    `face_score` diagnostic, and nothing downstream keeps a reference to the Face."""

    K = np.eye(3)
    D = np.zeros(12)
    face = _face(240.0, 60.0, 40.0, score=0.94)
    obs = tracking.to_observation(face, _W, _H, K, D, 12.5)

    assert obs.score == pytest.approx(0.94)
    assert obs.center == pytest.approx((240.0 / 319 * 2 - 1, 60.0 / 239 * 2 - 1))
    assert obs.timestamp == 12.5
    assert (obs.width, obs.height) == (_W, _H)


def test_no_face_is_an_observation_too():
    """Every tick produces one, including the empty ones: that is what drives the
    hold/relax clock downstream."""

    obs = tracking.to_observation(None, _W, _H, np.eye(3), np.zeros(12), 1.0)
    assert obs.center is None
    assert obs.roll is None
    assert obs.score == 0.0


def test_the_roll_is_read_off_the_eye_line():
    tilted = _Face(bbox=(100.0, 100.0, 40.0, 40.0), right_eye=(110.0, 110.0),
                   left_eye=(130.0, 130.0), nose=(120.0, 120.0))
    obs = tracking.to_observation(tilted, _W, _H, np.eye(3), np.zeros(12), 0.0)
    assert obs.roll == pytest.approx(np.pi / 4)
