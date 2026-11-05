# A bounded expression request, published by the engagement node on
# /skill/set_expression and consumed by skill_facade.
#
# A TOPIC rather than an action on purpose: setting an
# expression is fire-and-forget state, not a goal with a result worth waiting on.
#
# The engagement node never emits raw motor code - `label` is the hri_msgs/Expression
# vocabulary and the facade is the only component that turns it into motion, by
# POSTing the hostapp's /api/skill (never the daemon), so the single-writer rule holds.
std_msgs/Header header

string person_id
string label         # neutral|happy|sad|angry|surprised|scared|disgusted
float32 confidence   # 0..1
float32 intensity    # 0..1, the planner's k
string reason        # provenance for the trace, e.g. "cue:tension" / "engagement:recovery"
