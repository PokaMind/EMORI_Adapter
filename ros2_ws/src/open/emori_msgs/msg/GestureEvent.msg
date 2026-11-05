# One gesture the robot recognised. On /emori/gesture/events.
std_msgs/Header header
uint8 KIND_HEAD_TURN=0
uint8 KIND_END_NUDGE=1
uint8 KIND_BEGIN=2
uint8 KIND_LANG_TOGGLE=3
uint8 kind
string direction            # left|right|""
