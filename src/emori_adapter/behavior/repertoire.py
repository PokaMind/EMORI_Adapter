"""Reachy Mini gesture repertoire.

Every gesture returns one or more :class:`MotionIntent`. Amplitudes are kept
gentle and within the real robot's expressive range:

* head: 6-DOF Stewart platform - we use roll/pitch/yaw in degrees (~+/-25 deg)
  and small z translation in mm;
* antennas: 2 joints in radians. The REAL expressive range, measured from
  Pollen's own recorded emotion library (the reachy-mini-emotions-library
  dataset cached on the robot): official moves sweep the antennas to +/-1.9..3.3
  rad (110..190 deg) in single committed arcs -- the antennas are the lead
  emotional instrument, not an accent. Authored here as ``(left, right)`` (how
  a human reads a gesture); the robot client swaps to the daemon's
  ``[right, left]`` wire order at the transport boundary
  (see robot/client.py::_antennas_wire);
* speaker: short backchannel sounds (only if the daemon has media enabled);
* "wobbling": the daemon's audio-reactive head micro-motion, toggled on/off.

There are deliberately NO eye / LED gestures: Reachy Mini has no eyes or LEDs
(see docs/hardware-notes.md). Expressivity
comes from head + antennas + body yaw + audio.

The last seven gestures here (``sink_toward_shell`` .. ``settle_to_attend``) are the
per-sentence *expression cue* primitives: they are shaped by ``behavior/cues.py``
from the Pokamind cue family of the sentence being spoken, so their amplitudes and
durations arrive already scaled and are NOT authored constants like the ones above.
They are the only gestures that touch head ``z`` (translation toward/away from the
shell) or ``body_yaw``, neither of which any shipped gesture has ever exercised -
so both go through the clamps below, which are env-tunable on the robot.
"""

from __future__ import annotations

import logging
import os

from emori_adapter.models import HeadPose, MotionIntent

log = logging.getLogger("emori.behavior.repertoire")

# Antenna postures (radians). Symmetric unless noted.
ANTENNA_REST = (0.0, 0.0)
ANTENNA_PERKED = (0.7, -0.7)  # alert / attentive (mirror outward)
ANTENNA_DROOPED = (-0.5, 0.5)  # low energy / empathetic
ANTENNA_CURIOUS = (0.5, 0.1)  # asymmetric tilt = curiosity
# The measured emotional range (Pollen's recorded moves use these magnitudes):
ANTENNA_BLOOM = (1.9, -1.9)       # relief/joy: thrown wide open (~110 deg)
ANTENNA_ALERT_WIDE = (1.4, -1.4)  # excitement at full spread
ANTENNA_DROOP_DEEP = (-1.6, 1.6)  # deep sadness: fully fallen

# Head z travel (mm, negative = sink into the body) used by the cue primitives.
# Measured on a Reachy Mini Wireless (18, which its hardware profile in
# site_settings supplies); the code default stays conservative for any other robot:
# bisect there, exactly like driver.attend_up_pitch_deg treats the session-start look-up.
CUE_Z_MM_DEFAULT = 8.0
_CUE_Z_MM_MAX = 20.0
# Body yaw likewise: on as measured on a Reachy Mini Wireless (its hardware
# profile); elsewhere every cue term is dropped until the robot operator turns it on after
# measuring it.
CUE_BODY_YAW_MAX_RAD = 0.35


def cue_z_limit_mm() -> float:
    """Clamp limit for cue head-z travel (mm), env-overridable and range-checked."""

    raw = (os.environ.get("EMORI_CUE_Z_MM", "") or "").strip()
    if not raw:
        return CUE_Z_MM_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("EMORI_CUE_Z_MM=%r is not a number; using %s", raw, CUE_Z_MM_DEFAULT)
        return CUE_Z_MM_DEFAULT
    clamped = min(_CUE_Z_MM_MAX, max(0.0, value))
    if clamped != value:
        log.warning("EMORI_CUE_Z_MM=%s is outside 0..%s; using %s", value, _CUE_Z_MM_MAX, clamped)
    return clamped


def cue_body_yaw_enabled() -> bool:
    """True only when the robot operator has measured body yaw and opted in."""

    return (os.environ.get("EMORI_CUE_BODY_YAW", "") or "").strip() in ("1", "true", "True", "yes", "on")


def clamp_cue_z(z_mm: float, *, limit: float | None = None) -> float:
    """Clamp a cue's head z travel to +/- the (env-tunable) limit."""

    bound = cue_z_limit_mm() if limit is None else min(_CUE_Z_MM_MAX, max(0.0, float(limit)))
    return max(-bound, min(bound, float(z_mm)))


def cue_body_yaw(radians: float | None, *, enabled: bool | None = None) -> float | None:
    """Return a bounded body-yaw term, or None when the term is disabled (default)."""

    if radians is None:
        return None
    if not (cue_body_yaw_enabled() if enabled is None else bool(enabled)):
        return None
    return max(-CUE_BODY_YAW_MAX_RAD, min(CUE_BODY_YAW_MAX_RAD, float(radians)))


def neutral_gaze(duration: float = 0.8, reason: str = "neutral gaze") -> MotionIntent:
    """Centred, slightly forward head - attentive resting pose.

    Head-only on purpose: the antenna posture is left to affect modulation
    (:meth:`BehaviorEngine._apply_affect`), so a calm-but-sad learner gets drooped
    antennas while a positive one gets perked antennas over the same neutral gaze.
    """
    return MotionIntent(kind="goto", head=HeadPose(pitch=4.0), duration=duration, reason=reason)


def nod(depth_deg: float = 16.0, duration: float = 0.4, reason: str = "backchannel nod") -> list[MotionIntent]:
    """A single, clearly visible acknowledging nod (down then back up)."""
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=depth_deg), duration=duration,
                     interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=-2.0), duration=duration,
                     interpolation="ease_in_out", reason=reason + " (return)"),
    ]


