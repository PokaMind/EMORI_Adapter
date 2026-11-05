# One step in the life of a sentence's motion plan: planned, started, ended. On
# /emori/motion/plan_events, up to 200 ms after the step.
#
# A sentence with a plan gives three messages, one with neither flag, one with started and one
# with ended. A sentence that is skipped before it reaches the player gives one, with ended and
# outcome "skipped". A deduplicated double delivery is not a sentence and gives none.
std_msgs/Header header      # when this was published, not when the step happened
string plan_id              # one per sentence; the same id as the motion trace's records
uint32 seq                  # the number on the sentence's envelope
string family               # the family performed; empty on the talking flow, or if none planned
string slug                 # the planner's name for the plan, or why there is none
bool started                # a command has gone to the robot
bool ended                  # no more messages for this plan_id
string outcome              # with ended: finished|preempted|voice_end|skipped
float32 first_apply_delay_s # from the sentence's event to the first command, the wait for the
                            # voice included; -1.0 until started
string family_authored      # the family Pokamind forwarded; empty when it forwarded none
string family_source        # how the family was chosen: pokamind|text|delivery|carried|
                            # dialogue, or empty
string skip_reason          # with outcome skipped: no_voice|refused|head_reserved|not_owner|
                            # empty_text|cue_only|cue_motion_off
