"""YuNet face detection on ONNX Runtime, loaded from an explicit path.

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

numpy + onnxruntime.  No ROS, no relative import: loaded BY PATH by
`tests/test_gaze_detector.py`, the convention `tests/test_reachy_video.py` documents.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

_STRIDES = (8, 16, 32)
_MAX_STRIDE = 32


@dataclass(frozen=True)
class Face:
    """A face detected in pixel coordinates: bounding box, eye centers, and nose."""

    bbox: tuple[float, float, float, float]
    right_eye: tuple[float, float]
    left_eye: tuple[float, float]
    nose: tuple[float, float]
    # EMORI: upstream computes this confidence for NMS and then throws it away, so
    # "the closest face detected with HIGH CONFIDENCE" was unanswerable and the ROI
    # topic had nothing honest to put in its `c` field. Fifth and DEFAULTED, so every
    # four-argument construction in upstream's own tests still works.
    score: float = 0.0


def _nms(
    boxes: list[tuple[float, float, float, float]],
    scores: list[float],
    iou_threshold: float,
) -> list[int]:
    # Greedy IoU non-maximum suppression; returns kept indices, highest score first.
    if not boxes:
        return []
    b = np.array(boxes, dtype=np.float32)
    x1, y1 = b[:, 0], b[:, 1]
    x2, y2 = b[:, 0] + b[:, 2], b[:, 1] + b[:, 3]
    area = b[:, 2] * b[:, 3]
    order = np.array(scores, dtype=np.float32).argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        w = np.clip(
            np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0.0, None
        )
        h = np.clip(
            np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0.0, None
        )
        inter = w * h
        iou = inter / (area[i] + area[rest] - inter)
        order = rest[iou <= iou_threshold]
    return keep


class FaceDetector:
    """Detect faces in BGR frames with YuNet on ONNX Runtime."""

    def __init__(
        self,
        model_path: str,
        score_threshold: float = 0.6,
        nms_threshold: float = 0.3,
    ) -> None:
        """Create the YuNet detector from a file on disk, confined to one CPU thread.

        EMORI: the path is REQUIRED and `hf_hub_download` is gone. Upstream downloads
        the weights in this constructor; this node runs with `HF_HUB_OFFLINE=1` and no
        yunet in the cache, so that call is a guaranteed exception on the robot. The model
        is baked into the image and bind-mounted instead.
        """

        options = ort.SessionOptions()
        # Confine the detector to one thread so it can't spread across cores and starve
        # the 50 Hz control loop.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        # EMORI: both arenas OFF. The robot has ~266 MB free with 247 MB already in swap,
        # and the compose file's `mem_limit` is discarded by this kernel - so nothing
        # enforces a ceiling and ORT's default arena, which grows to the high-water mark
        # of every allocation it has ever made and never returns it, is the one thing in
        # this component that can push the control loop into swap.
        options.enable_cpu_mem_arena = False
        options.enable_mem_pattern = False
        self._session = ort.InferenceSession(
            model_path, options, providers=["CPUExecutionProvider"]
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._score_threshold = score_threshold
        self._nms_threshold = nms_threshold
        self.model_path = str(model_path)

    def detect(self, frame_bgr: NDArray[np.uint8]) -> list[Face]:
        """Return every face detected in a BGR frame."""
        height, width = frame_bgr.shape[:2]
        # YuNet's feature-pyramid fusion needs stride-aligned dims; pad up (origin unchanged).
        padded_h = math.ceil(height / _MAX_STRIDE) * _MAX_STRIDE
        padded_w = math.ceil(width / _MAX_STRIDE) * _MAX_STRIDE
        if (padded_h, padded_w) != (height, width):
            padded = np.zeros((padded_h, padded_w, 3), dtype=frame_bgr.dtype)
            padded[:height, :width] = frame_bgr
            frame_bgr = padded
        blob = frame_bgr.astype(np.float32).transpose(2, 0, 1)[np.newaxis]
        outputs = dict(
            zip(self._output_names, self._session.run(None, {self._input_name: blob}))
        )
        return self._decode(outputs, padded_w)

    def _decode(
        self, outputs: dict[str, NDArray[np.float32]], width: int
    ) -> list[Face]:
        boxes: list[tuple[float, float, float, float]] = []
        scores: list[float] = []
        faces: list[Face] = []
        for stride in _STRIDES:
            cls = outputs[f"cls_{stride}"][0, :, 0]
            obj = outputs[f"obj_{stride}"][0, :, 0]
            # YuNet's confidence is the geometric mean of the classification and objectness heads.
            score = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
            idx = np.nonzero(score >= self._score_threshold)[0]
            if idx.size == 0:
                continue
            bbox = outputs[f"bbox_{stride}"][0][idx]
            kps = outputs[f"kps_{stride}"][0][idx]
            cols = width // stride
            col = (idx % cols).astype(np.float32)
            row = (idx // cols).astype(np.float32)
            cx = (col + bbox[:, 0]) * stride
            cy = (row + bbox[:, 1]) * stride
            w = np.exp(bbox[:, 2]) * stride
            h = np.exp(bbox[:, 3]) * stride
            for k in range(idx.size):
                box = (
                    float(cx[k] - w[k] / 2),
                    float(cy[k] - h[k] / 2),
                    float(w[k]),
                    float(h[k]),
                )
                boxes.append(box)
                scores.append(float(score[idx[k]]))
                faces.append(
                    Face(
                        bbox=box,
                        right_eye=(
                            float((col[k] + kps[k, 0]) * stride),
                            float((row[k] + kps[k, 1]) * stride),
                        ),
                        left_eye=(
                            float((col[k] + kps[k, 2]) * stride),
                            float((row[k] + kps[k, 3]) * stride),
                        ),
                        nose=(
                            float((col[k] + kps[k, 4]) * stride),
                            float((row[k] + kps[k, 5]) * stride),
                        ),
                        # EMORI: the confidence, kept rather than discarded.
                        score=float(score[idx[k]]),
                    )
                )
        return [faces[i] for i in _nms(boxes, scores, self._nms_threshold)]