def head_tilt(angle_deg: float = 20.0, duration: float = 0.6, reason: str = "curious tilt") -> MotionIntent:
    """Sideways head tilt + asymmetric antennas - reads as curiosity/listening."""
    return MotionIntent(
        kind="goto", head=HeadPose(roll=angle_deg, yaw=angle_deg * 0.5),
        antennas=ANTENNA_CURIOUS, duration=duration, reason=reason,
    )


def gaze_to(yaw_deg: float, pitch_deg: float = 2.0, duration: float = 0.55,
            reason: str = "gaze shift") -> MotionIntent:
    """Shift gaze by orienting the head (approximate look-at via head yaw/pitch)."""
    return MotionIntent(
        kind="goto", head=HeadPose(yaw=yaw_deg, pitch=pitch_deg),
        duration=duration, reason=reason,
    )


def look_around(seq: int, reason: str = "attentive scan") -> MotionIntent:
    """Gentle, visible scan so an ENGAGED robot looks alive (left / centre / right)."""
    yaw = (18.0, 0.0, -18.0, 0.0)[seq % 4]
    pitch = 3.0 if seq % 2 == 0 else 6.0
    return MotionIntent(kind="goto", head=HeadPose(yaw=yaw, pitch=pitch),
                        duration=0.6, interpolation="minjerk", reason=reason)


def attentive_breath(seq: int, reason: str = "idle: listening") -> MotionIntent:
    """The listening idle: a held attentive pose with a slow breath, not a scan.

    While the learner has the floor, ``look_around``'s 18-degree sweep reads as a
    robot that stopped listening, and it is a real motor motion beside an open
    microphone. This keeps the head on the person (yaw 0, a slightly raised gaze),
    and only breathes: 1-2 degrees of pitch and a millimetre of z, alternating, with
    the antennas perked -- alive and attentive, and nearly silent.
    """

    up = seq % 2 == 0
    return MotionIntent(kind="goto",
                        head=HeadPose(pitch=(-3.0 if up else -1.0), z=(1.0 if up else -1.0)),
                        antennas=(0.75, -0.75) if up else ANTENNA_PERKED,
                        duration=1.2, interpolation="minjerk", reason=reason)


_SUSTAIN_YAW = (4.0, -3.0, 5.0, -4.0, 2.0, -5.0, 3.0, -2.0)
_SUSTAIN_PITCH = (3.0, 1.0, 4.0, 2.0, 3.5, 1.5, 2.5, 4.0)
_SUSTAIN_ROLL = (1.5, -1.0, 1.0, -1.5, 0.5, -1.0, 1.5, -0.5)


def sustain_beat(seq: int, reason: str = "speaking (sustain)", *, amp: float = 1.0,
                 tempo: float = 1.0, antenna_scale: float = 1.0) -> MotionIntent:
    """One voice-paced micro-beat for a sentence that outlives its plan.

    The player issues these while the voice is still audible after the plan's
    estimate ran out (the event leads the audio by seconds, and a long
    sentence outruns a 4 s estimate). Small on purpose -- a conversational hover of a
    few degrees with the antennas following the voice -- so it continues any shape
    without fighting it, and it is marked ``speech`` + ``pace="voice"`` so the beats
    land on the words exactly like the talking flow's.
    """

    j = seq % 8
    ant = (0.6, -0.25) if seq % 2 == 0 else (-0.25, 0.6)
    return MotionIntent(kind="goto",
                        head=HeadPose(yaw=_SUSTAIN_YAW[j] * amp, pitch=_SUSTAIN_PITCH[j] * amp,
                                      roll=_SUSTAIN_ROLL[j] * amp,
                                      z=(amp if j % 3 == 0 else 0.0)),
                        antennas=(ant[0] * antenna_scale, ant[1] * antenna_scale),
                        duration=0.34 * tempo, interpolation="ease_in_out",
                        speech=True, pace="voice", antenna_voice=True, reason=reason)


def perk_antennas(reason: str = "perk up (engaging)") -> MotionIntent:
    return MotionIntent(kind="antennas", antennas=ANTENNA_PERKED, duration=0.4, reason=reason)


def droop_antennas(reason: str = "soften (empathetic)") -> MotionIntent:
    return MotionIntent(kind="antennas", antennas=ANTENNA_DROOPED, duration=0.6, reason=reason)


def antenna_wiggle(reason: str = "attention-seeking wiggle") -> list[MotionIntent]:
    return [
        MotionIntent(kind="antennas", antennas=(0.8, -0.2), duration=0.25, reason=reason),
        MotionIntent(kind="antennas", antennas=(-0.2, 0.8), duration=0.25, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_PERKED, duration=0.25, reason=reason),
    ]


def exit_ack(reason: str = "head push heard") -> list[MotionIntent]:
    """Two big antenna flicks: "I felt that". Antennas only, so the head stays free."""

    return [
        MotionIntent(kind="antennas", antennas=ANTENNA_ALERT_WIDE, duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_DROOPED, duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_ALERT_WIDE, duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_DROOPED, duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_REST, duration=0.3, reason=reason),
    ]


def backchannel(sound: str, reason: str = "verbal backchannel") -> MotionIntent:
    """Play a short backchannel sound (e.g. 'mm-hm'). No-op if media is disabled."""
    return MotionIntent(kind="play_sound", sound=sound, reason=reason)


