# One relayed session envelope, typed and gated. On /emori/session/events, one message per
# envelope the hub relays, with no sampling.
#
# `seq` counts per process and has no gaps: a gap is envelopes that were lost, and is how a
# subscriber sees the loss. The raw envelope never travels unless the hub runs with
# EMORI_ROS_RAW_ENVELOPES=1.
std_msgs/Header header
uint32 seq                  # per process, gapless: a gap is a visible loss
string type                 # pokamind_* verbatim, or hub_session_opened|hub_session_finalized
string session_id           # pseudonym
string person_id            # pseudonym
string lang
builtin_interfaces/Time source_time
string text                 # EMORI_ROS_SPEECH_TEXT gate; "" by default
string cue_category
string cue_family
string cue_family_source
string cue_slug
string cue_recorded
float32 cue_budget_s
float32 est_duration
string source
bool has_results
string status
string payload_json         # whole stamped envelope only with EMORI_ROS_RAW_ENVELOPES=1
