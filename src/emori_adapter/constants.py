"""Canonical vocabulary shared across the adapter.

These mirror what Pokamind actually produces today (verified against the live
wire format):

* 31 canonical communication styles (``COMM_STYLES``).
* 5x5 per-modality metric dimensions (facial / tone / body / verbal).
* the 11 body-language fields.

Where Pokamind's exact metric sub-keys are not pinned down in the docs, the keys
below are best-effort; the translation layer always reads them defensively with
``.get(key, default)`` so a missing/renamed key degrades gracefully rather than
crashing. See docs/ros4hri-mapping.md for the authoritative mapping.
"""

from __future__ import annotations

# --- 31 canonical communication styles ---
COMM_STYLES: tuple[str, ...] = (
    "direct",
    "withdrawing",
    "pausing",
    "energetic",
    "quiet",
    "abrupt",
    "steady",
    "rapid",
    "mirroring",
    "firm",
    "reserved",
    "complimentary",
    "acknowledging",
    "planning",
    "paced",
    "investigative",
    "casual-toned",
    "muted",
    "brief",
    "lively",
    "neutral",
    "measured",
    "still",
    "ambiguous",
    "exploratory",
    "evaluative",
    "softening",
    "sudden",
    "analytical",
    "smooth",
    "corrective",
)

# Body-language emits a few off-list styles that Pokamind remaps before scoring.
STYLE_REMAP: dict[str, str] = {
    "composed": "steady",
    "decisive": "direct",
    "encouraging": "complimentary",
    "assertive": "direct",  # legacy alias
}

# --- Per-modality metric dimensions (each 0..100 in Pokamind) ---
FACIAL_METRIC_KEYS: tuple[str, ...] = (
    "eye_contact",
    "engagement",
    "smile_authenticity",
    "expressiveness",
    "positivity",
)
TONE_METRIC_KEYS: tuple[str, ...] = (
    "warmth",
    "steadiness",
    "fluency",
    "pitch_variety",
    "projection",
)
BODY_METRIC_KEYS: tuple[str, ...] = (
    "openness",
    "confidence",
    "engagement",
    "energy",
    "stillness",
)
VERBAL_METRIC_KEYS: tuple[str, ...] = (
    "clarity",
    "warmth",
    "curiosity",
    "reasoning",
    "expressiveness",
)

# --- Body-language fields the LIVE contract relays, with the value vocabularies
# verified against the live wire format. Values may be null on analysis failure (the
# envelope sanitizer drops them before they get here). The translation layer currently
# uses posture / head_position / overall_energy; the remaining categorical fields are
# forwarded by Pokamind at zero cost and are available for richer future behaviours.
BODY_LANGUAGE_FIELDS: dict[str, tuple[str, ...]] = {
    "posture": ("leaning_forward", "leaning_back", "slouched", "stiff",
                "upright", "relaxed", "not_visible"),
    "head_position": ("nodding", "shaking", "tilted_left", "tilted_right",
                      "down", "straight", "not_visible"),
    "gesture_type": ("open_palm", "pointing", "relaxed", "not_visible"),
    "overall_energy": ("low", "medium", "high"),
    # forwarded but not yet consumed:
    "shoulder_state": ("relaxed", "not_visible"),
    "body_orientation": ("not_visible",),
    "arm_position": ("not_visible",),
    "hand_activity": ("hands_on_chin", "hands_on_cheek", "hands_on_face", "not_visible"),
    "finger_state": ("not_visible",),
    # engagement_score and confidence_score are 0..100 numerics, handled separately
}

# Live Pokamind body values -> the adapter's canonical vocabulary (what the
# engagement nudge / energy mapping understand). Values not in this map and not
# already canonical carry no behavioural meaning (stiff, relaxed, not_visible...).
BODY_VALUE_NORMALIZE: dict[str, str] = {
    "leaning_forward": "lean_forward",
    "leaning_back": "lean_back",
    "slouched": "slouch",
    "straight": "upright",
    "tilted_left": "tilted",
    "tilted_right": "tilted",
    "medium": "moderate",
}


def normalize_body(body: dict) -> dict:
    """Fold live Pokamind body values onto the canonical vocabulary (str values only)."""
    out = {}
    for k, v in (body or {}).items():
        if isinstance(v, str):
            lv = v.lower()
            out[k] = BODY_VALUE_NORMALIZE.get(lv, lv)
        else:
            out[k] = v
    return out

# Communication styles that signal the learner is engaged / leaning in.
ENGAGEMENT_POSITIVE_STYLES: tuple[str, ...] = (
    "mirroring",
    "acknowledging",
    "investigative",
    "exploratory",
    "lively",
    "energetic",
    "complimentary",
    "still",  # sustained focus
)
# Styles that signal the learner is pulling away / shutting down.
ENGAGEMENT_NEGATIVE_STYLES: tuple[str, ...] = (
    "withdrawing",
    "muted",
    "quiet",
    "abrupt",
    "reserved",
    "ambiguous",
)

# Styles contributing to positive vs negative valence (affect).
VALENCE_POSITIVE_STYLES: tuple[str, ...] = (
    "complimentary",
    "lively",
    "energetic",
    "smooth",
    "casual-toned",
    "mirroring",
    "acknowledging",
)
VALENCE_NEGATIVE_STYLES: tuple[str, ...] = (
    "withdrawing",
    "abrupt",
    "muted",
    "corrective",
    "sudden",
)

# Styles contributing to high vs low arousal (activation).
AROUSAL_HIGH_STYLES: tuple[str, ...] = (
    "energetic",
    "rapid",
    "lively",
    "abrupt",
    "sudden",
)
AROUSAL_LOW_STYLES: tuple[str, ...] = (
    "quiet",
    "still",
    "paced",
    "muted",
    "reserved",
    "softening",
)