def talking_gesture(beats: int = 3, reason: str = "speaking") -> list[MotionIntent]:
    """Small head bobs + antenna flicks that animate the robot 'speaking' a line.

    Visible proof in the simulator that the robot is the one talking; on the real
    robot this runs alongside the AI voice playing on the speaker. This is
    the LEGACY talking shape, kept byte-identical for the kill-switch paths
    (EMORI_CUE_MOTION=0 / EMORI_CUE_TALK=0 / empty text); live sentences without a
    cue family get :func:`talking_flow` instead.
    """
    out: list[MotionIntent] = []
    for i in range(max(1, beats)):
        down = 10.0 if i % 2 == 0 else 4.0
        out.append(MotionIntent(kind="goto", head=HeadPose(pitch=down, yaw=(5.0 if i % 2 else -5.0)),
                                antennas=(0.45, -0.15) if i % 2 else (-0.15, 0.45),
                                duration=0.26, interpolation="ease_in_out", reason=reason))
    out.append(MotionIntent(kind="goto", head=HeadPose(pitch=2.0), antennas=ANTENNA_PERKED,
                            duration=0.3, reason=reason + " (settle)"))
    return out


# talking_flow's two authored patterns (base angles at amp 1.0, indexed per beat and
# wrapped modulo 8). Pattern A leads with gaze drift (yaw-dominant, conversational
# scanning); pattern B leads with nodding (pitch-dominant, affirming). ``sign``
# mirrors either pattern left/right, so one table yields four distinct looks before
# amplitude even varies.
#
# Row 2 is LIFT-led: rows 0 and 1 are both entirely positive in
# pitch, i.e. the head dips on every beat of every sentence they perform, which
# live reads as a robot talking to the desk. Row 2 alternates the sign, so the
# gaze rises on half its beats and a run of flow sentences no longer trends one
# way. Rows 0 and 1 are untouched (pinned by the mapping tests).
_TALK_YAW = ((10.0, -7.0, 4.0, -9.0, 7.0, -5.0, 9.0, -6.0),
             (4.0, -4.0, 6.0, -3.0, 5.0, -6.0, 3.0, -5.0),
             (8.0, -5.0, 3.0, -7.0, 6.0, -4.0, 7.0, -3.0))
_TALK_PITCH = ((6.0, 2.0, 8.0, 3.0, 5.0, 7.0, 2.0, 6.0),
               (9.0, 1.0, 7.0, 2.0, 9.0, 3.0, 6.0, 2.0),
               (-5.0, 3.0, -7.0, 2.0, -4.0, 5.0, -6.0, 1.0))
_TALK_ROLL = ((4.0, -2.0, 3.0, -4.0, 2.0, -3.0, 4.0, -2.0),
              (2.0, -3.0, 2.0, -2.0, 3.0, -2.0, 2.0, -3.0),
              (3.0, -4.0, 4.0, -2.0, 3.0, -3.0, 2.0, -4.0))
_TALK_Z = ((2.0, -2.0, 0.0, 3.0, -1.0, 2.0, 0.0, -2.0),
           (-2.0, 1.0, -3.0, 2.0, -1.0, 3.0, -2.0, 1.0),
           (3.0, -1.0, 4.0, 0.0, 2.0, -2.0, 3.0, 1.0))
_TALK_BODY = ((0.08, -0.06, 0.07, -0.08, 0.06, -0.07, 0.08, -0.06),
              (0.06, -0.08, 0.06, -0.07, 0.08, -0.06, 0.07, -0.08),
              (0.07, -0.07, 0.08, -0.06, 0.07, -0.08, 0.06, -0.07))


def talking_flow(*, beats: int, beat_s: float = 0.34, amp: float = 1.0, sign: float = 1.0,
                 variant: int = 0, phase: int = 0, z_limit_mm: float | None = None,
                 body_yaw_enabled: bool | None = None,
                 reason: str = "speaking (AI turn)") -> list[MotionIntent]:
    """Full-DOF conversational gesticulation for a sentence with no cue family.

    Every joint the platform has participates: head yaw (gaze drift), pitch
    (nodding undulation), roll (accents), z (a breathing lean), both antennas, and
    a slight body sway when the robot has body yaw enabled. ``sign`` mirrors the
    pattern, ``variant`` picks between the gaze-led, nod-led and
    lift-led tables, and ``phase`` rotates the starting beat (3 x 2 x 8 = 48 openings)
    so consecutive sentences read as conversation rather than a loop -- in a live
    run 4 of 10 sentences hashed into the same sign+variant class, and without the
    phase term those performed identical shapes.
    """
    v = variant % 3
    out: list[MotionIntent] = []
    for i in range(max(1, beats)):
        j = (i + phase) % 8
        # Every third beat is an ACCENT: a snappier, wider sweep with the
        # antennas thrown open -- the gesticulation that makes a point, not
        # just a metronome. Cartoon easing gives the accent its snap.
        accent = (i + phase) % 3 == 2
        boost = 1.5 if accent else 1.0
        ant_hi, ant_lo = (0.85, -0.3) if accent else (0.55, -0.2)
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=_TALK_PITCH[v][j] * amp,
                          yaw=sign * _TALK_YAW[v][j] * amp * boost,
                          roll=sign * _TALK_ROLL[v][j] * amp * boost,
                          z=clamp_cue_z(_TALK_Z[v][j] * amp + (2.0 * amp if accent else 0.0),
                                        limit=z_limit_mm)),
            antennas=(ant_hi * amp, ant_lo * amp) if i % 2 == 0 else (ant_lo * amp, ant_hi * amp),
            body_yaw=cue_body_yaw(sign * _TALK_BODY[v][j] * amp * boost, enabled=body_yaw_enabled),
            duration=beat_s * (0.85 if accent else 1.0),
            interpolation="cartoon" if accent else "ease_in_out", reason=reason))
    return out


def greeting_gesture(reason: str = "greeting") -> list[MotionIntent]:
    """Welcome sequence at session start: perk up, look up at the person, friendly
    antenna wave, settle into the attentive pose. Screenless, so the greeting must
    be unmistakably a greeting."""
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=-8.0), antennas=ANTENNA_PERKED,
                     duration=0.5, interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="antennas", antennas=(0.9, -0.3), duration=0.25, reason=reason),
        MotionIntent(kind="antennas", antennas=(-0.3, 0.9), duration=0.25, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_PERKED, duration=0.25, reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=4.0), duration=0.5, reason=reason + " (settle)"),
    ]


