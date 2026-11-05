# The REST `motion` object, field for field.
bool active
bool head_moving          # running head move, or within the settle time of the last
bool head_reserved
string client             # running move's client, "" when idle
string lane
bool gaze_active
bool has_face_seen        # false = null on the REST wire
float64 face_seen_s       # rounded to 0.01
bool has_command
Rpy command
bool has_residual
Rpy residual              # measured @ commanded^T
