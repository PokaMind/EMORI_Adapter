# What the robot node's motion arbiter is doing right now.
#
# Published on /reachy_mini/motion_status as a LATCHED topic (transient_local, depth 1):
# a tool that attaches after the fact must still learn who owns motion without waiting
# for the next change.  Re-published on every change and at least once a second.
#
# This is the one place the arbitration the host application does in its own
# process on v1 becomes observable from outside it: `busy` is the gesture interlock the
# push-turn detector needs, `head_reserved` says the head is currently an INPUT device
# and no robot-authored motion is allowed, `owner`/`expression_owner` say which client
# may move the robot at all.
bool busy               # a move is executing right now
string running_move_id  # "" when idle; otherwise the id returned by GoTo / the REST shim
uint8 queue_depth       # requests accepted and waiting, not counting the running one
string owner            # client holding the exclusive owner lock, "" when free
bool head_reserved      # true while the head is an input device (push-turn sensing)
string expression_owner # which client may use the expression lane: hostapp|ros|none
string motor_mode       # enabled|disabled|gravity_compensation (as the daemon spells it)
string backend          # mujoco|mockup|robot