def listening_gesture(reason: str = "listening (your turn)") -> list[MotionIntent]:
    """"Your turn" cue: a small forward lean + perked antennas so a screenless user
    knows the mic is live and it's their turn to speak. Deliberately distinct from
    greeting_gesture (reserved for session start) and quieter than it — no welcome
    wave, just an attentive settle the user reads as "go ahead"."""
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=-4.0), antennas=ANTENNA_PERKED,
                     duration=0.4, interpolation="ease_in_out", reason=reason),
    ]


def reprompt_gesture(reason: str = "re-prompt (take your time, I'm listening)") -> list[MotionIntent]:
    """The conversation watchdog's re-prompt: "still listening -- go ahead".

    Lean in with a clear head tilt, antennas thrown wide and flicked twice, a short
    hold, then back to the listening pose. Bigger and longer (~2.0 s, about the spoken
    line it accompanies) than
    ``listening_gesture`` so it is noticed by someone who is thinking, and AUTHORED
    goto/antennas intents only: on the v2 node every goto head pose is composed onto
    the learner's face aim, a recorded move (play_recorded) is not -- so this is
    performed toward the learner, wherever they stand. yaw stays 0 (= "at the face").
    """
    tilt = HeadPose(pitch=-7.0, roll=10.0)
    return [
        MotionIntent(kind="goto", head=tilt, antennas=ANTENNA_ALERT_WIDE,
                     duration=0.4, interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="antennas", antennas=(0.4, -1.4), duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=(1.4, -0.4), duration=0.2, reason=reason),
        MotionIntent(kind="antennas", antennas=ANTENNA_ALERT_WIDE, duration=0.2, reason=reason),
        MotionIntent(kind="goto", head=tilt, duration=0.6, reason=reason + " (hold)"),
        MotionIntent(kind="goto", head=HeadPose(pitch=-4.0), antennas=ANTENNA_PERKED,
                     duration=0.4, interpolation="ease_in_out", reason=reason + " (settle)"),
    ]


def thinking_gesture(reason: str = "heard you, thinking") -> list[MotionIntent]:
    """"I got that" the instant a turn is submitted, then a held considering pose.

    This is the screenless robot's ONLY way to acknowledge a learner. The speaker is
    not available: during a live session the TTS interlock reserves it for the
    session's own voice and refuses every other synth request
    (``speech_api.mount``), so a spoken backchannel is forbidden by design. Motion is
    not.

    It matters more than it looks. Measured on the robot, a learner waits ~3.5 s between
    finishing their sentence and hearing the reply, and on a screenless robot that is
    3.5 s of *nothing* -- indistinguishable from a robot that has crashed. The same
    wait on the Pokamind web app is covered by response text streaming in within
    ~50 ms, which is a large part of why the platform feels immediate and the robot
    does not, at almost identical latency.

    Shape: a quick shallow nod (the universal "heard you", deliberately the first and
    shortest intent so it lands within a beat of the learner stopping), then up into
    an off-axis considering pose that simply HOLDS until the reply arrives and
    ``talking_gesture`` takes over. Distinct from ``listening_gesture`` -- that one is
    symmetric and forward ("your turn"); this one is asymmetric and raised ("my turn,
    working on it") -- and never mistaken for speech, which is a beat sequence.
    """
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=10.0), duration=0.22,
                     interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=-6.0, yaw=8.0, roll=5.0),
                     antennas=ANTENNA_CURIOUS, duration=0.5, interpolation="minjerk",
                     reason=reason + " (considering)"),
    ]


def farewell_gesture(reason: str = "farewell") -> list[MotionIntent]:
    """Goodbye sequence at session end: a slow appreciative bow, then rest."""
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=14.0), antennas=(0.3, -0.3),
                     duration=0.8, interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=0.0), antennas=ANTENNA_REST,
                     duration=0.8, interpolation="ease_in_out", reason=reason + " (rest)"),
    ]


def sink_toward_shell(*, pitch_deg: float = 12.0, roll_deg: float = 3.0, yaw_deg: float = 0.0,
                      z_mm: float = -8.0,
                      antennas: tuple[float, float] = (-0.55, 0.55), sink_s: float = 0.6,
                      hold_s: float = 0.35, tremor_roll_deg: float = 0.0,
                      z_limit_mm: float | None = None, body_yaw_rad: float | None = None,
                      body_yaw_enabled: bool | None = None,
                      reason: str = "cue tension") -> list[MotionIntent]:
    """Head sinks slowly toward the shell and STAYS there - the 'nervous' read.

    Tension is stillness, not motion: one long move in, one hold, and never more
    than that. ``tremor_roll_deg`` (the lexicon's 'nervous' group only) splits the
    hold into a two-step micro-tremor rather than adding a third pose.
    ``body_yaw_rad`` turns the body slightly AWAY during the sink and holds it
    there - a guarded shoulder, gated like every body-yaw term.
    """
    z = clamp_cue_z(z_mm, limit=z_limit_mm)
    body = cue_body_yaw(body_yaw_rad, enabled=body_yaw_enabled)
    head = HeadPose(pitch=pitch_deg, roll=roll_deg, yaw=yaw_deg, z=z)
    sink = MotionIntent(kind="goto", head=head, antennas=antennas, body_yaw=body,
                        duration=sink_s, interpolation="ease_in_out", reason=reason)
    if tremor_roll_deg:
        half = max(0.0, hold_s) / 2.0
        # The antennas shiver WITH the head micro-tremor (one flicks up as the
        # other dips) - the 'nervous' read needs the whole body trembling, not a
        # steady droop under a shaking head.
        flutter = (antennas[0] + 0.12, antennas[1] - 0.12)
        return [
            sink,
            MotionIntent(kind="goto", head=HeadPose(pitch=pitch_deg, roll=roll_deg + tremor_roll_deg, yaw=yaw_deg, z=z),
                         antennas=flutter, body_yaw=body, duration=half, interpolation="ease_in_out",
                         reason=reason + " (tremor)"),
            MotionIntent(kind="goto", head=HeadPose(pitch=pitch_deg, roll=roll_deg - tremor_roll_deg, yaw=yaw_deg, z=z),
                         antennas=antennas, body_yaw=body, duration=half, interpolation="ease_in_out",
                         reason=reason + " (tremor)"),
        ]
    return [
        sink,
        MotionIntent(kind="goto", head=head, antennas=antennas, body_yaw=body, duration=hold_s,
                     interpolation="ease_in_out", reason=reason + " (hold)"),
    ]


