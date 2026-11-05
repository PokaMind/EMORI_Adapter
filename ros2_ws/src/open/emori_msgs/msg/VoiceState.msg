# The robot's own voice as the speaker output shows it. On /emori/speech/voice at 10 Hz, and
# only while somebody subscribes.
std_msgs/Header header
bool enabled
bool available
bool active
float32 level
float32 rms
float32 noise_rms
uint32 onsets
uint32 frames
float32 silent_for_s        # -1.0 = None
