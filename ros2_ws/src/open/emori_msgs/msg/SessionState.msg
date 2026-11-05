# The kiosk session as the hub sees it. Latched on /emori/session/state; published on every
# change and every 2 s.
#
# `session_id` and `person_id` are pseudonyms unless the hub runs with
# EMORI_ROS_DEIDENTIFY=0, the same gate Context and SpeechCue pass through.
std_msgs/Header header
uint8 PHASE_NO_SESSION=0
uint8 PHASE_BOOTING=1
uint8 PHASE_FRAME_ALIVE=2
uint8 PHASE_EXCHANGED=3
uint8 PHASE_END_REQUESTED=4
uint8 PHASE_FINALIZED=5
uint8 phase
uint32 seq
string session_id           # pseudonym unless EMORI_ROS_DEIDENTIFY=0
string person_id            # pseudonym unless EMORI_ROS_DEIDENTIFY=0
bool anonymous
string topic
string module_id
string lang
uint32 cycles
uint32 utterances
bool exchanged
string watchdog_state       # IDLE|ARMED|OPEN|SUBMITTED|TIMED_OUT
float32 idle_for_s
string last_status          # completed|abandoned|""
string last_end_reason
float32 last_avg_engagement # -1.0 not reported
string last_dominant_style
