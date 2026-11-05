# GET /api/state/full, field for field.
builtin_interfaces/Time stamp   # when the backend was read (up to the state cache age old)
string control_mode
PoseRpy head_pose
float64[] head_joints           # present_joints[:7]
float64 body_yaw
float64[] antennas_position     # [right, left]
bool has_motion
MotionBlock motion              # never cached, computed per call
