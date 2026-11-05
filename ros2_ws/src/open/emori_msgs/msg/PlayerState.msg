# The motion player. Latched on /emori/motion/player; published on change and polled at 5 Hz.
std_msgs/Header header
bool busy
uint8 queue_depth
bool legacy_motion_enabled
uint32 generation
string motion_owner