def levity_bounce(*, beats: int = 3, beat_s: float = 0.22, pitch_up: float = -8.0,
                  pitch_down: float = 2.0, yaw_deg: float = 7.0, roll_deg: float = 4.0,
                  z_mm: float = 3.0, body_yaw_rad: float = 0.10,
                  antennas_up: tuple[float, float] = (0.85, -0.25),
                  antennas_down: tuple[float, float] = (-0.25, 0.85),
                  z_limit_mm: float | None = None, body_yaw_enabled: bool | None = None,
                  reason: str = "cue levity") -> list[MotionIntent]:
    """Fast cartoon beat pairs - the amused / 'happy' read: livelier rotation + open antennas."""
    out: list[MotionIntent] = []
    for i in range(max(1, beats)):
        up = i % 2 == 0
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=pitch_up if up else pitch_down,
                          yaw=yaw_deg if up else -yaw_deg,
                          roll=roll_deg if up else -roll_deg,
                          z=clamp_cue_z(z_mm if up else 0.0, limit=z_limit_mm)),
            antennas=antennas_up if up else antennas_down,
            body_yaw=cue_body_yaw(body_yaw_rad if up else -body_yaw_rad, enabled=body_yaw_enabled),
            duration=beat_s, interpolation="cartoon", reason=reason))
    return out


def energize_bob(*, beats: int = 4, beat_s: float = 0.24, pitch_a: float = 9.0,
                 pitch_b: float = 2.0, yaw_deg: float = 9.0, body_yaw_rad: float = 0.12,
                 z_mm: float = 0.0, z_limit_mm: float | None = None,
                 antennas_a: tuple[float, float] = (0.6, -0.2),
                 antennas_b: tuple[float, float] = (-0.2, 0.6),
                 body_yaw_enabled: bool | None = None,
                 reason: str = "cue energy") -> list[MotionIntent]:
    """The densest family: ``talking_gesture`` with amplitude, a driving push.

    ``z_mm`` (new) lifts the head on the driving beats so the push is a whole-body
    rise, not a rotation alone; 0.0 is the original shape.
    """
    out: list[MotionIntent] = []
    for i in range(max(1, beats)):
        first = i % 2 == 0
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=pitch_a if first else pitch_b,
                          yaw=yaw_deg if first else -yaw_deg,
                          z=clamp_cue_z(z_mm if first else 0.0, limit=z_limit_mm)),
            antennas=antennas_a if first else antennas_b,
            body_yaw=cue_body_yaw(body_yaw_rad if first else -body_yaw_rad, enabled=body_yaw_enabled),
            duration=beat_s, interpolation="ease_in_out", reason=reason))
    return out


def lean_in_intimate(*, pitch_deg: float = -3.0, roll_deg: float = 2.0, z_mm: float = 5.0,
                     antennas: tuple[float, float] = (0.35, -0.35), lean_s: float = 0.6,
                     hold_s: float = 0.25, z_limit_mm: float | None = None,
                     reason: str = "cue intimacy") -> list[MotionIntent]:
    """A slow lean toward the person, then stillness - confiding.

    Minimal motion is the point: a robot that jiggles while confiding is not
    confiding.
    """
    head = HeadPose(pitch=pitch_deg, roll=roll_deg, z=clamp_cue_z(z_mm, limit=z_limit_mm))
    return [
        MotionIntent(kind="goto", head=head, antennas=antennas, duration=lean_s,
                     interpolation="minjerk", reason=reason),
        MotionIntent(kind="goto", head=head, antennas=antennas, duration=hold_s,
                     interpolation="minjerk", reason=reason + " (hold)"),
    ]


def soften_settle(*, nod_pitch: float = 7.0, return_pitch: float = -1.0, z_mm: float = -3.0,
                  roll_deg: float = 0.0,
                  antennas: tuple[float, float] = (-0.25, 0.25), nod_s: float = 0.35,
                  return_s: float = 0.35, z_limit_mm: float | None = None,
                  reason: str = "cue softening") -> list[MotionIntent]:
    """A small gentling nod - apologising, easing off.

    ``roll_deg`` (new) adds a gentle sympathetic head-cock into the nod; 0.0 is
    the original shape.
    """
    z = clamp_cue_z(z_mm, limit=z_limit_mm)
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=nod_pitch, roll=roll_deg, z=z),
                     antennas=antennas,
                     duration=nod_s, interpolation="minjerk", reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=return_pitch, z=z), antennas=antennas,
                     duration=return_s, interpolation="minjerk", reason=reason + " (return)"),
    ]


