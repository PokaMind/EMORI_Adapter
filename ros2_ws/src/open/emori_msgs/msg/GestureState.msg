# What the gesture sensor is doing. Latched on /emori/gesture/state; published on change and
# polled at 5 Hz.
std_msgs/Header header
string mode                 # push_turn|back_drive|off
bool head_reserved
bool residual_live
bool head_held
bool robot_moving
string moving_reason
bool selection_reserved
