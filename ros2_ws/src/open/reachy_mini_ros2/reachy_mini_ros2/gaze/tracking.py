"""Which face to look at, and how to stop chasing its jitter.

# Vendored from reachy_mini 1.10.0 (reachy_mini/vision/face_detector.py,
# reachy_mini/vision/face_tracking.py, reachy_mini/vision/look_at.py,
# reachy_mini/media/camera_utils.py) -- Copyright 2025 Pollen Robotics, Apache-2.0
# (the same licence as this package; see THIRD_PARTY_NOTICES.md).
# Vendored rather than imported because (a) the pinned dev venv is reachy_mini 1.8.0,
# which has no vision/ and no intrinsics_for_size, so an import here would make every
# host test of this logic impossible; (b) the SDK's FaceDetector calls hf_hub_download
# in its CONSTRUCTOR, and this node runs with HF_HUB_OFFLINE=1 and no yunet in the
# cache; (c) face_tracking.py's thread is welded to the daemon's GStreamer pipe, which
# v2 deleted.  Changes from upstream are marked EMORI:.

numpy only.  No ROS, no onnxruntime, no relative import: loaded BY PATH by
`tests/test_gaze_tracker.py`, the convention `tests/test_reachy_video.py` documents.
The whole module works in NORMALISED [-1, 1] image space, which is what makes
`max_jump` and the filter's dead zone resolution-independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

#: EMORI: the confidence a face must reach before the robot will turn its head towards
#: it. Deliberately NOT the detector's own threshold: 0.6 is "this is a face", 0.75 is
#: "this is a face I am willing to aim nine servos at". Conflating the two would mean
#: raising the detector's threshold and losing the ROI topic's honest confidence.
DEFAULT_SELECT_SCORE = 0.75

#: EMORI: how much bigger a challenger must be before it can steal the lock, and for how
#: many consecutive ticks. Hysteresis in BOTH, because the failure to avoid is a head
#: oscillating between two people standing in a queue.
DEFAULT_SWITCH_MARGIN = 1.35
DEFAULT_SWITCH_TICKS = 5


@dataclass
class FaceObservation:
    """A face target in ``set_tracking_face`` form (face center normalized to [-1, 1])."""

    center: tuple[float, float] | None
    roll: float | None
    width: int
    height: int
    camera_matrix: NDArray[np.float64]
    distortion: NDArray[np.float64]
    timestamp: float
    # EMORI: the confidence, carried through so the component can publish it as the ROI
    # message's `c` field and as the `face_score` diagnostic without keeping a second
    # reference to the Face itself.
    score: float = 0.0


class AdaptiveCenterFilter:
    """Face-center smoother with fixed tracking parameters.

    EMORI: renamed from upstream's ``_AdaptiveCenterFilter`` (leading underscore) because
    it is imported by name here and by the tests; the body is untouched.
    """

    _ALPHA = 0.3
    _FAST_ALPHA = 0.6
    _MOVEMENT_THRESHOLD = 0.15
    _DEAD_ZONE = 0.02

    def __init__(self) -> None:
        self._value: NDArray[np.float64] | None = None
        self._previous_input: NDArray[np.float64] | None = None

    def update(self, center: tuple[float, float]) -> tuple[float, float]:
        """Consume one raw center and return the filtered center."""
        current = np.asarray(center, dtype=np.float64)
        if self._value is None or self._previous_input is None:
            self._value = current.copy()
            self._previous_input = current.copy()
            return center

        movement = float(np.linalg.norm(current - self._previous_input))
        self._previous_input = current.copy()
        delta = current - self._value
        if float(np.linalg.norm(delta)) < self._DEAD_ZONE:
            return (float(self._value[0]), float(self._value[1]))

        alpha = self._FAST_ALPHA if movement > self._MOVEMENT_THRESHOLD else self._ALPHA
        self._value += alpha * delta
        return (float(self._value[0]), float(self._value[1]))

    def reset(self) -> None:
        """Forget filter history so the next observation is accepted immediately."""
        self._value = None
        self._previous_input = None


def _area(face: Any) -> float:
    return face.bbox[2] * face.bbox[3]


def _center(face: Any, width: int, height: int) -> tuple[float, float]:
    # Aim at the nose, because centering on the eye midpoint makes the robot look slightly above.
    return (
        face.nose[0] / max(width - 1, 1) * 2 - 1,
        face.nose[1] / max(height - 1, 1) * 2 - 1,
    )


def _dist2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


class Tracker:
    """Track one face: acquire largest, associate nearest, drop after misses.

    EMORI wraps upstream's rules rather than replacing them: the confidence gate
    and the challenger rule are applied around ``_select_upstream``, whose body is
    byte-for-byte 1.10.0's ``Tracker.select``, so the diff against the SDK stays
    readable and a future SDK bump is a re-copy rather than a re-derivation.
    """

    def __init__(
        self,
        min_area_frac: float = 0.003,
        max_jump: float = 0.5,
        max_misses: int = 20,
        *,
        select_score: float = DEFAULT_SELECT_SCORE,
        switch_margin: float = DEFAULT_SWITCH_MARGIN,
        switch_ticks: int = DEFAULT_SWITCH_TICKS,
    ) -> None:
        """Create a tracker with the given selection gates."""
        self._min_area_frac = min_area_frac
        self._max_jump = max_jump
        self._max_misses = max_misses
        self._select_score = float(select_score)
        self._switch_margin = float(switch_margin)
        self._switch_ticks = max(1, int(switch_ticks))
        self._center: tuple[float, float] | None = None
        self._misses = 0
        self._challenger_run = 0
        #: How many times the lock has moved to a bigger, closer face. Published as the
        #: `gaze_switches` diagnostic, and the component re-mints the face id on each.
        self.switches = 0
        #: Faces in the last processed frame, and how many of them were eligible. Both
        #: are diagnostics: "the detector sees three people, one of them well enough".
        self.last_detected = 0
        self.last_eligible = 0

    # --- EMORI: the wrapper -------------------------------------------------
    def select(self, faces: list[Any], width: int, height: int) -> Any | None:
        """Pick the face to aim at: the closest one seen with high confidence.

        "Closest" is largest bounding-box area, and that is an approximation stated as
        one: at 320x240 through a ~98 deg lens, apparent face width is a monotone proxy
        for distance among faces at comparable bearing. It is not a range sensor. A
        child at 0.5 m and an adult at 2 m differ in area by roughly 4:1, so the failure
        this could produce - a far adult outranking a near child - needs the nearer face
        to be less than half the pixel size of the farther one, which at conversational
        distances it is not.

        The confidence gate applies to acquisition AND retention: a face below
        `select_score` is invisible here, so a locked face whose confidence decays is
        counted as a miss on every tick and its lock is released after `max_misses` -
        the same path a face that walked out of frame takes, which is the behaviour the
        relax logic downstream is already written against.
        """

        self.last_detected = len(faces)
        eligible = [face for face in faces
                    if float(getattr(face, "score", 0.0)) >= self._select_score]
        self.last_eligible = len(eligible)

        locked = self._select_upstream(eligible, width, height)
        challenger = self._challenger(eligible, locked, width, height)
        if challenger is None:
            return locked

        # A deliberate re-acquisition: the lock moves and the caller re-mints the id.
        self.switches += 1
        self._center = _center(challenger, width, height)
        self._misses = 0
        return challenger

    def _challenger(self, eligible: list[Any], locked: Any | None,
                    width: int, height: int) -> Any | None:
        """The face that has earned the lock, or None.

        Without this the component follows the first person it ever saw until they leave
        the frame for four seconds - so somebody walking past behind a learner captures
        the robot for the rest of the session. With it, stealing the lock costs being
        35% larger for a full second running.
        """

        if locked is None:
            self._challenger_run = 0
            return None
        others = [face for face in eligible if face is not locked]
        if not others:
            self._challenger_run = 0
            return None
        best = max(others, key=_area)
        if _area(best) <= _area(locked) * self._switch_margin:
            self._challenger_run = 0
            return None
        self._challenger_run += 1
        if self._challenger_run < self._switch_ticks:
            return None
        self._challenger_run = 0
        return best

    # --- upstream, unchanged ------------------------------------------------
    def _select_upstream(self, faces: list[Any], width: int, height: int) -> Any | None:
        """Pick the face to aim at, or None when no plausible target is present."""
        if not faces:
            self._miss()
            return None
        if self._center is None:
            face = max(faces, key=_area)
            if _area(face) < self._min_area_frac * width * height:
                self._miss()
                return None
        else:
            center = self._center
            face = min(faces, key=lambda f: _dist2(_center(f, width, height), center))
            if _dist2(_center(face, width, height), center) > self._max_jump**2:
                self._miss()
                return None
        self._center = _center(face, width, height)
        self._misses = 0
        return face

    def _miss(self) -> None:
        self._misses += 1
        if self._misses > self._max_misses:
            self._center = None

    @property
    def has_target(self) -> bool:
        """Whether the tracker is still associated with a face."""
        return self._center is not None


def to_observation(
    face: Any | None,
    width: int,
    height: int,
    camera_matrix: NDArray[np.float64],
    distortion: NDArray[np.float64],
    timestamp: float,
) -> FaceObservation:
    """Reduce the tracked face (or its absence) to one normalized observation."""
    if face is None:
        return FaceObservation(
            None, None, width, height, camera_matrix, distortion, timestamp
        )
    roll = float(
        np.arctan2(
            face.left_eye[1] - face.right_eye[1],
            face.left_eye[0] - face.right_eye[0],
        )
    )
    return FaceObservation(
        _center(face, width, height),
        roll,
        width,
        height,
        camera_matrix,
        distortion,
        timestamp,
        # EMORI: see FaceObservation.score.
        float(getattr(face, "score", 0.0)),
    )