def release_exhale(*, sink_pitch: float = 4.0, open_pitch: float = -4.0, sink_z_mm: float = -16.0,
                   open_z_mm: float = -2.0, antennas_in: tuple[float, float] = ANTENNA_PERKED,
                   antennas_out: tuple[float, float] = ANTENNA_BLOOM, sink_s: float = 0.3,
                   out_s: float = 0.45, sway_roll_deg: float = 3.0,
                   z_limit_mm: float | None = None,
                   body_yaw_rad: float | None = None, body_yaw_enabled: bool | None = None,
                   reason: str = "cue release") -> list[MotionIntent]:
    """Relief, adapted from Pollen's own recorded ``relief1`` move (measured, not
    guessed): the exhale is VERTICAL -- the whole head drops toward -20 mm as the
    tension leaves the body, the pitch barely bows (+4), the antennas make ONE
    committed bloom to ~110 deg open and stay there, and a small roll sway at the
    bottom keeps the pose alive before the slow rise back. Four keyframes:
    drop, bottom-sway in, sway back, and the long open rise.

    ``body_yaw_rad`` turns the body slightly into the drop, and the rise swings it
    back to centre - the whole platform lets go, not just the head.
    """
    body_in = cue_body_yaw(body_yaw_rad, enabled=body_yaw_enabled)
    half_bloom = (antennas_out[0] * 0.5, antennas_out[1] * 0.5)
    low = clamp_cue_z(sink_z_mm, limit=z_limit_mm)
    return [
        MotionIntent(kind="goto", head=HeadPose(pitch=sink_pitch, z=low),
                     antennas=antennas_in, body_yaw=body_in, duration=sink_s,
                     interpolation="ease_in_out", reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=sink_pitch, roll=sway_roll_deg,
                                                z=clamp_cue_z(low * 0.9, limit=z_limit_mm)),
                     antennas=half_bloom, body_yaw=body_in, duration=out_s * 0.25,
                     interpolation="minjerk", reason=reason + " (sway)"),
        MotionIntent(kind="goto", head=HeadPose(pitch=sink_pitch * 0.5, roll=-sway_roll_deg * 0.6,
                                                z=clamp_cue_z(low * 0.8, limit=z_limit_mm)),
                     antennas=antennas_out, body_yaw=body_in, duration=out_s * 0.2,
                     interpolation="minjerk", reason=reason + " (sway)"),
        MotionIntent(kind="goto", head=HeadPose(pitch=open_pitch, z=clamp_cue_z(open_z_mm, limit=z_limit_mm)),
                     antennas=antennas_out, body_yaw=(0.0 if body_in is not None else None),
                     duration=out_s * 0.55, interpolation="ease_in_out",
                     reason=reason + " (exhale)"),
    ]


def settle_to_attend(duration: float = 0.3, antennas: tuple[float, float] = ANTENNA_PERKED,
                     body_yaw: float | None = None,
                     reason: str = "cue settle") -> MotionIntent:
    """Return to the attentive pose. EVERY cue plan ends with one of these.

    Same pose ``talking_gesture`` settles into, so a cue plan and the legacy
    speaking gesture leave the head in the identical place - which is what makes a
    plan preempted mid-flight by the next sentence recover cleanly. When a robot has
    body yaw enabled the cue planners pass ``body_yaw=0.0`` here, so a plan whose
    swing was cut mid-flight still brings the BODY back to centre, not just the
    head; ``None`` (the default) leaves the body uncommanded exactly as before.
    """
    return MotionIntent(kind="goto", head=HeadPose(pitch=2.0), antennas=antennas,
                        body_yaw=body_yaw, duration=duration, reason=reason)


def superseding(intents: list[MotionIntent]) -> list[MotionIntent]:
    """The same intents, with the first move set to replace what the robot is doing."""

    out = list(intents)
    for index, intent in enumerate(out):
        if intent.kind in ("goto", "look_at", "antennas", "play_recorded"):
            out[index] = intent.model_copy(update={"supersede": True})
            break
    return out


def calm_settle(duration: float = 1.0, reason: str = "de-escalation: slow & grounded") -> list[MotionIntent]:
    """Slow, low-amplitude settle used when the learner is agitated (de-escalation)."""
    return [
        MotionIntent(kind="wobble", wobble=False, reason=reason),
        MotionIntent(kind="goto", head=HeadPose(pitch=2.0), antennas=(-0.2, 0.2),
                     duration=duration, interpolation="ease_in_out", reason=reason),
    ]


# --- the talking GRAMMAR -------------------------------------------------
#
# ``talking_flow`` above is ONE shape: eight beats of the same period, the same
# alternating antenna flick, and (because every entry of ``_TALK_PITCH`` is
# positive) the head dipping DOWN on every single beat. On its own it made a whole
# conversation read as a few gestures on a loop: antenna movements and the face
# going down.
#
# A speaking body does not have one gesture; it has a grammar. These are the
# structural shapes a sentence can be performed with. Each differs in RHYTHM (how
# many beats and how they are spaced), in which DOF LEADS (yaw, pitch, roll, z or
# the body), and in what the ANTENNAS do (alternating flick, unison pulse,
# progressive open, held asymmetric trail) -- the three things an observer
# actually reads as "a different gesture". ``cues.py`` picks one per sentence from
# the sentence's own shape (question / exclamation / enumeration / length) and
# rotates away from the shapes it just performed.
TALK_SHAPES: tuple[str, ...] = (
    "flow", "nod_run", "gaze_arc", "lean_press", "head_cock",
    "list_three", "rise", "punch", "sway",
)


def talking_shape(name: str, *, beats: int, beat_s: float = 0.34, amp: float = 1.0,
                  sign: float = 1.0, variant: int = 0, phase: int = 0,
                  z_limit_mm: float | None = None, body_yaw_enabled: bool | None = None,
                  reason: str = "speaking (AI turn)") -> list[MotionIntent]:
    """One sentence performed in the named structural shape (see TALK_SHAPES).

    An unknown name falls back to ``talking_flow`` rather than raising: the shape
    name reaches here from a planner slug, and a typo must never silence a
    sentence's motion on a live robot.
    """

    builder = _TALK_BUILDERS.get(name)
    if builder is None:
        return talking_flow(beats=beats, beat_s=beat_s, amp=amp, sign=sign,
                            variant=variant, phase=phase, z_limit_mm=z_limit_mm,
                            body_yaw_enabled=body_yaw_enabled, reason=reason)
    return builder(beats=beats, beat_s=beat_s, amp=amp, sign=sign, variant=variant,
                   phase=phase, z_limit_mm=z_limit_mm, body_yaw_enabled=body_yaw_enabled,
                   reason=reason)


