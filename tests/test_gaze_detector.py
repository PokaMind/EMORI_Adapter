"""The vendored YuNet detector: offline by construction, and cheap by configuration.

``reachy_mini_ros2/gaze/vision.py`` is Pollen's detector (Apache-2.0, vendored) with two
changes that are not style. Upstream calls ``hf_hub_download`` in its CONSTRUCTOR; the
robot node runs with ``HF_HUB_OFFLINE=1`` and no yunet in the cache, so that call is a
guaranteed exception on the robot and the feature would never load once. And upstream
leaves ONNX Runtime's memory arena on, on a box with ~266 MB free and 247 MB already in
swap where ``mem_limit`` is discarded by the kernel.

The real model file is the fixture, deliberately: the thing the robot runs is the thing
this asserts against, so a model swap that changed the output shape fails here rather
than on hardware.

Loaded by PATH: ``ros2_ws`` is not on ``sys.path`` and putting it there would need ROS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import time

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MODULE = (_ROOT / "ros2_ws" / "src" / "open" / "reachy_mini_ros2" / "reachy_mini_ros2"
           / "gaze" / "vision.py")
_MODULE_NAME = "reachy_mini_gaze_vision_under_test"
_MODEL = _ROOT / "deploy" / "ros2" / "models" / "face_detection_yunet_2026may.onnx"
_FIXTURE = _ROOT / "tests" / "fixtures" / "face_320x240.jpg"

pytestmark = pytest.mark.skipif(
    not _MODEL.exists(),
    reason=f"the YuNet weights are not at {_MODEL}")


def _load():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


vision = _load()


@pytest.fixture(scope="module")
def detector():
    return vision.FaceDetector(str(_MODEL))


@pytest.fixture(scope="module")
def face_frame_bgr():
    """The synthetic test face, as the component hands it to the detector.

    ``tests/fixtures/face_320x240.jpg`` is one frame of a generated synthetic-face clip
    (no real person), at the robot camera's 320x240.

    The camera thread publishes RGB (that is the caps the node's pipeline asks for) and
    the detector wants BGR, so the channel flip is part of the contract under test.
    """

    from PIL import Image

    rgb = np.asarray(Image.open(_FIXTURE).convert("RGB"), dtype=np.uint8)
    assert rgb.shape == (240, 320, 3)
    return np.ascontiguousarray(rgb[:, :, ::-1])


# --- 1. the two EMORI changes ---------------------------------------------------

def test_the_detector_never_reaches_for_huggingface(monkeypatch):
    """The offline guarantee, asserted where it can actually fail.

    A future re-vendor from a newer SDK would bring the download back with it, and the
    symptom on the robot is a WARN in the container log and a robot that never looks at
    anybody - which is indistinguishable from "nobody was in front of it".
    """

    huggingface_hub = pytest.importorskip("huggingface_hub")

    def _explode(*args, **kwargs):
        raise AssertionError("the vendored detector must not download anything")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", _explode)
    detector = vision.FaceDetector(str(_MODEL))
    assert detector.model_path == str(_MODEL)


def test_the_model_path_is_required_and_is_the_only_source():
    """Asserted over the parsed CODE, not the text: the attribution header names
    `hf_hub_download` on purpose, as the reason this file is vendored at all."""

    import ast

    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Name):
            assert node.id != "hf_hub_download"
    assert "huggingface_hub" not in imported
    assert imported <= {"__future__", "math", "dataclasses", "numpy", "onnxruntime"}

    with pytest.raises(TypeError):
        vision.FaceDetector()          # type: ignore[call-arg]


def test_the_session_is_built_single_threaded_with_both_arenas_off(monkeypatch):
    """`intra`/`inter = 1` keeps the detector off the cores running the 50 Hz loop;
    the two arena flags keep it from growing to the high-water mark of every allocation
    it ever made on a robot where nothing enforces a memory ceiling."""

    captured = {}
    real_session = vision.ort.InferenceSession

    def _record(path, options, **kwargs):
        captured["options"] = options
        return real_session(path, options, **kwargs)

    monkeypatch.setattr(vision.ort, "InferenceSession", _record)
    vision.FaceDetector(str(_MODEL))

    options = captured["options"]
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1
    assert options.enable_cpu_mem_arena is False
    assert options.enable_mem_pattern is False


# --- 2. what it detects ---------------------------------------------------------

def test_the_synthetic_face_is_found_exactly_once(detector, face_frame_bgr):
    faces = detector.detect(face_frame_bgr)
    assert len(faces) == 1

    face = faces[0]
    assert face.score >= 0.6
    x, y, w, h = face.bbox
    assert (x, y, w, h) == pytest.approx((123.7, 68.6, 73.3, 103.6), abs=10.0)


def test_the_confidence_survives_detection(detector, face_frame_bgr):
    """Upstream computes this for NMS and throws it away. Without it "the closest face
    detected with HIGH CONFIDENCE" is unanswerable and the ROI topic's `c` field has
    nothing honest to put in it."""

    face = detector.detect(face_frame_bgr)[0]
    assert 0.0 < face.score <= 1.0
    assert face.score == pytest.approx(0.90, abs=0.15)


def test_an_empty_room_detects_nobody(detector):
    """The case the robot spends most of its life in. A detector that hallucinates here
    is a robot that stares at a wall."""

    grey = np.full((240, 320, 3), 128, dtype=np.uint8)
    assert detector.detect(grey) == []


def test_coordinates_stay_in_the_ORIGINAL_frame_after_padding(detector, face_frame_bgr):
    """240 rows are padded up to 256 for YuNet's stride-32 pyramid. The padding is added
    at the BOTTOM so the origin is unchanged - but a decode that reported coordinates in
    padded space would put the head a few degrees low, for ever, and nothing would say
    so."""

    assert 240 % 32 != 0, "the padding path is the one under test here"
    face = detector.detect(face_frame_bgr)[0]
    x, y, w, h = face.bbox
    assert 0.0 <= y and y + h <= 240.0
    assert 0.0 <= x and x + w <= 320.0
    for point in (face.nose, face.left_eye, face.right_eye):
        assert 0.0 <= point[0] <= 320.0
        assert 0.0 <= point[1] <= 240.0


def test_the_score_threshold_is_honoured(face_frame_bgr):
    strict = vision.FaceDetector(str(_MODEL), score_threshold=0.99)
    assert strict.detect(face_frame_bgr) == []


def test_one_frame_is_cheap(detector, face_frame_bgr, capsys):
    """Reported, never asserted: this box is not a CM4. The number is here so that a
    change which makes the detector ten times slower is visible in the test output
    before somebody discovers it as a 50 Hz control loop that started missing ticks.
    """

    detector.detect(face_frame_bgr)                     # warm the session
    started = time.perf_counter()
    for _ in range(5):
        detector.detect(face_frame_bgr)
    per_frame_ms = (time.perf_counter() - started) / 5 * 1000.0
    with capsys.disabled():
        print(f"\n  YuNet 320x240: {per_frame_ms:.1f} ms/frame on this box "
              f"(a Cortex-A72 is ~8-15x slower single-threaded)")
