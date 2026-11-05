"""Everything the skill facade *decides*, as pure functions over plain data.

Which owner may POST, what each action's body looks like on the wire, what a 429 /
409 / 423 means, which counter a refusal belongs in, how an outcome fills an action
result: none of that needs ROS, a socket, or a robot to be correct, and all of it is
the part that breaks silently.  Keeping it here - stdlib only, no ``rclpy``, no
``emori_msgs``, no ``urllib`` - is what lets ``tests/test_skill_facade.py`` exercise
it on a dev box.

ROS 2 Humble runs in the container on the robot and nowhere else: not on the dev
boxes, not on the CM4 host, not in CI.  Without this split, the first thing to notice
that a refusal was mapped to the wrong code would be a robot standing in front of a
person, and the symptom would be "it just does not move".

The other two halves are ``transport.py`` (where a decision becomes somebody else's
wire format) and ``facade_node.py`` (where rclpy finally appears).  This module knows
about neither, and must not learn.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

# Which lane may move the robot. Mirrors hostapp/skill_api.py MOTION_OWNERS exactly,
# including its rule that an unrecognised value is NOT permission.
MOTION_OWNERS = ("hostapp", "ros", "none")
DEFAULT_MOTION_OWNER = "hostapp"

# What a status alone means when the body carried no ``error_code`` of its own (a
# truncated answer, a proxy, something that is not the intake). Only the three the
# intake genuinely answers with are named; anything else stays ``http_<status>``
# rather than being given a meaning it may not have.
_STATUS_CODES = {
    409: "not_owner",       # an add-on owns the robot / legacy motion disabled
    423: "head_reserved",   # push-turn selection is reading the head's own pose
    429: "rate_limited",    # the 12-slot queue is shared; excess is refused, not queued
}

# Refusals that will pass on their own, without an operator: the queue was busy, the
# head was borrowed for a gesture, or this caller's budget was spent. A node may
# reasonably try the next cue; the others mean "you are not the writer" and repeating
# the request changes nothing.
TRANSIENT_CODES = frozenset({"rate_limited", "head_reserved", "dropped"})

# The ledger. Each outcome lands in exactly one of these, so the five numbers add up
# to `posted` + `suppressed` and a robot can be read without a debugger:
#   accepted    - 200, the intents went into the queue
#   dropped     - 200, but the queue was busy with motion that outranks a skill request
#   refused     - the intake answered 4xx/5xx (see error_code for which)
#   suppressed  - never sent: this graph does not own motion
#   unreachable - the hostapp did not answer at all
COUNTER_NAMES = ("posted", "accepted", "dropped", "refused", "suppressed", "unreachable")


@dataclass(frozen=True)
class SkillOutcome:
    """What became of one skill request - the whole answer, in one value."""

    accepted: bool
    error_code: str = ""
    counter: str = "accepted"

    @property
    def transient(self) -> bool:
        """Would trying again later plausibly work?"""

        return self.error_code in TRANSIENT_CODES


def normalize_owner(raw: str) -> str:
    """``EMORI_ROS_MOTION_OWNER``, fail-closed.

    A typo degrades to the shipped default rather than to permission: the one thing
    this knob must never do is open the robot to a second writer on a misspelling.
    """

    value = (raw or "").strip().lower()
    return value if value in MOTION_OWNERS else DEFAULT_MOTION_OWNER


def may_post(owner: str) -> bool:
    """Only ``ros`` sends. ``hostapp`` and ``none`` decide, count, and stay quiet."""

    return normalize_owner(owner) == "ros"


def replay_motion_body(name: str, intensity: float) -> dict:
    """The ``/api/skill`` body for a bounded gesture name."""

    return {"skill": "replay_motion", "name": str(name), "intensity": float(intensity)}


def look_at_body(yaw_deg: float, pitch_deg: float, duration: float) -> dict:
    """The ``/api/skill`` body for a bounded gaze target."""

    return {"skill": "look_at", "yaw_deg": float(yaw_deg),
            "pitch_deg": float(pitch_deg), "duration": float(duration)}


def set_expression_body(label: str, confidence: float) -> dict:
    """The ``/api/skill`` body for an expression.

    The planner's ``intensity`` has no field in the contract and is deliberately NOT
    smuggled in as ``confidence``: the hostapp derives its own amplitude from the
    confidence the perception side actually measured, and overwriting that with a
    planning number would make every downstream reading of it a fiction.
    """

    return {"skill": "set_expression", "label": str(label),
            "confidence": float(confidence)}


def parse_error_code(status: int, body: str) -> str:
    """The intake's ``error_code`` for a refusal, or the honest fallback.

    The body is authoritative because a 403 is three different faults (disabled, bad
    key, not loopback) and an operator needs to know which. A body that is missing,
    truncated or not JSON still leaves the status, which is an answer.
    """

    try:
        payload = json.loads(body or "")
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("error_code") or "").strip()
        if code:
            return code
    return _STATUS_CODES.get(int(status), "http_%d" % int(status))


def accepted_outcome(body: str) -> SkillOutcome:
    """Map a 200 answer.

    ``{"ok": true, "dropped": true}`` is an honest answer and not a failure: the queue
    was busy with motion that outranks a skill request. It is reported as its own
    ``dropped`` code rather than as a bare "not completed", so an action result never
    says "no" without saying why.
    """

    try:
        payload = json.loads(body or "")
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict) and payload.get("dropped", False):
        return SkillOutcome(False, "dropped", "dropped")
    return SkillOutcome(True, "", "accepted")


def refusal_outcome(status: int, body: str) -> SkillOutcome:
    """Map a 4xx/5xx answer from the intake."""

    return SkillOutcome(False, parse_error_code(status, body), "refused")


def unreachable_outcome() -> SkillOutcome:
    """The hostapp did not answer at all - never confused with a refusal."""

    return SkillOutcome(False, "unreachable", "unreachable")


def suppressed_outcome() -> SkillOutcome:
    """This graph does not own motion, so nothing was sent.

    Reported with the same ``not_owner`` code the hostapp would have answered with, so
    a behaviour node reads one vocabulary whichever side refused.
    """

    return SkillOutcome(False, "not_owner", "suppressed")


def apply_outcome(result, outcome: SkillOutcome, *, done_field: str):
    """Fill an action result from an outcome.

    The two actions differ only in the name of their boolean (``completed`` for
    ReplayMotion, ``reached`` for LookAt), so the mapping lives here once instead of
    being re-derived - correctly, twice - in the two execute callbacks.
    """

    setattr(result, done_field, outcome.accepted)
    result.error_code = outcome.error_code
    return result


def new_counters() -> dict:
    return {name: 0 for name in COUNTER_NAMES}