def _talk_flow(**kw) -> list[MotionIntent]:
    return talking_flow(**kw)


def _talk_nod_run(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                  phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                  reason: str) -> list[MotionIntent]:
    """Affirming nods: pitch leads, yaw stays home, the antennas pulse IN UNISON.

    The nod is the one gesture everyone reads instantly, and unison antennas (both
    sweeping wide together, then relaxing together) are a visibly different
    instrument from the flow's left/right flick. Each nod is smaller than the last,
    which is what makes a run of them read as agreement rather than a tic.
    """

    out: list[MotionIntent] = []
    pairs = max(1, beats // 2)
    for i in range(pairs):
        depth = (14.0 if i == 0 else 10.0) * amp * max(0.5, 1.0 - 0.12 * i)
        wide = min(1.3, (0.95 - 0.15 * i) * amp)
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=depth, roll=sign * 2.0 * amp * (1.0 if i % 2 == 0 else -1.0),
                          z=clamp_cue_z(-1.5 * amp, limit=z_limit_mm)),
            antennas=(wide, -wide),
            body_yaw=cue_body_yaw(sign * 0.04 * amp, enabled=body_yaw_enabled),
            duration=beat_s * 0.8, interpolation="ease_in_out", reason=reason))
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=-3.0 * amp, z=clamp_cue_z(1.0 * amp, limit=z_limit_mm)),
            antennas=(0.3 * amp, -0.3 * amp),
            duration=beat_s * 0.7, interpolation="ease_in_out",
            reason=reason + " (up)"))
    return out


def _talk_gaze_arc(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                   phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                   reason: str) -> list[MotionIntent]:
    """Thinking aloud: the gaze swings away in a slow arc and comes back on the last
    beats. Yaw leads and the whole body follows it; the antennas TRAIL, held
    asymmetric, instead of beating -- so nothing about it reads like the flow."""

    n = max(3, beats)
    away = sign * (13.0 + 3.0 * (variant % 2)) * amp
    out: list[MotionIntent] = []
    for i in range(n):
        frac = (i + 1) / n
        arc = away * (frac / 0.66 if frac <= 0.66 else max(0.0, (1.0 - frac) / 0.34))
        home = 1.0 - (abs(arc) / abs(away) if away else 0.0)
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(yaw=arc, pitch=(3.0 - 4.0 * frac) * amp, roll=arc * 0.18,
                          z=clamp_cue_z(-2.0 * amp * home, limit=z_limit_mm)),
            antennas=(0.55 * amp, -0.1 * amp) if sign > 0 else (-0.1 * amp, 0.55 * amp),
            body_yaw=cue_body_yaw(arc * 0.010, enabled=body_yaw_enabled),
            duration=beat_s * 1.15, interpolation="minjerk", reason=reason))
    return out


def _talk_lean_press(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                     phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                     reason: str) -> list[MotionIntent]:
    """Making the point: the head comes FORWARD (z+, gaze slightly raised), presses
    on the accents, and eases back at the end. z leads -- the axis the flow barely
    uses -- so the sentence reads as insistence rather than punctuation."""

    n = max(3, beats)
    out = [MotionIntent(
        kind="goto",
        head=HeadPose(pitch=-4.0 * amp, yaw=sign * 3.0 * amp,
                      z=clamp_cue_z(6.0 * amp, limit=z_limit_mm)),
        antennas=(0.7 * amp, -0.7 * amp),
        body_yaw=cue_body_yaw(sign * 0.05 * amp, enabled=body_yaw_enabled),
        duration=beat_s * 1.1, interpolation="ease_in_out", reason=reason + " (in)")]
    for i in range(max(1, n - 2)):
        press = i % 2 == 0
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=(7.0 if press else -2.0) * amp,
                          yaw=sign * (0.0 if press else 5.0) * amp,
                          roll=sign * (3.0 if press else -2.0) * amp,
                          z=clamp_cue_z((7.0 if press else 5.0) * amp, limit=z_limit_mm)),
            antennas=(1.0 * amp, -0.35 * amp) if press else (0.45 * amp, -0.45 * amp),
            duration=beat_s * (0.75 if press else 0.9),
            interpolation="cartoon" if press else "ease_in_out",
            reason=reason + (" (press)" if press else "")))
    out.append(MotionIntent(
        kind="goto",
        head=HeadPose(pitch=1.0 * amp, z=clamp_cue_z(1.0 * amp, limit=z_limit_mm)),
        antennas=(0.6 * amp, -0.6 * amp),
        body_yaw=cue_body_yaw(0.0, enabled=body_yaw_enabled),
        duration=beat_s, interpolation="ease_in_out", reason=reason + " (back)"))
    return out


def _talk_head_cock(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                    phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                    reason: str) -> list[MotionIntent]:
    """Roll leads: the head cocks over and STAYS cocked, breathing a degree or two,
    one antenna up and one near rest. Almost no pitch -- the opposite read of the
    nod-heavy flow, and the clearest 'considering what I am saying' shape we have."""

    n = max(2, beats - 1)
    roll = sign * (11.0 + 3.0 * (variant % 2)) * amp
    out = [MotionIntent(
        kind="goto", head=HeadPose(roll=roll, pitch=-2.0 * amp, yaw=sign * 4.0 * amp),
        antennas=(0.85 * amp, 0.1 * amp) if sign > 0 else (0.1 * amp, 0.85 * amp),
        duration=beat_s * 1.2, interpolation="minjerk", reason=reason)]
    for i in range(max(1, n - 1)):
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(roll=roll * (0.85 if i % 2 == 0 else 1.0),
                          pitch=(2.0 if i % 2 == 0 else -1.5) * amp,
                          yaw=sign * 4.0 * amp,
                          z=clamp_cue_z((1.5 if i % 2 == 0 else -1.5) * amp, limit=z_limit_mm)),
            antennas=(0.7 * amp, 0.2 * amp) if sign > 0 else (0.2 * amp, 0.7 * amp),
            duration=beat_s * 0.95, interpolation="ease_in_out", reason=reason))
    return out


