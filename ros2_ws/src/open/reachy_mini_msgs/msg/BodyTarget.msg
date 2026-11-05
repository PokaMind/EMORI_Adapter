# One streaming pose target: the ROS form of the daemon's POST /api/move/set_target.
#
# Fire-and-forget and UNQUEUED on purpose.  A target is the caller's latest wish, not a
# goal worth a result: the arbiter applies it directly to the backend when nothing is
# moving and answers `busy` while a move runs, which is exactly what Pollen's daemon
# does ({"status":"ignored","reason":"move_running"}).  Use the GoTo action when you
# want interpolation, a duration and an outcome.
#
# Each of the three parts is optional and carries its own `use_*` flag, because "do not
# touch the antennas" and "put the antennas at 0" are different requests and a bare
# float array cannot tell them apart.
std_msgs/Header header

bool use_head                 # when false, head_pose is ignored
geometry_msgs/Pose head_pose  # head frame relative to base_link; metres and a unit quaternion

bool use_antennas             # when false, antennas is ignored
float32[2] antennas           # [right, left] in radians - the daemon's wire order

bool use_body_yaw             # when false, body_yaw is ignored
float32 body_yaw              # radians
