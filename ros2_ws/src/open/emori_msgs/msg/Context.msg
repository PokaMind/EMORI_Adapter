# EMORI per-cycle context for one learner (REP-155 extension, published under
# /humans/persons/<id>/context). Carries the full normalised signal so a single
# message gives downstream behaviour/analytics modules everything they need.
std_msgs/Header header

string person_id
string session_id
uint32 seq

# Engagement (mirrors hri_msgs/EngagementLevel constants in `engagement_level`).
float32 engagement_score          # 0..1
uint8   engagement_level          # 0 UNKNOWN 1 DISENGAGED 2 ENGAGING 3 ENGAGED 4 DISENGAGING

# Affect (circumplex) + discrete expression label (hri_msgs/Expression vocabulary).
float32 valence                   # -1..1
float32 arousal                   # -1..1
string  expression                # neutral|happy|sad|angry|surprised|scared|disgusted
float32 expression_confidence     # 0..1

# Temporal / semantic / relational context.
float32 engagement_trend          # -1..1 (d engagement / dt, smoothed)
float32 escalation                # 0..1 (agitation trajectory -> de-escalation + trainer alert)
float32 rapport                   # 0..1
float32 attention                 # 0..1
string  dominant_style            # one of the 31 communication styles
string  topic
string  note                      # "escalation" | "attention-dropping" | "re-engaging" | ""

# --- v2: person report (raw observations) ---
# Everything above is INTERPRETATION; everything below is OBSERVATION, in the session
# analysis' own vocabulary. Appended, never reordered: every field above is already bound by a
# compiled subscriber, and tests/test_ros_contracts.py pins that order as a golden list.
#
# -1.0 means NOT REPORTED, and it is the point of this block. On the live wire only the
# styles and body_language channels arrive; the facial/tone/verbal ones never do, and a
# lost face nulls the body too. A 0.0 here would say "we measured this person at zero",
# which is a different session from the one that actually happened.
string  posture                   # canonical vocabulary (upright|lean_forward|...), "" = absent
string  head_position             # (upright|nodding|tilted|shaking|...), "" = absent
string  gesture_type              # (open_palm|pointing|...), "" = absent
string  overall_energy            # (low|moderate|high), "" = absent
float32 body_engagement           # 0..1, -1.0 = not reported
float32 body_confidence           # 0..1, -1.0 = not reported
float32 face_positivity           # 0..1, -1.0 = not reported (LIVE: always -1.0)
float32 voice_warmth              # 0..1, -1.0 = not reported (LIVE: always -1.0)
float32 pitch_variety             # 0..1, -1.0 = not reported (LIVE: always -1.0)
string[] style_names              # top-5, descending by value, ties broken by name
float32[] style_values            # parallel to style_names, 0..1
uint8   report_flags              # bit0 BODY, bit1 FACE, bit2 TONE, bit3 VERBAL, bit4 STYLES
string  lang                      # kiosk language of the session ("" until known)
float32 age_s                     # freshness: seconds since the source envelope stamp,
                                  # -1.0 when the envelope carried no usable timestamp

# --- constants: the contract any consumer can read from the interface alone -------
# The sentinel and the channel bits live here so a consumer need not install the adapter
# core - or guess - to know that -1.0 is absence rather than a score. rosidl compiles
# these into the generated class in every language, so
# `ros2 interface show emori_msgs/msg/Context` is the whole contract,
# and `hri_context_analyzer` reads them off `Context` instead of importing the core.
float32 NOT_REPORTED=-1.0
uint8 REPORT_BODY=1
uint8 REPORT_FACE=2
uint8 REPORT_TONE=4
uint8 REPORT_VERBAL=8
uint8 REPORT_STYLES=16