def _talk_list_three(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                     phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                     reason: str) -> list[MotionIntent]:
    """Enumerating: three accents that step DOWN in size and ACROSS in yaw -- the
    'first..., second..., and then...' hand. Its beats shrink, which no other shape
    here does, so a list never reads like the flow's even metronome."""

    out: list[MotionIntent] = []
    for i in range(3):
        scale = (1.0, 0.82, 0.66)[i]
        across = sign * (-9.0 + 9.0 * i) * amp
        wide = min(1.3, (0.5 + 0.25 * i) * amp)
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=9.0 * scale * amp, yaw=across, roll=-across * 0.2,
                          z=clamp_cue_z(-2.0 * scale * amp, limit=z_limit_mm)),
            antennas=(wide, -wide),
            body_yaw=cue_body_yaw(across * 0.008, enabled=body_yaw_enabled),
            duration=beat_s * 0.8, interpolation="cartoon", reason=reason))
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=-2.0 * amp, yaw=across * 0.5),
            antennas=(0.35 * amp, -0.35 * amp),
            duration=beat_s * 0.65, interpolation="ease_in_out", reason=reason + " (lift)"))
    return out


def _talk_rise(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
               phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
               reason: str) -> list[MotionIntent]:
    """A question: the head RISES beat by beat and the antennas open progressively,
    ending lifted and held -- the shape a raised eyebrow would have. The only shape
    whose pitch trend is upward for its whole length, which is exactly why a
    question performed with it never looks like a statement."""

    n = max(3, beats)
    out: list[MotionIntent] = []
    for i in range(n):
        frac = (i + 1) / n
        wide = min(1.4, (0.4 + 0.9 * frac) * amp)
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=(3.0 - 9.0 * frac) * amp,
                          yaw=sign * (4.0 - 6.0 * frac) * amp,
                          roll=sign * 3.0 * frac * amp,
                          z=clamp_cue_z(4.0 * frac * amp, limit=z_limit_mm)),
            antennas=(wide, -wide),
            body_yaw=cue_body_yaw(sign * 0.05 * frac * amp, enabled=body_yaw_enabled),
            duration=beat_s * (1.0 if i < n - 1 else 1.3),
            interpolation="ease_in_out" if i < n - 1 else "minjerk", reason=reason))
    return out


def _talk_punch(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
                phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
                reason: str) -> list[MotionIntent]:
    """One committed accent, then stillness -- for a short, emphatic line. The whole
    platform moves ONCE (pitch, yaw, roll, z and the body together) instead of
    beating; stillness after a strike is what makes the strike read."""

    out = [
        MotionIntent(
            kind="goto",
            head=HeadPose(pitch=15.0 * amp, yaw=sign * 8.0 * amp, roll=sign * 5.0 * amp,
                          z=clamp_cue_z(-4.0 * amp, limit=z_limit_mm)),
            antennas=(min(1.6, 1.25 * amp), -min(1.6, 1.25 * amp)),
            body_yaw=cue_body_yaw(sign * 0.12 * amp, enabled=body_yaw_enabled),
            duration=beat_s * 0.65, interpolation="cartoon", reason=reason + " (punch)"),
        MotionIntent(
            kind="goto",
            head=HeadPose(pitch=-3.0 * amp, yaw=sign * 2.0 * amp,
                          z=clamp_cue_z(2.0 * amp, limit=z_limit_mm)),
            antennas=(0.65 * amp, -0.65 * amp),
            body_yaw=cue_body_yaw(0.0, enabled=body_yaw_enabled),
            duration=beat_s * 1.2, interpolation="ease_in_out", reason=reason + " (hold)"),
    ]
    for i in range(max(0, beats - 3)):
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(pitch=(4.0 if i % 2 == 0 else -1.0) * amp,
                          yaw=sign * (-4.0 if i % 2 == 0 else 3.0) * amp),
            antennas=(0.5 * amp, -0.5 * amp) if i % 2 == 0 else (0.35 * amp, -0.35 * amp),
            duration=beat_s * 0.9, interpolation="ease_in_out", reason=reason))
    return out


def _talk_sway(*, beats: int, beat_s: float, amp: float, sign: float, variant: int,
               phase: int, z_limit_mm: float | None, body_yaw_enabled: bool | None,
               reason: str) -> list[MotionIntent]:
    """The quiet shape: a slow lateral sway with almost no pitch, the antennas
    drifting rather than beating. For short or low-key lines, where the flow's eight
    dipping beats are simply more motion than the words carry."""

    n = max(2, beats - 1)
    out: list[MotionIntent] = []
    for i in range(n):
        side = 1.0 if i % 2 == 0 else -1.0
        out.append(MotionIntent(
            kind="goto",
            head=HeadPose(yaw=sign * side * 5.0 * amp, roll=sign * side * 3.0 * amp,
                          pitch=(1.5 if i % 2 == 0 else -1.0) * amp,
                          z=clamp_cue_z(side * 1.5 * amp, limit=z_limit_mm)),
            antennas=(0.5 * amp, -0.2 * amp) if side > 0 else (0.2 * amp, -0.5 * amp),
            body_yaw=cue_body_yaw(sign * side * 0.05 * amp, enabled=body_yaw_enabled),
            duration=beat_s * 1.35, interpolation="minjerk", reason=reason))
    return out


_TALK_BUILDERS = {
    "flow": _talk_flow,
    "nod_run": _talk_nod_run,
    "gaze_arc": _talk_gaze_arc,
    "lean_press": _talk_lean_press,
    "head_cock": _talk_head_cock,
    "list_three": _talk_list_three,
    "rise": _talk_rise,
    "punch": _talk_punch,
    "sway": _talk_sway,
}
