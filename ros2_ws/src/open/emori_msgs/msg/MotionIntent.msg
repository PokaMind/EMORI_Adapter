# One robot-agnostic motion command: emori_adapter.models.MotionIntent, in the same units
# (millimetres and degrees for the head, radians for antennas and body yaw, seconds).
# A `has_*` flag says whether the part is commanded at all, so "leave it alone" and "put it
# at zero" stay different.
string kind
bool has_head
float32 x_mm
float32 y_mm
float32 z_mm
float32 roll_deg
float32 pitch_deg
float32 yaw_deg
bool has_antennas
float32 antenna_left_rad
float32 antenna_right_rad
bool has_body_yaw
float32 body_yaw_rad
float32 duration
string interpolation
string move
string reason
bool speech
string pace                 # fixed|voice
bool antenna_voice
bool supersede              # cut what this client's last move is still doing; carried to SubmitMove
string tint                 # the family tint a sustain beat takes
