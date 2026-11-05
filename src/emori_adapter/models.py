"""Internal normalised data models for the EMORI adapter.

Three layers of data:

1. ``AnalysisCycle``  - one ~2 s cycle of Pokamind analysis (the *input* contract).
2. ``DerivedSignals`` - normalised engagement / affect / context (the ROS4HRI semantics).
3. ``MotionIntent``   - a robot-agnostic behaviour command (the *output* contract).

Keeping these as explicit, validated models (pydantic v2) means the mock source,
the CSV replay, and the live WebSocket client all converge on the
same shape, and the ROS 2 publishers have one stable thing to serialise.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class EngagementLevel(IntEnum):
    """Mirrors ``hri_msgs/EngagementLevel`` constants exactly (REP-155)."""

    UNKNOWN = 0
    DISENGAGED = 1
    ENGAGING = 2
    ENGAGED = 3
    DISENGAGING = 4


# hri_msgs/Expression string constants we target (core circumplex labels).
ExpressionLabel = Literal[
    "neutral",
    "happy",
    "sad",
    "angry",
    "surprised",
    "scared",
    "disgusted",
]


class AnalysisCycle(BaseModel):
    """One per-cycle slice of Pokamind multimodal analysis.

    Field shapes follow the live analysis output (the 4x5 metric dicts and the
    body-language fields). All analysis fields are optional so a text-only or
    camera-off session still produces a valid (sparser) cycle.
    """

    timestamp: str = Field(..., description="ISO-8601 timestamp of the cycle")
    session_id: str
    person_id: str = Field(..., description="Stable ROS4HRI person id for the learner")
    seq: int = Field(0, description="Monotonic cycle index within the session")

    # 31 communication styles -> percentage 0..100 (rolling-window values).
    communication_styles: dict[str, float] = Field(default_factory=dict)

    # 4 x 5 per-modality metric dimensions, each 0..100.
    facial_metrics: dict[str, float] = Field(default_factory=dict)
    tone_metrics: dict[str, float] = Field(default_factory=dict)
    body_language_metrics: dict[str, float] = Field(default_factory=dict)
    verbal_metrics: dict[str, float] = Field(default_factory=dict)

    # Categorical body language (11 fields) + raw prosody features (optional).
    body_language: dict[str, Any] = Field(default_factory=dict)
    prosody: dict[str, float] = Field(default_factory=dict)

    # Incremental transcription for this cycle (semantic context).
    transcript_delta: str = ""

    # Pass-through session metadata (module, scenario, technique, language...).
    meta: dict[str, Any] = Field(default_factory=dict)


class ContextSignal(BaseModel):
    """Temporal / semantic / relational context (the EMORI ``/context`` extension)."""

    engagement_trend: float = Field(0.0, description="d(engagement)/dt, smoothed, -1..1")
    escalation: float = Field(0.0, description="arousal-with-negative-valence trajectory, 0..1")
    rapport: float = Field(0.0, description="relational rapport estimate, 0..1")
    attention: float = Field(0.0, description="attention estimate, 0..1")
    dominant_style: str = "neutral"
    topic: str = ""
    note: str = ""


class DerivedSignals(BaseModel):
    """Normalised signals produced by the translation layer for one cycle."""

    timestamp: str
    session_id: str
    person_id: str
    seq: int = 0

    engagement_score: float = Field(0.0, ge=0.0, le=1.0)
    engagement_level: EngagementLevel = EngagementLevel.UNKNOWN

    valence: float = Field(0.0, ge=-1.0, le=1.0)
    arousal: float = Field(0.0, ge=-1.0, le=1.0)
    expression: ExpressionLabel = "neutral"
    expression_confidence: float = Field(0.0, ge=0.0, le=1.0)

    top_styles: list[tuple[str, float]] = Field(default_factory=list)
    context: ContextSignal = Field(default_factory=ContextSignal)


class HeadPose(BaseModel):
    """Convenience head pose in human-friendly units (mm + degrees).

    Robot clients convert to whatever the daemon expects (the REST API uses
    metres + radians; the SDK helper ``reachy_mini.utils.create_head_pose``
    defaults to metres + degrees — pass ``mm=True`` for millimetre inputs).
    """

    x: float = 0.0  # mm
    y: float = 0.0  # mm
    z: float = 0.0  # mm
    roll: float = 0.0  # deg
    pitch: float = 0.0  # deg
    yaw: float = 0.0  # deg


MotionKind = Literal["goto", "set_target", "antennas", "look_at", "play_sound", "wobble", "wake_up", "play_recorded"]


class MotionIntent(BaseModel):
    """A single robot-agnostic behaviour command.

    The behaviour engine emits these; the robot client realises them. ``reason``
    is carried purely for logging / telemetry so a watcher can see *why* the robot
    did something (useful for the trainer dashboard story).
    """

    kind: MotionKind
    head: Optional[HeadPose] = None
    # radians, authored as (left, right) — the robot client swaps to the daemon's
    # [right, left] wire order (SetAntennasCmd) at the transport boundary.
    antennas: Optional[tuple[float, float]] = None
    body_yaw: Optional[float] = None  # radians
    duration: float = 0.6
    interpolation: Literal["linear", "minjerk", "ease_in_out", "cartoon"] = "minjerk"
    sound: Optional[str] = None
    wobble: Optional[bool] = None
    # Name of a Pollen recorded move (reachy-mini-emotions-library) for
    # kind="play_recorded"; duration then mirrors the recording's length so the
    # player can wait it out (the daemon plays the file in real time).
    move: Optional[str] = None
    priority: int = 0
    reason: str = ""
    # --- voice-locked performance --------------------------------------------
    # ``speech`` marks an intent that PERFORMS the robot's own voice (a per-sentence
    # cue plan). The player then ties the whole batch to the speaker output it taps:
    # the first intent holds (bounded) until the voice is actually audible, and the
    # batch jumps to its settle the moment the voice stops -- motion happens WHILE
    # the sentence is spoken, never before it starts and never after it ends.
    speech: bool = False
    # ``pace="voice"`` lets a beat wait for the next syllable onset instead of a
    # fixed sleep (bounded both ways), so gesticulation lands on the words.
    pace: Literal["fixed", "voice"] = "fixed"
    # ``antenna_voice`` scales the commanded antenna angles by the live voice level,
    # so the antennas read as the voice's own meter (quiet word, quiet antennas).
    antenna_voice: bool = False
    # ``tint`` is the family a speech plan performs, so that the beats carrying a sentence
    # past the end of its plan keep that family's colour instead of going generic.
    tint: str = ""
    # ``supersede`` cuts what this client's last move is still doing and clears anything
    # queued behind it, so this one starts now instead of after them.
    supersede: bool = False
